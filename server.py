#!/usr/bin/env python3
"""Loopback-only skills library. Python standard library only."""
import argparse
from collections import Counter
import base64
import struct
from urllib.parse import urlsplit
import hashlib
import json
import os
import re
import secrets
import shutil
import threading
import time
import sys
import socket
import socketserver
import subprocess
import storage
import updates
import skill_updates
import services
from portable import claude_app_sync, linked, recycle_windows
from usage import read_usage
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer as HTTPThreadingServer

BASE = Path(__file__).resolve().parent
CODEX = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex')))
TOKEN = secrets.token_urlsafe(32)
DELETE_LOCK = threading.RLock()
DATA_DIR = storage.data_home()
MANAGED = False
SKILL_UPDATER = None
BACKUP = None
CLAUDE = Path(os.environ.get('CLAUDE_CONFIG_DIR', str(Path.home() / '.claude')))
# Codex, shared agent and Claude Code skill folders; a folder linked into several roots is listed once.
ROOTS = [(CODEX / 'skills', 'Personal'), (Path.home() / '.agents/skills', 'Personal'), (CLAUDE / 'skills', 'Personal'),
         (CODEX / 'plugins/cache', 'Plugin cache'), (CLAUDE / 'plugins/cache', 'Plugin cache')]

class ThreadingHTTPServer(HTTPThreadingServer):
    def server_bind(self):
        # This server is loopback-only: reverse DNS adds no useful information and
        # can delay startup for tens of seconds on machines without a PTR response.
        socketserver.TCPServer.server_bind(self)
        self.server_name = 'localhost'
        self.server_port = self.server_address[1]


def metadata(raw, fallback):
    values = {}
    match = re.match(r'^---\s*\n(.*?)\n---(?:\s*\n|$)', raw, re.S)
    if match:
        lines = match[1].splitlines()
        for i, line in enumerate(lines):
            field = re.match(r'^(name|description):\s*(.*)$', line)
            if not field:
                continue
            key, val = field.groups()
            if val in ('>', '|', '>-', '|-') or not val:
                continuation = []
                for next_line in lines[i + 1:]:
                    if next_line and not next_line[0].isspace():
                        break
                    continuation.append(next_line.strip())
                val = ' '.join(continuation)
            elif val.startswith('"') and val.endswith('"'):
                try:
                    val = json.loads(val)
                except ValueError:
                    val = val[1:-1]
            elif val.startswith("'") and val.endswith("'"):
                val = val[1:-1].replace("''", "'")
            values[key] = val
    return values.get('name') or fallback, values.get('description') or 'Open this skill to read its instructions.'

def safe_folder(folder):
    folder = Path(folder)
    # Do not remove a root, a linked target, or any system skill.
    for root, _ in ROOTS:
        try:
            relative = folder.relative_to(root)
        except ValueError:
            continue
        if not relative.parts or '.system' in relative.parts:
            continue
        if any(linked(p) for p in [folder, *folder.parents] if p != root.parent):
            continue
        if folder.resolve().is_relative_to(root.resolve()):
            return True
    return False

def trash_skill(skill_id):
    with DELETE_LOCK:
        item = next((s for s in skills() if s['id'] == skill_id), None)
        if item is None:
            raise ValueError('Skill no longer exists. Refresh the library.')
        if not item['deletable']:
            raise ValueError('System and linked skills cannot be deleted here.')
        folders = [Path(p) for p in item['copies']]
        if not all(safe_folder(p) and (p / 'SKILL.md').is_file() for p in folders):
            raise ValueError('Skill location changed. Refresh the library.')
        if os.name == 'nt':
            recycle_windows(folders)
            return {'message': 'Moved to Recycle Bin. Start a new agent chat to pick up changes.'}
        trash = Path.home() / '.Trash'
        trash.mkdir(exist_ok=True)
        moved = []
        try:
            for folder in folders:
                destination = trash / (folder.name + '-skill-' + secrets.token_hex(6))
                shutil.move(str(folder), str(destination))
                moved.append((folder, destination))
        except OSError:
            for original, destination in reversed(moved):
                shutil.move(str(destination), str(original))
            raise
        return {'message': 'Moved to Trash. Start a new agent chat to pick up changes.'}

def read_catalog():
    """The sole source for dashboard grouping, ordering, and presentation metadata."""
    return storage.catalog(DATA_DIR)


def catalog_keys(item):
    """A skill is known in the catalog by its folder (stable across updates) and by its declared name."""
    return [key for key in dict.fromkeys([item.get('folder'), item['name']]) if key]


def matches_rule(item, rule):
    return matches_rule_keys(catalog_keys(item), rule)


def matches_rule_keys(keys, rule):
    if any(key in rule.get('excluded_skills', []) for key in keys):
        return False
    members = [member if isinstance(member, str) else member['name'] for member in rule.get('skills', [])]
    return any(key in members for key in keys) or any(key.startswith(prefix) for key in keys for prefix in rule.get('prefixes', []))


def purpose_groups(entries, config=None):
    config = config if config is not None else read_catalog()
    definitions = config['groups']
    fallback = config['fallback_group']
    groups = [{**fallback, 'kind': 'personal', 'unassigned': True}] + [{**group, 'kind': 'personal'} for group in definitions]
    labels = config.get('plugin_labels', {})
    for plugin in sorted({item['plugin'] for item in entries if item['plugin']}):
        label = labels.get(plugin, plugin.replace('-', ' ').title())
        groups.append({'id': 'plugin-' + plugin, 'label': label, 'kind': 'plugin', 'section': 'Plugins', 'area': 'Plugins', 'description': 'Everything bundled with ' + label + '.'})
    for item in entries:
        item['groupOrder'] = {}
        item['steps'] = {}
        item['catalogOrder'] = [999, 999]
        item['customized'] = any(str(Path(folder).resolve()) in config.get('customized_skills', []) for folder in item.get('copies', []))
        item['displaySource'] = item['source']
        item['compactSection'] = None
        item['compactOrder'] = 0
        if item['plugin']:
            item['pluginLabel'] = labels.get(item['plugin'], item['plugin'].replace('-', ' ').title())
            item['groups'] = ['plugin-' + item['plugin']]
            continue
        if item['source'] == 'System':
            item['groups'] = []
            continue
        item['groups'] = []
        for group_index, group in enumerate(definitions):
            if not matches_rule(item, group):
                continue
            group_id = group['id']
            item['groups'].append(group_id)
            members = group.get('skills', [])
            names = [member if isinstance(member, str) else member['name'] for member in members]
            position = next((names.index(key) for key in catalog_keys(item) if key in names), len(names))
            item['groupOrder'][group_id] = position
            if position < len(members) and isinstance(members[position], dict) and 'step' in members[position]:
                item['steps'][group_id] = members[position]['step']
            item['catalogOrder'] = min(item['catalogOrder'], [group_index, position])
        if not item['groups']:
            item['groups'] = [fallback['id']]
            item['catalogOrder'] = [-1, 0]
        for index, section in enumerate(config.get('compact_sections', []), 1):
            if item['source'] in section.get('sources', ['Mine']) and matches_rule(item, section):
                item['compactSection'] = section['id']
                item['compactOrder'] = index
                if section.get('tab'):
                    item['displaySource'] = section['tab']
                    group_id = 'toolkit-' + section['id']
                    item['groups'] = [group_id]
                    item['groupOrder'] = {group_id: 0}
                    if not any(g['id'] == group_id for g in groups):
                        groups.append({'id': group_id, 'label': section['label'], 'kind': 'toolkit',
                                       'section': section.get('section', 'Toolkits'), 'area': 'Toolkit',
                                       'description': section.get('description', '')})
                break
    return groups

def member_name(member):
    return member if isinstance(member, str) else member['name']


def place_skill(skill_id, group_id, expected=None, anchor=None, after=False):
    """Move a personal skill, or reorder it next to another skill of the same category.

    Dropping a skill on a category it already belongs to only changes its position there; moving it
    elsewhere removes it from its other categories while retaining prefix rules for other skills."""
    with DELETE_LOCK:
        entries = skills()
        item = next((s for s in entries if s['id'] == skill_id), None)
        if item is None or item['source'] != 'Mine' or item['plugin']:
            raise ValueError('Choose an existing personal skill.')
        config = read_catalog()
        before = storage.revision(config)
        if expected is not None and expected != before:
            raise storage.ConflictError('The catalog changed. Reload before moving this skill.')
        target = next((g for g in config['groups'] if g['id'] == group_id), None)
        if target is None and group_id != config['fallback_group']['id']:
            raise ValueError('Choose an existing category.')
        neighbour = None
        if anchor is not None:
            neighbour = next((s for s in entries if s['id'] == anchor and s['source'] == 'Mine' and not s['plugin']), None)
            if target is None or neighbour is None or neighbour is item or not matches_rule(neighbour, target):
                raise ValueError('Choose a skill in that category to place this one next to.')
        keys = catalog_keys(item)
        reorder = target is not None and matches_rule(item, target)
        kept = next((m for m in (target or {}).get('skills', []) if member_name(m) in keys), keys[0])
        for group in ([target] if reorder else [*config['groups'], *config.get('compact_sections', [])]):
            group['skills'] = [m for m in group.get('skills', []) if member_name(m) not in keys]
            if not reorder and any(key.startswith(prefix) for key in keys for prefix in group.get('prefixes', [])):
                group['excluded_skills'] = list(dict.fromkeys([*group.get('excluded_skills', []), keys[0]]))
        if target:
            target['excluded_skills'] = [n for n in target.get('excluded_skills', []) if n not in keys]
            members = target.setdefault('skills', [])
            if neighbour is None:
                members.append(kept)
            else:
                # Prefix-only members have no stored position yet; give them their current (alphabetical) one.
                listed = {member_name(m) for m in members}
                implicit = sorted((s for s in entries if s is not item and s['source'] == 'Mine' and not s['plugin']
                                   and matches_rule(s, target) and not listed & set(catalog_keys(s))),
                                  key=lambda s: s['name'].lower())
                members.extend(catalog_keys(s)[0] for s in implicit)
                names = catalog_keys(neighbour)
                index = next(i for i, m in enumerate(members) if member_name(m) in names)
                members.insert(index + 1 if after else index, kept)
        storage.save_catalog(config, before, DATA_DIR)
        label = target['label'] if target else 'Uncategorized'
        return {'message': ('Order updated in ' if reorder else 'Skill moved to ') + label + '.'}


def customize_skill(skill_id, expected=None):
    """Keep an installed skill's customization status in the personal catalog."""
    with DELETE_LOCK:
        item = next((s for s in skills() if s['id'] == skill_id), None)
        if item is None or item['source'] != 'Mine' or item.get('origin', {}).get('kind') != 'installed':
            raise ValueError('Choose an installed personal skill.')
        config = read_catalog()
        before = storage.revision(config)
        if expected != before:
            raise storage.ConflictError('The catalog changed. Reload before moving this skill.')
        config['customized_skills'] = list(dict.fromkeys([
            *config.get('customized_skills', []),
            *(str(Path(folder).resolve()) for folder in item['copies'])]))
        storage.save_catalog(config, before, DATA_DIR)
        return {'message': 'Customized skill moved to My skills.'}


def https_link(value):
    if not isinstance(value, str):
        return ''
    try:
        url = urlsplit(value)
        return value if url.scheme == 'https' and url.hostname and not url.username and not url.password else ''
    except ValueError:
        return ''


def skill_icon(folder):
    """Return safe inline raster data; never expose arbitrary skill files or SVG."""
    try:
        root = Path(folder).resolve()
        metadata = root / 'agents/openai.yaml'
        if metadata.stat().st_size > 65536:
            return {}
        fields = {}
        in_interface = False
        for line in metadata.read_text(encoding='utf-8').splitlines():
            if line and not line[0].isspace() and not line.startswith('#'):
                in_interface = line.strip() == 'interface:'
                continue
            match = re.match(r'^  (icon_large|icon_small):\s*(.*?)\s*$', line) if in_interface else None
            if not match:
                continue
            value = match[2]
            if value.startswith('"'):
                value = json.loads(value)
            elif value.startswith("'") and value.endswith("'"):
                value = value[1:-1].replace("''", "'")
            else:
                value = value.split(' #', 1)[0].strip()
            if isinstance(value, str):
                fields[match[1]] = value
        for field in ('icon_large', 'icon_small'):
            relative = Path(fields.get(field, ''))
            if relative.is_absolute() or '..' in relative.parts or relative.suffix.lower() != '.png':
                continue
            path = (root / relative).resolve()
            if not path.is_relative_to(root) or path.stat().st_size > 512 * 1024:
                continue
            data = path.read_bytes()
            if len(data) < 33 or data[:16] != b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR':
                continue
            width, height = struct.unpack('>II', data[16:24])
            if not (0 < width <= 4096 and 0 < height <= 4096):
                continue
            result = {'icon': 'data:image/png;base64,' + base64.b64encode(data).decode('ascii')}
            credit_path = path.with_suffix('.credit.json').resolve()
            try:
                if credit_path.is_relative_to(root) and credit_path.stat().st_size <= 8192:
                    credit = json.loads(credit_path.read_text(encoding='utf-8'))
                    if isinstance(credit, dict) and isinstance(credit.get('author'), str):
                        author_url = https_link(credit.get('authorUrl'))
                        source_url = https_link(credit.get('sourceUrl'))
                        if author_url and source_url and credit['author'].strip():
                            result['iconCredit'] = {'author': credit['author'][:160], 'authorUrl': author_url,
                                                    'sourceUrl': source_url}
            except (OSError, ValueError, RuntimeError):
                pass
            return result
    except (OSError, ValueError, RuntimeError, UnicodeError):
        pass
    return {}



# Default icons: Microsoft Fluent Emoji (MIT), shipped in icons/fluent with every release.
ICON_CREDIT = {'author': 'Microsoft Fluent Emoji (MIT)', 'authorUrl': 'https://github.com/microsoft',
               'sourceUrl': 'https://github.com/microsoft/fluentui-emoji'}
ICON_STOPWORDS = set('a an and are as at be by for from in into is it its of on or that the this to use used uses using when with without your you not only all any can do does new one such than then their them these they via what which who will'.split())
_icon_cache = {'dir': None, 'mtime': None, 'icons': []}


def icon_set_dirs():
    return [Path(__file__).resolve().parent / 'icons/fluent']


def icon_words(text):
    words = set()
    for word in re.findall(r'[a-z0-9]+', str(text).lower()):
        if len(word) < 2 or word in ICON_STOPWORDS:
            continue
        words.add(word)
        for suffix in ('ing', 'es', 's'):
            if word.endswith(suffix) and len(word) - len(suffix) >= 3:
                words.add(word[:-len(suffix)])
                break
    return words


def load_icon_set():
    """Return [(file, weighted terms)] from the first readable icon set, cached by mtime."""
    for directory in icon_set_dirs():
        index = directory / 'index.json'
        try:
            mtime = index.stat().st_mtime
            if _icon_cache['dir'] == directory and _icon_cache['mtime'] == mtime:
                return directory, _icon_cache['icons']
            icons = []
            for entry in json.loads(index.read_text(encoding='utf-8')).get('icons', []):
                name = Path(str(entry.get('file', ''))).name
                if not name.endswith('.png'):
                    continue
                terms = {}
                for word in icon_words(' '.join(entry.get('keywords', []))) | icon_words(entry.get('name', '')):
                    terms[word] = 2
                for word in icon_words(' '.join(entry.get('tags', []))):
                    terms[word] = 3
                icons.append((name, terms))
            _icon_cache.update(dir=directory, mtime=mtime, icons=icons)
            return directory, icons
        except (OSError, ValueError, AttributeError, TypeError):
            continue
    return None, []


def suggest_icon(name, description, used=None):
    """Pick the semantically closest bundled icon, or none when nothing is close enough."""
    directory, icons = load_icon_set()
    if not icons:
        return {}
    title = icon_words(str(name).replace('-', ' '))
    words = {word: 2 for word in icon_words(description)} | {word: 4 for word in title}
    best, best_score = None, 0
    for file, terms in icons:
        score = sum(weight * terms[word] for word, weight in words.items() if word in terms)
        score -= 2 * (used or {}).get(file, 0)
        if score > best_score:
            best, best_score = file, score
    if best_score < 8:
        return {}
    try:
        path = (directory / best).resolve()
        if not path.is_relative_to(directory.resolve()) or path.stat().st_size > 512 * 1024:
            return {}
        data = path.read_bytes()
    except OSError:
        return {}
    if data[:8] != b'\x89PNG\r\n\x1a\n':
        return {}
    if used is not None:
        used[best] = used.get(best, 0) + 1
    return {'icon': 'data:image/png;base64,' + base64.b64encode(data).decode('ascii'),
            'iconCredit': dict(ICON_CREDIT), 'iconSuggested': True}



def icon_choices():
    """Every bundled icon as inline PNG data for the picker."""
    directory, icons = load_icon_set()
    result = []
    for file, terms in icons:
        try:
            data = (directory / file).read_bytes()
        except OSError:
            continue
        if data[:8] == b'\x89PNG\r\n\x1a\n':
            result.append({'file': file, 'name': file[:-4].replace('_', ' '), 'terms': sorted(terms),
                           'icon': 'data:image/png;base64,' + base64.b64encode(data).decode('ascii')})
    return result


def write_icon_metadata(folder, value):
    """Point interface.icon_small/icon_large at value (or remove them when value is None)."""
    metadata = folder / 'agents/openai.yaml'
    lines = metadata.read_text(encoding='utf-8').splitlines() if metadata.exists() else []
    output, in_interface, seen_interface = [], False, False
    for line in lines:
        if line and not line[0].isspace() and not line.startswith('#'):
            if in_interface and value:
                output += [f'  icon_small: "{value}"', f'  icon_large: "{value}"']
            in_interface = line.strip() == 'interface:'
            seen_interface = seen_interface or in_interface
            output.append(line)
            continue
        if in_interface and re.match(r'^  (icon_small|icon_large):', line):
            continue
        output.append(line)
    if in_interface and value:
        output += [f'  icon_small: "{value}"', f'  icon_large: "{value}"']
    if not seen_interface and value:
        output += ['interface:', f'  icon_small: "{value}"', f'  icon_large: "{value}"']
    metadata.parent.mkdir(parents=True, exist_ok=True)
    metadata.write_text('\n'.join(output) + '\n', encoding='utf-8')


def set_skill_icon(skill_id, file):
    """Save a bundled icon into a personal skill folder, or return the skill to its automatic icon."""
    with DELETE_LOCK:
        item = next((s for s in skills() if s['id'] == skill_id), None)
        if item is None or not item['deletable']:
            raise ValueError('Icons can be changed on personal skills only.')
        source = None
        if file is not None:
            directory, icons = load_icon_set()
            if not isinstance(file, str) or file not in {name for name, _ in icons}:
                raise ValueError('Choose an icon from the list.')
            source = directory / file
        for copy in dict.fromkeys(str(Path(folder).resolve()) for folder in item['copies']):
            folder = Path(copy)
            icon, credit = folder / 'assets/icon.png', folder / 'assets/icon.credit.json'
            if source:
                icon.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, icon)
                credit.write_text(json.dumps(ICON_CREDIT, indent=2) + '\n', encoding='utf-8')
                write_icon_metadata(folder, './assets/icon.png')
            else:
                write_icon_metadata(folder, None)
                for path in (icon, credit):
                    path.unlink(missing_ok=True)
        return {'message': 'Icon saved.' if source else 'Automatic icon restored.'}


def picker_name(folder, fallback):
    """Read the picker title from the interface block without YAML dependencies."""
    try:
        raw = (Path(folder) / 'agents' / 'openai.yaml').read_text(encoding='utf-8')
    except (OSError, UnicodeError):
        return fallback
    in_interface = False
    for line in raw.splitlines():
        if line and not line[0].isspace() and not line.startswith('#'):
            in_interface = line.strip() == 'interface:'
            continue
        if not in_interface:
            continue
        match = re.match(r'^  display_name:\s*(.*?)\s*$', line)
        if match:
            value = match[1]
            if value.startswith('"'):
                try:
                    value = json.loads(value)
                except ValueError:
                    return fallback
            elif value.startswith("'") and value.endswith("'"):
                value = value[1:-1].replace("''", "'")
            else:
                value = value.split(' #', 1)[0].strip()
            return value if isinstance(value, str) and value.strip() else fallback
    return fallback

AUTO_CHECK_INTERVAL = 24 * 3600


def updatable_skills():
    entries = skills()
    purpose_groups(entries)
    return [item for item in entries if item['source'] == 'Mine' and item.get('origin', {}).get('kind') == 'installed'
            and item['origin'].get('source') and not item.get('customized')]


def auto_check_due(now=None):
    """Run one quiet check of every updatable skill when the last one is a day old. Returns True if started."""
    path = DATA_DIR / 'auto-update-check.json'
    now = time.time() if now is None else now
    last = (storage.read_json(path) or {}).get('at', 0)
    if now - last < AUTO_CHECK_INTERVAL:
        return False
    items = updatable_skills()
    if items:
        skill_updater().submit('check', items)
    storage.atomic_json(path, {'at': now})
    return bool(items)


def auto_check_loop():
    while True:
        try:
            auto_check_due()
        except (OSError, ValueError, RuntimeError):
            pass  # Busy or offline: try again next hour.
        time.sleep(3600)


def lookup_installed_skill(identifier):
    entries = skills()
    purpose_groups(entries)
    item = next((item for item in entries if item['id'] == identifier), None)
    if item is None:
        raise ValueError('This skill is no longer installed. Refresh the dashboard.')
    return item


def skill_updater():
    global SKILL_UPDATER
    with DELETE_LOCK:
        if SKILL_UPDATER is None or SKILL_UPDATER.directory != DATA_DIR:
            SKILL_UPDATER = skill_updates.Updater(DATA_DIR, lambda: ROOTS, lookup_installed_skill, DELETE_LOCK)
        return SKILL_UPDATER


def installation_sources():
    """Read installation receipts by resolved folder, never infer authorship from a name."""
    installed = {}
    for root, source in ROOTS:
        if source != 'Personal':
            continue
        receipt = root.parent / '.skill-lock.json'
        try:
            if receipt.stat().st_size > 2 * 1024 * 1024:
                continue
            value = json.loads(receipt.read_text(encoding='utf-8'))
            records = value.get('skills', {}) if isinstance(value, dict) else {}
            if not isinstance(records, dict):
                continue
            for folder, record in records.items():
                if (not isinstance(folder, str) or folder in ('.', '..') or
                        not re.fullmatch(r'[A-Za-z0-9_.-]+', folder) or not isinstance(record, dict)):
                    continue
                repository = record.get('source')
                if record.get('sourceType') != 'github' or not isinstance(repository, str):
                    continue
                if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
                    continue
                target = root / folder
                if target.is_dir():
                    installed[str(target.resolve())] = {
                        'kind': 'installed', 'label': 'Installed', 'source': repository,
                        'url': 'https://github.com/' + repository,
                        **({'skillPath': record['skillPath']} if isinstance(record.get('skillPath'), str) else {}),
                        **({'ref': record['ref']} if isinstance(record.get('ref'), str) else {}),
                    }
        except (OSError, ValueError, RuntimeError):
            continue
    return installed


def canonical(value):
    """Compare skill names case-insensitively, treating any run of other characters as one dash."""
    return re.sub(r'[^a-z0-9]+', '-', str(value or '').lower()).strip('-')


def link_source(folder, root):
    """Where a skill folder really lives when a link below a scanned root points outside every scanned root."""
    folder = Path(folder)
    try:
        relative = folder.relative_to(root)
    except ValueError:
        return ''
    if not any(linked(root.joinpath(*relative.parts[:i])) for i in range(1, len(relative.parts) + 1)):
        return ''
    real = folder.resolve()
    for scanned, _ in ROOTS:
        try:
            if real.is_relative_to(scanned.resolve()):
                return ''
        except OSError:
            continue
    return str(real)


def skill_warnings(entries):
    """Read-only health notes. They never rename, move or relink anything."""
    personal = [item for item in entries if item['source'] == 'Mine' and not item['plugin']]
    by_name = {}
    for item in personal:
        by_name.setdefault(canonical(item['name']), []).append(item)
    for item in entries:
        item['warnings'] = []
    for item in personal:
        warnings = item['warnings']
        labels = [('folder', item['folder']), ('name', item['name']), ('display name', item['displayName'])]
        if len({canonical(value) for _, value in labels}) > 1:
            warnings.append({'kind': 'name-mismatch', 'message': 'Names disagree: ' + ', '.join(
                label + ' “' + value + '”' for label, value in labels) + '. Keep the folder, name: and display_name the same.'})
        others = [copy for other in by_name[canonical(item['name'])] for copy in other['copies']
                  if other is not item]
        if len(item['copies']) > 1 or others:
            places = list(dict.fromkeys([*item['copies'], *others]))
            warnings.append({'kind': 'duplicate', 'message': 'The same skill name is in ' + str(len(places)) +
                             ' places; agents may load either one.', 'paths': places})
        for target in item.get('linkedFrom', []):
            warnings.append({'kind': 'foreign-link', 'message': 'Linked from outside your skill folders, so another tool manages it.',
                             'paths': [target]})
    return entries


def broken_links():
    """Links in personal skill folders whose target no longer exists."""
    found = []
    for root, source in ROOTS:
        if source != 'Personal' or not root.is_dir():
            continue
        try:
            children = list(root.iterdir())
        except OSError:
            continue
        for child in children:
            if linked(child) and not child.exists():
                try:
                    target = os.readlink(child)
                    # Windows reports link targets with its extended-length prefix.
                    target = target[4:] if target.startswith('\\\\?\\') else target
                except OSError:
                    target = ''
                found.append({'kind': 'broken-link', 'name': child.name, 'path': str(child), 'target': target})
    return sorted(found, key=lambda item: item['path'])


def catalog_references(config):
    """Every skill name the personal catalog lists explicitly, with the places that list it."""
    references = {}
    for group in [config['fallback_group'], *config['groups'], *config.get('compact_sections', [])]:
        for member in group.get('skills', []):
            references.setdefault(member if isinstance(member, str) else member['name'], []).append(group['label'])
    for name in config.get('installed_skills', {}):
        references.setdefault(name, []).append('Installed credits')
    return references


def missing_catalog_entries(entries, config=None):
    config = config if config is not None else read_catalog()
    present = {key for item in entries for key in catalog_keys(item)}
    return [{'kind': 'missing', 'name': name, 'places': list(dict.fromkeys(places))}
            for name, places in sorted(catalog_references(config).items()) if name not in present]


def forget_catalog_entry(name, replacement=None, expected=None):
    """Drop a skill that no longer exists from the catalog, or hand its places to a renamed skill."""
    with DELETE_LOCK:
        entries = skills()
        if not isinstance(name, str) or any(name in catalog_keys(item) for item in entries):
            raise ValueError('That skill still exists. Refresh the dashboard.')
        target = None
        if replacement is not None:
            target = next((item for item in entries if item['id'] == replacement and item['source'] == 'Mine' and not item['plugin']), None)
            if target is None:
                raise ValueError('Choose an existing personal skill.')
        config = read_catalog()
        before = storage.revision(config)
        if expected != before:
            raise storage.ConflictError('The catalog changed. Reload before changing it.')
        if name not in catalog_references(config):
            raise ValueError('That entry is no longer in your catalog.')
        replace_catalog_entry(config, name, catalog_keys(target) if target else [])
        storage.save_catalog(config, before, DATA_DIR)
        if target:
            return {'message': 'Catalog places moved to ' + target['displayName'] + '.'}
        return {'message': '“' + name + '” removed from your catalog.'}


def replace_catalog_entry(config, name, replacement_keys):
    new = next(iter(replacement_keys), None)
    keys = set(replacement_keys)
    for group in [config['fallback_group'], *config['groups'], *config.get('compact_sections', [])]:
        members = []
        already = any(member_name(member) in keys for member in group.get('skills', []))
        for member in group.get('skills', []):
            if member_name(member) != name:
                members.append(member)
            elif new and not already:
                members.append(new if isinstance(member, str) else {**member, 'name': new})
        if 'skills' in group:
            group['skills'] = members
        if name in group.get('excluded_skills', []):
            group['excluded_skills'] = [n for n in group['excluded_skills'] if n != name]
            if new and new not in group['excluded_skills']:
                group['excluded_skills'].append(new)
    credits = config.get('installed_skills', {})
    if name in credits:
        credit = credits.pop(name)
        if new and not any(key in credits for key in keys):
            credits[new] = credit


def folder_rename_state(entries):
    counts = Counter(key for item in entries for key in catalog_keys(item))
    folders = {}
    for item in entries:
        if not item.get('deletable') or item['source'] != 'Mine' or len(item['copies']) != 1:
            continue
        keys = catalog_keys(item)
        if any(counts[key] != 1 for key in keys):
            continue
        try:
            stat = Path(item['copies'][0]).stat()
        except OSError:
            continue
        # Creation time distinguishes a renamed folder from a reused file ID.
        birth = getattr(stat, 'st_birthtime_ns', getattr(stat, 'st_birthtime', None))
        if stat.st_ino and birth is not None:
            folders[f'{stat.st_dev}:{stat.st_ino}:{birth}'] = keys
    return folders, counts


def read_folder_snapshot(path):
    try:
        saved = storage.read_json(path, {})
    except (OSError, ValueError):
        return {}
    if not isinstance(saved, dict):
        return {}
    return {identity: keys for identity, keys in saved.items()
            if isinstance(keys, list) and all(isinstance(key, str) for key in keys)}


def reconcile_skill_renames():
    with DELETE_LOCK:
        entries = skills()
        config = read_catalog()
        before = storage.revision(config)
        path = DATA_DIR / 'skill-folders.json'
        previous = read_folder_snapshot(path)
        current, counts = folder_rename_state(entries)
        for identity, keys in current.items():
            old_keys = previous.get(identity, [])
            if not old_keys or old_keys == keys or any(key in counts and key not in keys for key in old_keys):
                continue
            matched = [rule for rule in [*config['groups'], *config.get('compact_sections', [])]
                       if matches_rule_keys(old_keys, rule)]
            for old in old_keys:
                if old not in keys:
                    replace_catalog_entry(config, old, keys)
            # A prefix can stop matching after a rename. Keep the previous
            # membership as an explicit entry when the new name is eligible.
            for rule in matched:
                if not matches_rule_keys(keys, rule) and not set(keys).intersection(rule.get('excluded_skills', [])):
                    rule.setdefault('skills', []).append(keys[0])
        if storage.revision(config) != before:
            storage.save_catalog(config, before, DATA_DIR)
        if current != previous:
            storage.atomic_json(path, current)
        return {'ok': True}


def skills():
    found, seen = [], set()
    installed = installation_sources()
    for root, source in ROOTS:
        if not root.exists():
            continue
        for folder, dirs, files in os.walk(root, followlinks=True):
            dirs[:] = [d for d in dirs if d not in ('node_modules', '.git', '__pycache__', '.venv') and not d.startswith(('.skill-dashboard-update-', '.skill-dashboard-old-'))]
            # Avoid symlink cycles while allowing installed linked skills.
            real = Path(folder).resolve()
            if real in seen:
                dirs[:] = []
                continue
            seen.add(real)
            if 'SKILL.md' not in files:
                continue
            path = Path(folder) / 'SKILL.md'
            try:
                raw = path.read_text(encoding='utf-8')
                stat = path.stat()
            except (OSError, UnicodeError):
                continue
            name, description = metadata(raw, path.parent.name)
            relative = path.relative_to(root)
            group = 'Mine' if source == 'Personal' else source
            plugin = ''
            if source == 'Personal' and relative.parts[0] == '.system':
                group = 'System'
            claude_app = source == 'Personal' and len(relative.parts) > 2 and claude_app_sync(root / relative.parts[0])
            if claude_app:
                group = 'System'
            if source == 'Plugin cache':
                parts = relative.parts
                plugin = parts[1] if len(parts) > 2 else parts[0]
                group = 'External'
            found.append(dict(id=hashlib.sha256(str(path).encode()).hexdigest()[:20], name=name, folder=path.parent.name, displayName=picker_name(path.parent, name), **skill_icon(path.parent), wordCount=len(raw.split()), instructionBytes=len(raw.encode("utf-8")), description=description, source=group, plugin=plugin, path=str(path), content=raw, modified=stat.st_mtime, deletable=group != 'System' and safe_folder(path.parent), copies=[str(path.parent)], claudeApp=claude_app,
                              linkedFrom=[target] if source == 'Personal' and (target := link_source(path.parent, root)) else []))
    # Collapse identical copies within each origin; retain different versions as distinct entries.
    unique = {}
    for item in found:
        key = (item['source'], item['plugin'], item['name'], item['content'])
        if key not in unique:
            unique[key] = item
        else:
            unique[key]['copies'].extend(item['copies'])
            unique[key]['deletable'] = unique[key]['deletable'] and item['deletable']
            unique[key]['linkedFrom'].extend(item['linkedFrom'])
    try:
        credits = read_catalog().get('installed_skills', {})
    except (OSError, ValueError):
        credits = {}
    for item in unique.values():
        if item.pop('claudeApp'):
            item['origin'] = {'kind': 'claude-app', 'label': 'Synced by the Claude app'}
        elif item['source'] == 'System':
            item['origin'] = {'kind': 'system', 'label': 'Built in'}
        elif item['plugin']:
            item['origin'] = {'kind': 'plugin', 'label': 'Plugin'}
        else:
            item['origin'] = next((installed[str(Path(folder).resolve())] for folder in item['copies']
                                   if str(Path(folder).resolve()) in installed),
                                  {'kind': 'local', 'label': 'Local'})
        credit_key = next((key for key in catalog_keys(item) if key in credits), None)
        if item['origin']['kind'] == 'local' and item['source'] == 'Mine' and credit_key:
            # Installed without a receipt: the catalog records who made it and, when known, its GitHub source.
            value = credits[credit_key]
            value = value if isinstance(value, dict) else {'credit': value}
            item['origin'] = {'kind': 'installed', 'label': 'Installed', 'credit': value['credit'],
                              **({'source': value['source'], 'url': 'https://github.com/' + value['source']} if value.get('source') else {}),
                              **({'skillPath': value['skillPath']} if value.get('skillPath') else {})}
        item['usage'] = read_usage(item['copies'])
    ordered = sorted(unique.values(), key=lambda s: (s['name'].lower(), s['source'], s['plugin']))
    used = {}
    for item in ordered:
        if not item.get('icon'):
            item.update(suggest_icon(item['name'], item['description'], used))
    return skill_warnings(ordered)

class Handler(BaseHTTPRequestHandler):
    def json_response(self, payload, status=200):
        data = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        expected_host = f'127.0.0.1:{self.server.server_port}'
        host = self.headers.get('Host', '')
        if host not in (expected_host, f'localhost:{self.server.server_port}'):
            return self.json_response({'error': 'Invalid host'}, 403)
        if self.headers.get('Origin') != 'http://' + host or self.headers.get('X-Library-Token') != TOKEN:
            return self.json_response({'error': 'Refresh this page before making changes.'}, 403)
        if self.path not in ('/api/delete', '/api/skill-updates', '/api/place', '/api/customize', '/api/catalog', '/api/settings', '/api/update', '/api/launcher', '/api/backup', '/api/forget', '/api/icon', '/api/skill-renames'):
            return self.json_response({'error': 'Not found'}, 404)
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if length < 1 or length > 1024 * 1024:
                raise ValueError('Invalid request')
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError('Invalid request')
            if self.path == '/api/skill-updates':
                updater = skill_updater()
                action = payload.get('action')
                if action == 'check-all':
                    return self.json_response(updater.submit('check', updatable_skills()))
                if not isinstance(payload.get('id'), str):
                    raise ValueError('Choose an installed skill.')
                item = lookup_installed_skill(payload['id'])
                if action == 'source':
                    return self.json_response(updater.set_source(item, payload.get('repository'), payload.get('path'), payload.get('ref', '')))
                if action == 'review':
                    return self.json_response(updater.review(item, payload.get('review')))
                return self.json_response(updater.submit(action, [item], payload.get('review') if action == 'apply' else payload.get('backup')))
            if self.path == '/api/catalog':
                storage.save_catalog(payload.get('catalog'), payload.get('revision'), DATA_DIR)
                return self.json_response({'message': 'Organization saved.'})
            if self.path == '/api/skill-renames':
                return self.json_response(reconcile_skill_renames())
            if self.path == '/api/forget':
                return self.json_response(forget_catalog_entry(payload.get('name'), payload.get('replacement'), payload.get('revision')))
            if self.path == '/api/backup':
                if BACKUP is None:
                    raise ValueError('GitHub backup is unavailable in this server instance.')
                return self.json_response(BACKUP.submit(payload.get('action'), payload))
            if self.path == '/api/launcher':
                if not MANAGED:
                    raise ValueError('Install the dashboard before adding its search launcher.')
                state = services.configure_shortcut(storage.install_home(), payload.get('enabled'), DATA_DIR)
                return self.json_response(state)
            if self.path == '/api/settings':
                return self.json_response(updates.set_preferences(payload, DATA_DIR))
            if self.path == '/api/update':
                if not MANAGED:
                    raise ValueError('This development checkout does not receive automatic updates.')
                return self.json_response(updates.request_update(DATA_DIR))
            if not isinstance(payload.get('id'), str):
                raise ValueError('Invalid skill')
            if self.path == '/api/icon':
                return self.json_response(set_skill_icon(payload['id'], payload.get('file')))
            if self.path == '/api/customize':
                return self.json_response(customize_skill(payload['id'], payload.get('revision')))
            if self.path == '/api/place':
                if not isinstance(payload.get('group'), str):
                    raise ValueError('Invalid category')
                anchor = payload.get('anchor')
                if anchor is not None and not isinstance(anchor, str):
                    raise ValueError('Invalid skill')
                self.json_response(place_skill(payload['id'], payload['group'], payload.get('revision'), anchor, payload.get('after') is True))
            else:
                self.json_response(trash_skill(payload['id']))
        except storage.ConflictError as error:
            self.json_response({'error': str(error)}, 409)
        except (ValueError, OSError, subprocess.SubprocessError) as error:
            self.json_response({'error': str(error)}, 400)

    def do_GET(self):
        if self.headers.get('Host') not in (f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'):
            self.send_error(403)
            return
        route = self.path.split('?')[0]
        if route == '/api/backup':
            return self.json_response(BACKUP.status() if BACKUP else {'available': False, 'busy': False, 'message': 'GitHub backup is unavailable.'})
        if route == '/api/icons':
            return self.json_response({'icons': icon_choices()})
        if route == '/api/health':
            return self.json_response({'application': 'skill-dashboard', 'pid': os.getpid()})
        if route == '/api/catalog':
            config = read_catalog()
            return self.json_response({'catalog': config, 'revision': storage.revision(config)})
        if route == '/api/settings':
            state = updates.status(DATA_DIR)
            state['managed'] = MANAGED
            state['launcher'] = services.shortcut_status(storage.install_home()) if MANAGED else {'supported': False, 'installed': False, 'conflict': False, 'label': 'Search launcher'}
            if not MANAGED:
                state['version'] = 'development'
            return self.json_response(state)
        if route == '/api/skills':
            entries = skills()
            updater = skill_updater()
            for item in entries:
                if item.get('origin', {}).get('kind') == 'installed' and item['origin'].get('source'):
                    item['skillUpdate'] = updater.describe(item)
            config = read_catalog()
            groups = purpose_groups(entries, config)
            issues = missing_catalog_entries(entries, config) + broken_links()
            folders, _ = folder_rename_state(entries)
            renames_pending = folders != read_folder_snapshot(DATA_DIR / 'skill-folders.json')
            data = json.dumps(dict(skills=entries, issues=issues, renamesPending=renames_pending, skillUpdates=updater.status(), groups=groups, display=config.get('display', {}), compactSections=config.get('compact_sections', []), catalogRevision=storage.revision(config), platform=sys.platform, uiRevision=hashlib.sha256((BASE / 'dist/index.html').read_bytes()).hexdigest(), token=TOKEN), ensure_ascii=False).encode()
            mime = 'application/json; charset=utf-8'
        elif route in ('/', '/index.html'):
            data = (BASE / 'dist/index.html').read_bytes()
            mime = 'text/html; charset=utf-8'
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(data)

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int)
    parser.add_argument('--data-dir', type=Path, default=storage.data_home())
    parser.add_argument('--managed', action='store_true')
    args = parser.parse_args()
    MANAGED = args.managed
    DATA_DIR = args.data_dir.expanduser().resolve()
    storage.ensure_catalog(DATA_DIR)
    preferences = updates.preferences(DATA_DIR)
    port = args.port if args.port is not None else preferences.get('port', 8765)
    try:
        server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    except OSError:
        if args.port is not None:
            raise
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    preferences['port'] = server.server_port
    storage.atomic_json(DATA_DIR / 'settings.json', preferences)
    url = f'http://127.0.0.1:{server.server_port}'
    if args.managed:
        storage.atomic_json(DATA_DIR / 'server.json', {'url': url, 'pid': os.getpid()})
    BACKUP = services.GitHubBackup(storage.install_home(), DATA_DIR, [('codex', CODEX / 'skills'), ('agents', Path.home() / '.agents/skills'), ('claude', CLAUDE / 'skills')])
    BACKUP.start_scheduler()
    threading.Thread(target=auto_check_loop, daemon=True, name='skill-update-check').start()
    print(f'MySkills: {url}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        BACKUP.stop_event.set()
        server.server_close()

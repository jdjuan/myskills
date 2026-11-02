#!/usr/bin/env python3
"""Loopback-only skills library. Python standard library only."""
import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import threading
import sys
import socket
import storage
import updates
from portable import linked, recycle_windows
from usage import read_usage
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = Path(__file__).resolve().parent
CODEX = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex')))
TOKEN = secrets.token_urlsafe(32)
DELETE_LOCK = threading.RLock()
DATA_DIR = storage.data_home()
MANAGED = False
ROOTS = [(CODEX / 'skills', 'Personal'), (Path.home() / '.agents/skills', 'Personal'), (CODEX / 'plugins/cache', 'Plugin cache')]

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
            return {'message': 'Moved to Recycle Bin. Restart Codex chats to pick up changes.'}
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
        return {'message': 'Moved to Trash. Restart Codex chats to pick up changes.'}

def read_catalog():
    """The sole source for dashboard grouping, ordering, and presentation metadata."""
    return storage.catalog(DATA_DIR)


def matches_rule(item, rule):
    if item['name'] in rule.get('excluded_skills', []):
        return False
    members = [member if isinstance(member, str) else member['name'] for member in rule.get('skills', [])]
    return item['name'] in members or any(item['name'].startswith(prefix) for prefix in rule.get('prefixes', []))


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
            position = names.index(item['name']) if item['name'] in names else len(names)
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

def place_skill(skill_id, group_id, expected=None):
    """Move a personal skill while retaining category prefix rules for other skills."""
    with DELETE_LOCK:
        item = next((s for s in skills() if s['id'] == skill_id), None)
        if item is None or item['source'] != 'Mine' or item['plugin']:
            raise ValueError('Choose an existing personal skill.')
        config = read_catalog()
        before = storage.revision(config)
        if expected is not None and expected != before:
            raise storage.ConflictError('The catalog changed. Reload before moving this skill.')
        target = next((g for g in config['groups'] if g['id'] == group_id), None)
        if target is None and group_id != config['fallback_group']['id']:
            raise ValueError('Choose an existing category.')
        for group in [*config['groups'], *config.get('compact_sections', [])]:
            group['skills'] = [m for m in group.get('skills', [])
                               if (m if isinstance(m, str) else m['name']) != item['name']]
            if any(item['name'].startswith(prefix) for prefix in group.get('prefixes', [])):
                group['excluded_skills'] = list(dict.fromkeys([*group.get('excluded_skills', []), item['name']]))
        if target:
            target['excluded_skills'] = [n for n in target.get('excluded_skills', []) if n != item['name']]
            target.setdefault('skills', []).append(item['name'])
        storage.save_catalog(config, before, DATA_DIR)
        return {'message': 'Skill moved to ' + (target['label'] if target else 'Uncategorized') + '.'}


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

def skills():
    found, seen = [], set()
    for root, source in ROOTS:
        if not root.exists():
            continue
        for folder, dirs, files in os.walk(root, followlinks=True):
            dirs[:] = [d for d in dirs if d not in ('node_modules', '.git', '__pycache__', '.venv')]
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
            if source == 'Plugin cache':
                parts = relative.parts
                plugin = parts[1] if len(parts) > 2 else parts[0]
                group = 'External'
            found.append(dict(id=hashlib.sha256(str(path).encode()).hexdigest()[:20], name=name, displayName=picker_name(path.parent, name), wordCount=len(raw.split()), instructionBytes=len(raw.encode("utf-8")), description=description, source=group, plugin=plugin, path=str(path), content=raw, modified=stat.st_mtime, deletable=group != 'System' and safe_folder(path.parent), copies=[str(path.parent)]))
    # Collapse identical copies within each origin; retain different versions as distinct entries.
    unique = {}
    for item in found:
        key = (item['source'], item['plugin'], item['name'], item['content'])
        if key not in unique:
            unique[key] = item
        else:
            unique[key]['copies'].extend(item['copies'])
            unique[key]['deletable'] = unique[key]['deletable'] and item['deletable']
    for item in unique.values():
        item['usage'] = read_usage(item['copies'])
    return sorted(unique.values(), key=lambda s: (s['name'].lower(), s['source'], s['plugin']))

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
        if self.path not in ('/api/delete', '/api/place', '/api/catalog', '/api/settings', '/api/update'):
            return self.json_response({'error': 'Not found'}, 404)
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if length < 1 or length > 1024 * 1024:
                raise ValueError('Invalid request')
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError('Invalid request')
            if self.path == '/api/catalog':
                storage.save_catalog(payload.get('catalog'), payload.get('revision'), DATA_DIR)
                return self.json_response({'message': 'Organization saved.'})
            if self.path == '/api/settings':
                return self.json_response(updates.set_preferences(payload, DATA_DIR))
            if self.path == '/api/update':
                if not MANAGED:
                    raise ValueError('This development checkout does not receive automatic updates.')
                return self.json_response(updates.request_update(DATA_DIR))
            if not isinstance(payload.get('id'), str):
                raise ValueError('Invalid skill')
            if self.path == '/api/place':
                if not isinstance(payload.get('group'), str):
                    raise ValueError('Invalid category')
                self.json_response(place_skill(payload['id'], payload['group'], payload.get('revision')))
            else:
                self.json_response(trash_skill(payload['id']))
        except storage.ConflictError as error:
            self.json_response({'error': str(error)}, 409)
        except (ValueError, OSError) as error:
            self.json_response({'error': str(error)}, 400)

    def do_GET(self):
        if self.headers.get('Host') not in (f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'):
            self.send_error(403)
            return
        route = self.path.split('?')[0]
        if route == '/api/health':
            return self.json_response({'application': 'skill-dashboard', 'pid': os.getpid()})
        if route == '/api/catalog':
            config = read_catalog()
            return self.json_response({'catalog': config, 'revision': storage.revision(config)})
        if route == '/api/settings':
            state = updates.status(DATA_DIR)
            state['managed'] = MANAGED
            if not MANAGED:
                state['version'] = 'development'
            return self.json_response(state)
        if route == '/api/skills':
            entries = skills()
            config = read_catalog()
            groups = purpose_groups(entries, config)
            data = json.dumps(dict(skills=entries, groups=groups, display=config.get('display', {}), compactSections=config.get('compact_sections', []), catalogRevision=storage.revision(config), platform=sys.platform, uiRevision=hashlib.sha256((BASE / 'dist/index.html').read_bytes()).hexdigest(), token=TOKEN), ensure_ascii=False).encode()
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
    print(f'Skill dashboard: {url}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

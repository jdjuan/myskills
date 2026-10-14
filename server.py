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
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = Path(__file__).resolve().parent
CODEX = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex')))
TOKEN = secrets.token_urlsafe(32)
DELETE_LOCK = threading.Lock()
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
        if any(p.is_symlink() for p in [folder, *folder.parents] if p != root.parent):
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
    return json.loads((BASE / 'catalog.json').read_text(encoding='utf-8'))


def matches_rule(item, rule):
    members = [member if isinstance(member, str) else member['name'] for member in rule.get('skills', [])]
    return item['name'] in members or any(item['name'].startswith(prefix) for prefix in rule.get('prefixes', []))


def purpose_groups(entries, config=None):
    config = config if config is not None else read_catalog()
    definitions = config['groups']
    fallback = config['fallback_group']
    groups = [{**group, 'kind': 'personal'} for group in definitions] + [{**fallback, 'kind': 'personal'}]
    labels = config.get('plugin_labels', {})
    for plugin in sorted({item['plugin'] for item in entries if item['plugin']}):
        label = labels.get(plugin, plugin.replace('-', ' ').title())
        groups.append({'id': 'plugin-' + plugin, 'label': label, 'kind': 'plugin', 'section': 'Plugins', 'area': 'Plugins', 'description': 'Everything bundled with ' + label + '.'})
    for item in entries:
        item['groupOrder'] = {}
        item['steps'] = {}
        item['catalogOrder'] = [999, 999]
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
        for index, section in enumerate(config.get('compact_sections', []), 1):
            if item['source'] in section.get('sources', ['Mine']) and matches_rule(item, section):
                item['compactSection'] = section['id']
                item['compactOrder'] = index
                break
    return groups

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
            found.append(dict(id=hashlib.sha256(str(path).encode()).hexdigest()[:20], name=name, description=description, source=group, plugin=plugin, path=str(path), content=raw, modified=stat.st_mtime, deletable=group != 'System' and safe_folder(path.parent), copies=[str(path.parent)]))
    # Collapse identical copies within each origin; retain different versions as distinct entries.
    unique = {}
    for item in found:
        key = (item['source'], item['plugin'], item['name'], item['content'])
        if key not in unique:
            unique[key] = item
        else:
            unique[key]['copies'].extend(item['copies'])
            unique[key]['deletable'] = unique[key]['deletable'] and item['deletable']
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
            return self.json_response({'error': 'Refresh this page before deleting.'}, 403)
        if self.path != '/api/delete':
            return self.json_response({'error': 'Not found'}, 404)
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if length < 1 or length > 1024:
                raise ValueError('Invalid request')
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict) or not isinstance(payload.get('id'), str):
                raise ValueError('Invalid skill')
            self.json_response(trash_skill(payload['id']))
        except (ValueError, OSError) as error:
            self.json_response({'error': str(error)}, 400)

    def do_GET(self):
        if self.headers.get('Host', '').split(':')[0] not in ('127.0.0.1', 'localhost'):
            self.send_error(403)
            return
        route = self.path.split('?')[0]
        if route == '/api/skills':
            entries = skills()
            config = read_catalog()
            groups = purpose_groups(entries, config)
            data = json.dumps(dict(skills=entries, groups=groups, display=config.get('display', {}), compactSections=config.get('compact_sections', []), token=TOKEN), ensure_ascii=False).encode()
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
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    print(f'Skills library: http://127.0.0.1:{args.port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()

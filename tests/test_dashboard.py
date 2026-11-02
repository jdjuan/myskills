import copy
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server
import storage


def config():
    return {'version': 1, 'groups': [
        {'id': 'writing', 'label': 'Writing', 'section': 'Work', 'area': 'Work', 'skills': [{'name': 'draft', 'step': 1}, 'revise']},
        {'id': 'tools', 'label': 'Tools', 'section': 'Work', 'skills': [], 'prefixes': ['tool-']}],
        'fallback_group': {'id': 'other', 'label': 'Uncategorized', 'section': 'Needs placement'},
        'plugin_labels': {'sample-plugin': 'Sample'}, 'compact_sections': []}


def entry(name, source='Mine', plugin=''):
    return dict(name=name, source=source, plugin=plugin)


class IsolatedState(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.data = self.base / 'data'
        storage.atomic_json(self.data / 'catalog.json', config())
        patcher = patch.object(server, 'DATA_DIR', self.data)
        patcher.start(); self.addCleanup(patcher.stop)


class CatalogTests(IsolatedState):
    def test_grouping_order_steps_and_sources(self):
        entries = [entry('revise'), entry('draft'), entry('tool-new'), entry('unknown'), entry('draft', 'External', 'sample-plugin'), entry('builtin', 'System')]
        groups = server.purpose_groups(entries)
        self.assertEqual(groups[0]['id'], 'other')
        self.assertEqual(entries[0]['groups'], ['writing'])
        self.assertLess(entries[1]['catalogOrder'], entries[0]['catalogOrder'])
        self.assertEqual(entries[1]['steps'], {'writing': 1})
        self.assertEqual(entries[2]['groups'], ['tools'])
        self.assertEqual(entries[3]['groups'], ['other'])
        self.assertEqual(entries[4]['groups'], ['plugin-sample-plugin'])
        self.assertEqual(entries[4]['pluginLabel'], 'Sample')
        self.assertEqual(entries[5]['groups'], [])

    def test_catalog_order_is_data_not_code(self):
        value = config(); value['groups'].reverse()
        entries = [entry('draft'), entry('tool-new')]
        server.purpose_groups(entries, value)
        self.assertLess(entries[1]['catalogOrder'], entries[0]['catalogOrder'])

    def test_compact_rules_and_multi_membership_preserved(self):
        value = config()
        value['groups'][1]['skills'] = ['draft']
        value['compact_sections'] = [{'id': 'toolkit', 'label': 'Toolkit', 'prefixes': ['tool-'], 'tab': 'External'}]
        entries = [entry('draft'), entry('tool-new')]
        server.purpose_groups(entries, value)
        self.assertEqual(entries[0]['groups'], ['writing', 'tools'])
        self.assertEqual(entries[1]['displaySource'], 'External')
        self.assertEqual(entries[1]['groups'], ['toolkit-toolkit'])

    def test_move_categorized_prefix_member_and_uncategorize(self):
        item = dict(entry('tool-new'), id='example')
        with patch.object(server, 'skills', return_value=[item]):
            server.place_skill('example', 'writing')
            entries = [entry('tool-new')]; server.purpose_groups(entries)
            self.assertEqual(entries[0]['groups'], ['writing'])
            server.place_skill('example', 'other')
            entries = [entry('tool-new')]; server.purpose_groups(entries)
            self.assertEqual(entries[0]['groups'], ['other'])
            with self.assertRaises(ValueError): server.place_skill('missing', 'tools')

    def test_conflict_preserves_external_edit(self):
        old = server.read_catalog()
        revision = storage.revision(old)
        updated = copy.deepcopy(old); updated['groups'][0]['label'] = 'Changed by Codex'
        storage.save_catalog(updated, revision, self.data)
        with self.assertRaises(storage.ConflictError): storage.save_catalog(old, revision, self.data)
        self.assertEqual(server.read_catalog()['groups'][0]['label'], 'Changed by Codex')

    def test_removing_category_does_not_delete_skills(self):
        value = server.read_catalog(); before = storage.revision(value)
        value['groups'].pop(0)
        storage.save_catalog(value, before, self.data)
        entries = [entry('draft')]; server.purpose_groups(entries)
        self.assertEqual(entries[0]['groups'], ['other'])

    def test_invalid_catalog_is_not_saved(self):
        value = server.read_catalog(); value['groups'].append(copy.deepcopy(value['groups'][0]))
        with self.assertRaises(ValueError): storage.save_catalog(value, 'anything', self.data)
        self.assertEqual(len(server.read_catalog()['groups']), 2)

    def test_neutral_template_contains_no_personal_members(self):
        template = storage.read_json(server.BASE / 'catalog.json')
        self.assertEqual(template['groups'], [])
        self.assertEqual(template['compact_sections'], [])


class DiscoveryTests(IsolatedState):
    def test_picker_title_refresh_and_fallback(self):
        root = self.base / 'skills'; root.mkdir()
        (root / 'SKILL.md').write_text('---\nname: fixture\ndescription: Example\n---\nBody', encoding='utf-8')
        (root / 'agents').mkdir()
        metadata = root / 'agents/openai.yaml'
        with patch.object(server, 'ROOTS', [(root, 'Personal')]):
            metadata.write_text('interface:\n  display_name: "First"\n', encoding='utf-8')
            self.assertEqual(server.skills()[0]['displayName'], 'First')
            metadata.write_text("interface:\n  display_name: 'Second'\n", encoding='utf-8')
            self.assertEqual(server.skills()[0]['displayName'], 'Second')
            metadata.unlink()
            self.assertEqual(server.skills()[0]['displayName'], 'fixture')

    def test_deletion_protects_system_and_links(self):
        root = self.base / 'skills'; root.mkdir()
        own = root / 'example'; own.mkdir(); (own / 'SKILL.md').write_text('---\nname: example\n---\nBody')
        system = root / '.system/builtin'; system.mkdir(parents=True); (system / 'SKILL.md').write_text('Protected')
        with patch.object(server, 'ROOTS', [(root, 'Personal')]):
            entries = server.skills(); item = next(s for s in entries if s['name'] == 'example')
            protected = next(s for s in entries if s['source'] == 'System')
            self.assertFalse(server.safe_folder(root))
            with self.assertRaises(ValueError): server.trash_skill(protected['id'])
            if sys.platform == 'win32':
                with patch.object(server, 'recycle_windows') as recycle:
                    server.trash_skill(item['id']); recycle.assert_called_once_with([own])
            else:
                with patch.object(Path, 'home', return_value=self.base): server.trash_skill(item['id'])
                self.assertFalse(own.exists())
                self.assertEqual(len(list((self.base / '.Trash').glob('*/SKILL.md'))), 1)
            self.assertTrue(system.exists())


class HttpTests(IsolatedState):
    def setUp(self):
        super().setUp()
        self.http = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True); self.thread.start()
        self.url = 'http://127.0.0.1:' + str(self.http.server_port)
        self.addCleanup(self.close_server)
        patcher = patch.object(server, 'skills', return_value=[entry('draft')])
        patcher.start(); self.addCleanup(patcher.stop)

    def close_server(self):
        self.http.shutdown(); self.http.server_close(); self.thread.join()

    def request(self, path, payload=None, headers=None):
        defaults = {'Origin': self.url, 'X-Library-Token': server.TOKEN}
        request = urllib.request.Request(self.url + path, data=json.dumps(payload).encode() if payload is not None else None,
                                         headers=defaults if headers is None else headers)
        with urllib.request.urlopen(request) as response: return json.load(response)

    def test_snapshot_health_and_private_files(self):
        snapshot = self.request('/api/skills')
        self.assertEqual(snapshot['skills'][0]['groups'], ['writing'])
        self.assertIn('catalogRevision', snapshot)
        self.assertEqual(self.request('/api/health')['application'], 'skill-dashboard')
        for path in ['/catalog.json', '/settings.json', '/server.py']:
            with self.assertRaises(urllib.error.HTTPError) as error: self.request(path)
            self.assertEqual(error.exception.code, 404); error.exception.close()

    def test_mutations_require_origin_and_token(self):
        for path in ['/api/delete', '/api/place', '/api/catalog', '/api/settings', '/api/update']:
            for headers in [{'Origin': 'https://example.com', 'X-Library-Token': server.TOKEN}, {'Origin': self.url}]:
                with self.assertRaises(urllib.error.HTTPError) as error: self.request(path, {}, headers)
                self.assertEqual(error.exception.code, 403); error.exception.close()

    def test_catalog_roundtrip_and_stale_save(self):
        value = self.request('/api/catalog')
        value['catalog']['groups'][0]['label'] = 'New name'
        self.request('/api/catalog', value)
        self.assertEqual(self.request('/api/catalog')['catalog']['groups'][0]['label'], 'New name')
        with self.assertRaises(urllib.error.HTTPError) as error: self.request('/api/catalog', value)
        self.assertEqual(error.exception.code, 409); error.exception.close()

    def test_settings_persist_and_preserve_internal_fields(self):
        storage.atomic_json(self.data / 'settings.json', {'port': 9000, 'codexHome': 'example'})
        self.request('/api/settings', {'autoUpdate': False})
        self.assertFalse(self.request('/api/settings')['autoUpdate'])
        self.assertEqual(storage.read_json(self.data / 'settings.json')['port'], 9000)

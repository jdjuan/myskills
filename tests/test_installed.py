"""Installed collection routing and explicit customization persistence."""
import json
from pathlib import Path
import shutil
import subprocess
from unittest.mock import patch
import test_dashboard as helpers
import server
import storage


class InstalledTests(helpers.IsolatedState):
    def test_receipt_and_customization_survive_rediscovery(self):
        root = self.base / 'agents/skills'
        folder = root / 'example'
        folder.mkdir(parents=True)
        (folder / 'SKILL.md').write_text('---\nname: example\n---\nInstructions', encoding='utf-8')
        storage.atomic_json(root.parent / '.skill-lock.json', {'skills': {
            'example': {'source': 'owner/repo', 'sourceType': 'github'}}})
        with patch.object(server, 'ROOTS', [(root, 'Personal')]):
            item = server.skills()[0]
            self.assertEqual(item['origin']['kind'], 'installed')
            server.purpose_groups([item])
            self.assertFalse(item['customized'])
            with self.assertRaises(storage.ConflictError):
                server.customize_skill(item['id'], 'stale')
            server.customize_skill(item['id'], storage.revision(server.read_catalog()))
            (folder / 'SKILL.md').write_text('---\nname: renamed-example\n---\nCustomized', encoding='utf-8')
            item = server.skills()[0]
            server.purpose_groups([item])
            self.assertTrue(item['customized'])
            self.assertEqual(item['origin']['source'], 'owner/repo')
            self.assertEqual(server.read_catalog()['customized_skills'], [str(folder.resolve())])

    def test_reject_customizing_system_or_uninstalled_skill(self):
        for source, kind in [('System', 'system'), ('Mine', 'local'), ('External', 'plugin')]:
            with patch.object(server, 'skills', return_value=[dict(id='x', source=source, origin={'kind': kind})]):
                with self.assertRaises(ValueError):
                    server.customize_skill('x', storage.revision(server.read_catalog()))
        for value in [None, {}, [1]]:
            config = helpers.config();config['customized_skills'] = value
            with self.assertRaises(ValueError):
                storage.validate_catalog(config)

    def test_collection_routing(self):
        if not shutil.which('node'):
            self.skipTest('Node unavailable')
        html = (Path(server.BASE) / 'dist/index.html').read_text(encoding='utf-8')
        self.assertIn('id="tab-installed"', html)
        code = html[html.index('function collection('):html.index('function render(){')]
        cases = '''
const assert=require('node:assert/strict');
assert.equal(collection({source:'Mine',origin:{kind:'installed'}}),'Installed');
assert.equal(collection({source:'Mine',origin:{kind:'installed'},displaySource:'System'}),'Installed');
assert.equal(collection({source:'Mine',origin:{kind:'installed'},customized:true,displaySource:'System'}),'Mine');
assert.equal(collection({source:'Mine'}),'Mine');
assert.equal(collection({source:'External',origin:{kind:'plugin'}}),'External');
assert.equal(collection({source:'System'}),'System');
'''
        result = subprocess.run(['node'], input=code+cases, text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

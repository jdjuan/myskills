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

    def test_catalog_credit_marks_skill_installed_without_update_source(self):
        root = self.base / 'agents/skills'
        for name in ('borrowed', 'own'):
            (root / name).mkdir(parents=True)
            (root / name / 'SKILL.md').write_text('---\nname: ' + name + '\n---\nInstructions', encoding='utf-8')
        config = server.read_catalog()
        config['installed_skills'] = {'borrowed': 'Someone Else'}
        storage.save_catalog(config, storage.revision(server.read_catalog()), server.DATA_DIR)
        with patch.object(server, 'ROOTS', [(root, 'Personal')]):
            items = {item['name']: item for item in server.skills()}
        self.assertEqual(items['borrowed']['origin'], {'kind': 'installed', 'label': 'Installed', 'credit': 'Someone Else'})
        self.assertEqual(items['own']['origin']['kind'], 'local')

    def test_catalog_source_enables_update_checks(self):
        root = self.base / 'agents/skills'
        (root / 'borrowed').mkdir(parents=True)
        (root / 'borrowed' / 'SKILL.md').write_text('---\nname: borrowed\n---\nInstructions', encoding='utf-8')
        config = server.read_catalog()
        config['installed_skills'] = {'borrowed': {'credit': 'Owner', 'source': 'owner/repo', 'skillPath': 'skills/borrowed/SKILL.md'}}
        storage.save_catalog(config, storage.revision(server.read_catalog()), server.DATA_DIR)
        with patch.object(server, 'ROOTS', [(root, 'Personal')]):
            origin = server.skills()[0]['origin']
        self.assertEqual(origin['source'], 'owner/repo')
        self.assertEqual(origin['skillPath'], 'skills/borrowed/SKILL.md')
        self.assertEqual(origin['credit'], 'Owner')

    def test_daily_check_runs_once_per_day(self):
        calls = []
        class Fake:
            def submit(self, action, items):
                calls.append((action, [i['name'] for i in items]))
        with patch.object(server, 'skill_updater', return_value=Fake()), \
             patch.object(server, 'updatable_skills', return_value=[{'name': 'example'}]):
            self.assertTrue(server.auto_check_due(now=1_000_000))
            self.assertFalse(server.auto_check_due(now=1_000_000 + 3600))
            self.assertTrue(server.auto_check_due(now=1_000_000 + 25 * 3600))
        self.assertEqual(calls, [('check', ['example']), ('check', ['example'])])

    def test_renamed_skill_keeps_category_and_credit_by_folder(self):
        root = self.base / 'agents/skills'
        (root / 'aeo').mkdir(parents=True)
        (root / 'aeo' / 'SKILL.md').write_text('---\nname: ai-seo\n---\nInstructions', encoding='utf-8')
        config = server.read_catalog()
        config['groups'].append({'id': 'seo', 'label': 'SEO', 'section': 'Marketing', 'skills': ['aeo']})
        config['installed_skills'] = {'aeo': {'credit': 'Owner', 'source': 'owner/repo'}}
        storage.save_catalog(config, storage.revision(server.read_catalog()), server.DATA_DIR)
        with patch.object(server, 'ROOTS', [(root, 'Personal')]):
            item = server.skills()[0]
            server.purpose_groups([item])
        self.assertEqual(item['name'], 'ai-seo')
        self.assertEqual(item['groups'], ['seo'])
        self.assertEqual(item['origin']['source'], 'owner/repo')

    def test_reject_invalid_installed_credits(self):
        config = server.read_catalog()
        for value in ('', {'credit': 'Owner', 'source': 'not a repo'}, {'credit': 'Owner', 'skillPath': '../x/SKILL.md'}, {'source': 'owner/repo'}):
            config['installed_skills'] = {'borrowed': value}
            with self.assertRaises(ValueError):
                storage.validate_catalog(config)

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

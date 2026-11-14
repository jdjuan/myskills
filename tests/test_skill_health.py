"""Read-only health checks: stale catalog entries, name mismatches, duplicates and links."""
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import server
import storage
from test_dashboard import IsolatedState


def make_skill(folder, name=None, display=None, body='Body'):
    folder.mkdir(parents=True)
    (folder / 'SKILL.md').write_text(f'---\nname: {name or folder.name}\ndescription: Example\n---\n{body}', encoding='utf-8')
    if display:
        (folder / 'agents').mkdir()
        (folder / 'agents/openai.yaml').write_text(f'interface:\n  display_name: "{display}"\n', encoding='utf-8')
    return folder


def symlinks_supported():
    import tempfile
    with tempfile.TemporaryDirectory() as directory:
        try:
            (Path(directory) / 'link').symlink_to(directory)
            return True
        except (OSError, NotImplementedError):
            return False


SYMLINKS = symlinks_supported()


def snapshot(*roots):
    """Every path, link target and file body under the roots, to prove checks never touch disk."""
    state = {}
    for root in roots:
        for folder, dirs, files in os.walk(root):
            for name in dirs + files:
                path = Path(folder) / name
                state[str(path)] = os.readlink(path) if path.is_symlink() else path.read_bytes() if path.is_file() else None
    return state


class HealthBase(IsolatedState):
    def setUp(self):
        super().setUp()
        self.agents = self.base / 'agents'
        self.codex = self.base / 'codex'
        self.store = self.base / 'skills-manager/skills'
        for folder in (self.agents, self.codex, self.store):
            folder.mkdir(parents=True)
        patcher = patch.object(server, 'ROOTS', [(self.codex, 'Personal'), (self.agents, 'Personal')])
        patcher.start(); self.addCleanup(patcher.stop)

    def warnings(self, name):
        item = next(item for item in server.skills() if item['name'] == name)
        return {warning['kind']: warning for warning in item['warnings']}


class HealthTests(HealthBase):

    def test_canonical_names(self):
        self.assertEqual(server.canonical('Add Product to jdjuan.io'), 'add-product-to-jdjuan-io')
        self.assertEqual(server.canonical('  Reel  Maker '), 'reel-maker')

    def test_matching_names_have_no_warning(self):
        make_skill(self.agents / 'add-product-to-jdjuan-io', display='Add Product to jdjuan.io')
        make_skill(self.agents / 'plain')
        self.assertEqual(self.warnings('add-product-to-jdjuan-io'), {})
        self.assertEqual(self.warnings('plain'), {})

    def test_name_mismatch_is_warned_not_renamed(self):
        make_skill(self.agents / 'create-demo', name='create-demo', display='Demo Video')
        make_skill(self.agents / 'talking-video', name='reel-maker')
        before = snapshot(self.agents)
        self.assertIn('Demo Video', self.warnings('create-demo')['name-mismatch']['message'])
        self.assertIn('talking-video', self.warnings('reel-maker')['name-mismatch']['message'])
        self.assertEqual(snapshot(self.agents), before)

    def test_duplicates_across_roots(self):
        make_skill(self.codex / 'grilling')
        make_skill(self.agents / 'grilling')
        make_skill(self.codex / 'seo', body='Old')
        make_skill(self.agents / 'seo', body='New')
        same = self.warnings('grilling')['duplicate']
        self.assertEqual(sorted(same['paths']), sorted([str(self.codex / 'grilling'), str(self.agents / 'grilling')]))
        versions = [item for item in server.skills() if item['name'] == 'seo']
        self.assertEqual(len(versions), 2)
        for item in versions:
            self.assertEqual(item['warnings'][0]['kind'], 'duplicate')

    @unittest.skipUnless(SYMLINKS, 'This system cannot create symbolic links')
    def test_link_into_another_managers_store(self):
        make_skill(self.store / 'teach')
        (self.agents / 'teach').symlink_to(self.store / 'teach')
        make_skill(self.agents / 'local')
        (self.codex / 'local').symlink_to(self.agents / 'local')  # Between scanned roots: not foreign.
        before = snapshot(self.agents, self.codex, self.store.parent)
        warning = self.warnings('teach')['foreign-link']
        self.assertEqual(warning['paths'], [str((self.store / 'teach').resolve())])
        self.assertNotIn('foreign-link', self.warnings('local'))
        self.assertEqual(snapshot(self.agents, self.codex, self.store.parent), before)

    @unittest.skipUnless(SYMLINKS, 'This system cannot create symbolic links')
    def test_broken_links_are_reported_only(self):
        (self.agents / 'gone').symlink_to(self.store / 'gone')
        issues = server.broken_links()
        self.assertEqual([(i['name'], i['target']) for i in issues], [('gone', str(self.store / 'gone'))])
        self.assertTrue((self.agents / 'gone').is_symlink())


class StaleCatalogTests(HealthBase):
    def stale_catalog(self):
        config = server.read_catalog()
        config['groups'][0]['skills'] = [{'name': 'ad-maker', 'step': 1}, 'draft']
        config['groups'][1]['excluded_skills'] = ['ad-maker']
        config['installed_skills'] = {'ad-maker': {'credit': 'Someone', 'source': 'someone/skills'}}
        storage.save_catalog(config, storage.revision(server.read_catalog()), self.data)

    def test_missing_entries_listed_with_places(self):
        make_skill(self.agents / 'draft')
        self.stale_catalog()
        missing = server.missing_catalog_entries(server.skills())
        self.assertEqual(missing, [{'kind': 'missing', 'name': 'ad-maker', 'places': ['Writing', 'Installed credits']}])

    def test_remove_stale_entry(self):
        make_skill(self.agents / 'draft')
        self.stale_catalog()
        server.forget_catalog_entry('ad-maker', expected=storage.revision(server.read_catalog()))
        config = server.read_catalog()
        self.assertEqual(config['groups'][0]['skills'], ['draft'])
        self.assertEqual(config['groups'][1]['excluded_skills'], [])
        self.assertEqual(config['installed_skills'], {})
        self.assertEqual(server.missing_catalog_entries(server.skills()), [])

    def test_renamed_skill_takes_over_places(self):
        make_skill(self.agents / 'draft')
        make_skill(self.agents / 'ad-designer')
        self.stale_catalog()
        new = next(item for item in server.skills() if item['name'] == 'ad-designer')
        server.forget_catalog_entry('ad-maker', new['id'], storage.revision(server.read_catalog()))
        config = server.read_catalog()
        self.assertEqual(config['groups'][0]['skills'], [{'name': 'ad-designer', 'step': 1}, 'draft'])
        self.assertEqual(config['groups'][1]['excluded_skills'], ['ad-designer'])
        self.assertEqual(config['installed_skills'], {'ad-designer': {'credit': 'Someone', 'source': 'someone/skills'}})
        entries = server.skills(); server.purpose_groups(entries)
        self.assertEqual(next(item for item in entries if item['name'] == 'ad-designer')['steps'], {'writing': 1})

    def test_replacement_already_in_group_is_not_duplicated(self):
        make_skill(self.agents / 'draft')
        self.stale_catalog()
        draft = next(item for item in server.skills() if item['name'] == 'draft')
        server.forget_catalog_entry('ad-maker', draft['id'], storage.revision(server.read_catalog()))
        self.assertEqual(server.read_catalog()['groups'][0]['skills'], ['draft'])

    def test_refuses_existing_skill_and_stale_revision(self):
        make_skill(self.agents / 'draft')
        self.stale_catalog()
        revision = storage.revision(server.read_catalog())
        with self.assertRaises(ValueError):
            server.forget_catalog_entry('draft', expected=revision)
        with self.assertRaises(storage.ConflictError):
            server.forget_catalog_entry('ad-maker', expected='old')
        with self.assertRaises(ValueError):
            server.forget_catalog_entry('ad-maker', 'no-such-id', revision)
        self.assertIn('ad-maker', server.catalog_references(server.read_catalog()))


if __name__ == '__main__':
    unittest.main()

"""Offline update safety tests; no real installed skills or GitHub writes."""
import copy
import hashlib
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import skill_updates as updates
import storage


class SkillUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / 'skills'
        self.folder = self.root / 'example'
        self.folder.mkdir(parents=True)
        (self.folder / 'SKILL.md').write_text('Original instructions\n', encoding='utf-8')
        (self.folder / 'old.txt').write_text('Remove me\n', encoding='utf-8')
        self.item = dict(id='example', name='example', source='Mine', plugin='', copies=[str(self.folder)],
                         origin=dict(kind='installed', source='owner/repo', skillPath='skills/example/SKILL.md'))
        self.directory = self.base / 'data'
        self.lookup = lambda identifier: self.item
        self.roots = lambda: [(self.root, 'Personal')]
        self.updater = updates.Updater(self.directory, self.roots, self.lookup)
        self.sha = 'a' * 40
        self.files = {'SKILL.md': b'New instructions\n', 'scripts/run.py': b'print("new")\n'}
        self.tree_extra = []
        self.patches = [patch.object(updates, 'github_json', side_effect=self.github),
                        patch.object(updates, 'download', side_effect=self.raw)]
        for patcher in self.patches:
            patcher.start(); self.addCleanup(patcher.stop)

    def github(self, endpoint):
        if endpoint == '/repos/owner/repo':
            return {'default_branch': 'main'}
        if '/commits/' in endpoint:
            return {'sha': self.sha}
        if '/git/trees/' in endpoint:
            return {'tree': [dict(path='skills/example/'+name, mode='100755' if name.endswith('.py') else '100644',
                                 type='blob', size=len(body), sha=hashlib.sha1(b'blob '+str(len(body)).encode()+b'\0'+body).hexdigest())
                             for name, body in self.files.items()] + self.tree_extra}
        raise AssertionError(endpoint)

    def raw(self, url, maximum):
        marker = '/'+self.sha+'/skills/example/'
        return self.files[url.split(marker, 1)[1]]

    def checked(self):
        self.updater.check(self.item)
        token = self.updater.record(self.item)['review']
        return token, self.updater.review(self.item, token)

    def test_review_apply_restore_and_usage_preservation(self):
        (self.folder / 'usage.jsonl').write_text('first\n', encoding='utf-8')
        before = updates.manifest(self.folder)
        token, review = self.checked()
        self.assertTrue(review['firstReview'])
        self.assertEqual({c['kind'] for c in review['changes']}, {'added', 'removed', 'modified'})
        self.assertIn('-Original instructions', review['changes'][0]['diff'])
        self.assertEqual(updates.manifest(self.folder), before)
        # New events after review must survive both update and rollback.
        (self.folder / 'usage.jsonl').write_text('first\nsecond\n', encoding='utf-8')
        self.updater.apply(self.item, token)
        record = self.updater.record(self.item)
        self.assertEqual(record['installedCommit'], self.sha)
        self.assertEqual((self.folder / 'SKILL.md').read_bytes(), self.files['SKILL.md'])
        self.assertFalse((self.folder / 'old.txt').exists())
        self.assertEqual((self.folder / 'usage.jsonl').read_text(), 'first\nsecond\n')
        self.assertEqual(storage.read_json(self.directory/'skill-update-backups'/record['backup']/'transaction.json')['phase'], 'complete')
        if os.name != 'nt':
            self.assertTrue((self.folder / 'scripts/run.py').stat().st_mode & 0o111)
        (self.folder / 'usage.jsonl').write_text('first\nsecond\nthird\n', encoding='utf-8')
        self.updater.restore(self.item, record['backup'])
        self.assertEqual(updates.manifest(self.folder), before)
        self.assertEqual((self.folder / 'usage.jsonl').read_text(), 'first\nsecond\nthird\n')
        self.assertIsNone(self.updater.record(self.item)['baseline'])

    def test_local_name_and_icon_survive_update(self):
        (self.folder / 'agents').mkdir()
        (self.folder / 'agents/openai.yaml').write_text('interface:\n  display_name: "Mine"\n', encoding='utf-8')
        (self.folder / 'assets').mkdir()
        (self.folder / 'assets/flaticon.png').write_bytes(b'icon')
        self.files['agents/openai.yaml'] = b'interface:\n  display_name: "Upstream"\n'
        token, review = self.checked()
        self.assertNotIn('agents/openai.yaml', {c['path'] for c in review['changes']})
        self.assertNotIn('assets/flaticon.png', {c['path'] for c in review['changes']})
        self.updater.apply(self.item, token)
        self.assertEqual((self.folder / 'SKILL.md').read_bytes(), self.files['SKILL.md'])
        self.assertIn('Mine', (self.folder / 'agents/openai.yaml').read_text())
        self.assertEqual((self.folder / 'assets/flaticon.png').read_bytes(), b'icon')

    def test_renamed_update_shows_new_name(self):
        (self.folder / 'SKILL.md').write_text('---\nname: old-name\n---\nOld\n', encoding='utf-8')
        (self.folder / 'agents').mkdir()
        (self.folder / 'agents/openai.yaml').write_text('interface:\n  display_name: "Old"\n  icon_large: "./assets/flaticon.png"\n', encoding='utf-8')
        self.files['SKILL.md'] = b'---\nname: new-name\n---\nNew\n'
        token, _ = self.checked()
        self.updater.apply(self.item, token)
        metadata = (self.folder / 'agents/openai.yaml').read_text()
        self.assertNotIn('display_name', metadata)
        self.assertIn('icon_large', metadata)

    def test_baseline_blocks_local_customizations_without_network(self):
        token, _ = self.checked()
        self.updater.apply(self.item, token)
        (self.folder / 'SKILL.md').write_text('My custom changes', encoding='utf-8')
        with patch.object(updates, 'github_json', side_effect=AssertionError('No network for customized files')):
            self.updater.check(self.item)
        self.assertEqual(self.updater.record(self.item)['status'], 'customized')
        self.assertNotIn('review', self.updater.record(self.item))
        with self.assertRaisesRegex(ValueError, 'Local files changed'):
            self.updater.restore(self.item, self.updater.record(self.item)['backup'])

    def test_modified_after_review_and_modified_download_are_rejected(self):
        token, _ = self.checked()
        (self.folder / 'SKILL.md').write_text('Edit during review', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'Local files changed'):
            self.updater.apply(self.item, token)
        self.assertEqual((self.folder/'SKILL.md').read_text(), 'Edit during review')
        token, _ = self.checked()
        payload = self.directory/'skill-update-reviews'/token/'files/SKILL.md'
        payload.write_text('Tampered', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'downloaded update changed'):
            self.updater.apply(self.item, token)

    def test_no_change_establishes_baseline_and_persists_status(self):
        (self.folder/'old.txt').unlink()
        (self.folder/'SKILL.md').write_bytes(self.files['SKILL.md'])
        del self.files['scripts/run.py']
        self.updater.check(self.item)
        record = self.updater.record(self.item)
        self.assertEqual(record['status'], 'current')
        self.assertIn('lastChecked', record)
        self.assertNotIn('review', record)
        reopened = updates.Updater(self.directory, self.roots, self.lookup)
        self.assertEqual(reopened.describe(self.item)['installedCommit'], self.sha)
        with patch.object(updates, 'download', side_effect=AssertionError('Commit unchanged')):
            reopened.check(self.item)

    def test_repository_change_without_skill_change(self):
        token, _ = self.checked(); self.updater.apply(self.item, token)
        self.sha = 'b'*40
        self.updater.check(self.item)
        self.assertEqual(self.updater.record(self.item)['status'], 'current')
        self.assertEqual(self.updater.record(self.item)['installedCommit'], self.sha)
        self.assertNotIn('review', self.updater.record(self.item))

    def test_source_discovery_ambiguity_and_explicit_override(self):
        self.item['origin'].pop('skillPath')
        self.tree_extra = [dict(path='other/example/SKILL.md', mode='100644', type='blob', sha='b'*40, size=1)]
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            self.updater.check(self.item)
        self.updater.set_source(self.item, 'owner/repo', 'skills/example', 'release/next')
        token, review = self.checked()
        self.assertEqual(review['source']['ref'], 'release/next')
        self.updater.set_source(self.item, 'owner/repo', 'skills/example', 'main')
        with self.assertRaisesRegex(ValueError, 'no longer current'):
            self.updater.review(self.item, token)

    def test_unsafe_remote_paths_modes_and_hashes(self):
        for path in ['../escape', 'C:/file', '/absolute', 'foo\\bar', 'CON.txt', 'a./file']:
            self.tree_extra = [dict(path='skills/example/'+path, type='blob', mode='100644', size=1, sha='b'*40)]
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.updater.check(self.item)
        for mode, kind in [('120000', 'blob'), ('160000', 'commit')]:
            self.tree_extra = [dict(path='skills/example/link', type=kind, mode=mode, size=1, sha='b'*40)]
            with self.subTest(mode=mode), self.assertRaisesRegex(ValueError, 'link or submodule'):
                self.updater.check(self.item)
        self.tree_extra = []
        with patch.object(updates, 'download', return_value=b'wrong'):
            with self.assertRaisesRegex(ValueError, 'did not match'):
                self.updater.check(self.item)
        self.assertEqual((self.folder/'SKILL.md').read_text(), 'Original instructions\n')

    def test_case_conflicting_directories_are_rejected(self):
        self.files['Scripts/other.py'] = b'x'
        with self.assertRaisesRegex(ValueError, 'conflicting file names'):
            self.updater.check(self.item)

    def test_system_external_links_and_dependency_folders_are_protected(self):
        for source in ['System', 'External']:
            item = dict(self.item, source=source)
            with self.assertRaises(ValueError):
                self.updater.targets(item)
        item = dict(self.item, copies=[str(self.base)])
        with self.assertRaises(ValueError):
            self.updater.targets(item)
        token, _ = self.checked()
        (self.folder/'.git').mkdir()
        with self.assertRaisesRegex(ValueError, 'development checkout'):
            self.updater.apply(self.item, token)
        (self.folder/'.git').rmdir()
        if os.name != 'nt':
            (self.folder/'link').symlink_to(self.base)
            with self.assertRaisesRegex(ValueError, 'Linked files'):
                self.updater.check(self.item)

    def test_failed_swap_rolls_back_multiple_copies(self):
        other = self.root/'another'
        shutil.copytree(self.folder, other)
        self.item['copies'].append(str(other))
        token, _ = self.checked()
        before = updates.manifest(self.folder)
        original = os.replace
        calls = []
        def fail_once(source, destination):
            if Path(source).name.startswith('.skill-dashboard-update-'):
                calls.append(source)
                if len(calls) == 2:
                    raise OSError('Simulated failure')
            return original(source, destination)
        with patch.object(updates.os, 'replace', side_effect=fail_once):
            with self.assertRaisesRegex(OSError, 'Simulated failure'):
                self.updater.apply(self.item, token)
        self.assertEqual(updates.manifest(self.folder), before)
        self.assertEqual(updates.manifest(other), before)
        self.assertNotIn('installedCommit', self.updater.record(self.item))

    def test_restart_recovers_swap_before_state_commit(self):
        token, plan = self.checked()
        self.updater.replace(self.item, self.directory/'skill-update-reviews'/token/'files', plan['current'], plan['proposed'])
        self.assertEqual((self.folder/'SKILL.md').read_bytes(), self.files['SKILL.md'])
        updates.Updater(self.directory, self.roots, self.lookup)
        self.assertEqual((self.folder/'SKILL.md').read_text(), 'Original instructions\n')

    def test_async_failure_status_and_customized_refusal(self):
        with patch.object(self.updater, 'remote', side_effect=ValueError('Source unavailable')):
            self.updater.submit('check', [self.item])
            self.updater.thread.join(5)
        self.assertFalse(self.updater.busy)
        self.assertEqual(self.updater.describe(self.item)['status'], 'error')
        self.assertIn('Source unavailable', self.updater.status()['message'])
        self.item['customized'] = True
        self.updater.submit('check', [self.item]); self.updater.thread.join(5)
        self.assertIn('My skills', self.updater.status()['message'])

    def test_failed_state_commit_restores_files_and_prior_baseline(self):
        token, plan = self.checked()
        with patch.object(self.updater, 'save', side_effect=OSError('State disk failure')):
            with self.assertRaisesRegex(OSError, 'State disk failure'):
                self.updater.apply(self.item, token)
        self.assertEqual(updates.manifest(self.folder), plan['current'])
        self.assertNotIn('baseline', self.updater.record(self.item))
        self.updater.apply(self.item, token)
        backup = self.updater.record(self.item)['backup']
        before = updates.manifest(self.folder)
        with patch.object(self.updater, 'save', side_effect=OSError('State disk failure')):
            with self.assertRaisesRegex(OSError, 'State disk failure'):
                self.updater.restore(self.item, backup)
        self.assertEqual(updates.manifest(self.folder), before)
        self.assertEqual(self.updater.record(self.item)['backup'], backup)

    def test_usage_open_before_swap_remains_attached_to_new_folder(self):
        path = self.folder/'usage.jsonl'
        path.write_text('first\n', encoding='utf-8')
        token, _ = self.checked()
        with path.open('a', encoding='utf-8') as stream:
            try:
                self.updater.apply(self.item, token)
            except PermissionError:
                if os.name != 'nt':
                    raise
                self.assertEqual((self.folder/'SKILL.md').read_text(), 'Original instructions\n')
            stream.write('late\n');stream.flush()
        self.assertEqual(path.read_text(), 'first\nlate\n')

    def test_expired_review_returns_to_checkable_status(self):
        token, plan = self.checked()
        path = self.directory/'skill-update-reviews'/token/'review.json'
        plan['created'] = 0
        storage.atomic_json(path, plan)
        with self.assertRaisesRegex(ValueError, 'expired'):
            self.updater.review(self.item, token)
        self.assertNotIn('review', self.updater.describe(self.item))
        self.assertEqual(self.updater.describe(self.item)['status'], 'unchecked')


class InstalledUpdateHttpTests(unittest.TestCase):
    def test_update_routes_require_token_and_validate_skill_and_review(self):
        import json
        import threading
        import urllib.request
        import urllib.error
        import server
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)/'skills'
            folder = root/'example';folder.mkdir(parents=True)
            (folder/'SKILL.md').write_text('Original', encoding='utf-8')
            storage.atomic_json(root.parent/'.skill-lock.json', {'skills': {'example': {'source':'owner/repo', 'sourceType':'github', 'skillPath':'skills/example/SKILL.md'}}})
            storage.atomic_json(Path(directory)/'data/catalog.json', {'version':1, 'groups':[], 'fallback_group':{'id':'other','label':'Other','section':'Skills'}, 'compact_sections':[]})
            with patch.object(server,'DATA_DIR',Path(directory)/'data'), patch.object(server,'ROOTS',[(root,'Personal')]), patch.object(server,'SKILL_UPDATER',None):
                http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
                thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
                url='http://127.0.0.1:'+str(http.server_port)
                def request(payload=None, authorized=True):
                    headers={'Origin':url,'X-Library-Token':server.TOKEN} if authorized else {'Origin':'https://example.com'}
                    req=urllib.request.Request(url+('/api/skill-updates' if payload else '/api/skills'), data=json.dumps(payload).encode() if payload else None, headers=headers)
                    with urllib.request.urlopen(req) as response:return json.load(response)
                try:
                    snapshot=request()
                    self.assertFalse(snapshot['skillUpdates']['busy'])
                    item=snapshot['skills'][0]
                    self.assertEqual(item['skillUpdate']['source']['path'],'skills/example')
                    with self.assertRaises(urllib.error.HTTPError) as error:
                        request({'action':'check-all'},False)
                    self.assertEqual(error.exception.code,403);error.exception.close()
                    for payload in [{'action':'review','id':item['id'],'review':'../escape'}, {'action':'apply','id':item['id']}, {'action':'source','id':item['id'],'repository':'https://evil.test/x','path':''}]:
                        with self.assertRaises(urllib.error.HTTPError) as error:
                            request(payload)
                        self.assertEqual(error.exception.code,400);error.exception.close()
                    state=request({'action':'source','id':item['id'],'repository':'owner/repo','path':'new/path','ref':'release'})
                    self.assertEqual(state['source']['path'],'new/path')
                    self.assertEqual(request()['skills'][0]['skillUpdate']['status'],'unchecked')
                    with patch.object(server.skill_updater(),'remote',side_effect=ValueError('Source missing')):
                        request({'action':'check-all'})
                        server.skill_updater().thread.join(5)
                    self.assertEqual(request()['skills'][0]['skillUpdate']['status'],'error')
                finally:
                    http.shutdown();http.server_close();thread.join()

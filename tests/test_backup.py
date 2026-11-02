"""Back up and restore through a disposable in-memory GitHub API, never an account."""
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import services
import storage


class FakeGitHub:
    def __init__(self):
        self.exists = False
        self.private = True
        self.owner = 'example'
        self.head = None
        self.blobs = {}
        self.trees = {}
        self.commits = {}
        self.calls = []
        self.reject_push = False

    def blob(self, content):
        sha = services.GitHubBackup.blob_sha(content)
        self.blobs[sha] = content
        return sha

    def tree(self, entries):
        entries = copy.deepcopy(entries)
        for entry in entries:
            if 'content' in entry:
                entry['sha'] = self.blob(entry.pop('content').encode('utf-8'))
            entry['size'] = len(self.blobs[entry['sha']])
        sha = hashlib.sha1(json.dumps(entries, sort_keys=True).encode()).hexdigest()
        self.trees[sha] = entries
        return sha

    def api(self, endpoint, payload=None, method=None):
        self.calls.append((endpoint, copy.deepcopy(payload), method))
        if endpoint == 'user': return {'login': 'example'}
        if endpoint == 'user/repos':
            assert payload['private'] is True
            self.exists = True
            tree = self.tree([{'path': 'README.md', 'sha': self.blob(b'Initial README'), 'mode': '100644', 'type': 'blob'}])
            self.head = 'initial'; self.commits[self.head] = {'tree': {'sha': tree}, 'parents': []}
            return self.metadata()
        if endpoint == 'repos/example/codex-skills-backup':
            if not self.exists: raise services.GitHubError(404, 'Not found')
            return self.metadata()
        prefix = 'repos/example/codex-skills-backup/git/'
        if not endpoint.startswith(prefix): raise AssertionError(endpoint)
        route = endpoint[len(prefix):]
        if route == 'ref/heads/main': return {'object': {'sha': self.head}}
        if route.startswith('commits/'):
            return self.commits[route.split('/')[-1]]
        if route.startswith('trees/'):
            sha = route.split('/')[-1].split('?')[0]
            return {'tree': copy.deepcopy(self.trees[sha]), 'truncated': False}
        if route.startswith('blobs/'):
            content = self.blobs[route.split('/')[-1]]
            return {'content': base64.b64encode(content).decode(), 'size': len(content), 'encoding': 'base64'}
        if route == 'blobs': return {'sha': self.blob(base64.b64decode(payload['content']))}
        if route == 'trees': return {'sha': self.tree(payload['tree'])}
        if route == 'commits':
            sha = hashlib.sha1(json.dumps(payload, sort_keys=True).encode()).hexdigest()
            self.commits[sha] = {'tree': {'sha': payload['tree']}, 'parents': payload['parents']}
            return {'sha': sha}
        if route == 'refs/heads/main':
            assert method == 'PATCH' and payload['force'] is False
            if self.reject_push: raise services.GitHubError(409, 'Remote changed')
            assert self.commits[payload['sha']]['parents'] == [self.head]
            self.head = payload['sha']; return {'object': {'sha': self.head}}
        raise AssertionError(endpoint)

    def metadata(self):
        return {'private': self.private, 'owner': {'login': self.owner}, 'default_branch': 'main'}

    def files(self):
        return {e['path']: self.blobs[e['sha']] for e in self.trees[self.commits[self.head]['tree']['sha']]}

    def inject(self, path, content, mode='100644'):
        tree = copy.deepcopy(self.trees[self.commits[self.head]['tree']['sha']])
        tree = [item for item in tree if item['path'] != path]
        tree.append({'path': path, 'sha': self.blob(content), 'mode': mode, 'type': 'blob'})
        self.commits[self.head]['tree']['sha'] = self.tree(tree)


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='github-backup-test ')
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.data = self.base / 'data'
        self.root = self.base / 'skills'
        self.skill = self.root / 'example-skill'; self.skill.mkdir(parents=True)
        (self.skill / 'SKILL.md').write_text('---\nname: example-skill\n---\nOriginal instructions', encoding='utf-8')
        (self.skill / 'scripts').mkdir()
        (self.skill / 'scripts/run.py').write_text('print("example")', encoding='utf-8')
        (self.skill / 'asset.png').write_bytes(b'\x89PNG\0example')
        template = storage.read_json(Path(services.__file__).parent / 'catalog.json')
        storage.atomic_json(self.data / 'catalog.json', template)
        self.backup = services.GitHubBackup(self.base / 'home', self.data, [('codex', self.root), ('agents', self.base / 'agents')])
        self.remote = FakeGitHub()
        self.backup.api = self.remote.api

    def test_private_backup_contains_skills_supporting_files_and_catalog(self):
        self.backup.enable('codex-skills-backup')
        files = self.remote.files()
        self.assertIn('skills/codex/example-skill/SKILL.md', files)
        self.assertIn('skills/codex/example-skill/scripts/run.py', files)
        self.assertEqual(files['skills/codex/example-skill/asset.png'], b'\x89PNG\0example')
        self.assertIn('catalog.json', files)
        self.assertEqual(json.loads(files['backup.json'])['application'], 'skill-dashboard')
        self.assertTrue(self.backup.config()['lastSuccess'])
        self.assertFalse(self.backup.config()['daily'])
        self.assertFalse(any(str(self.base).encode() in content for content in files.values()))

    def test_excludes_builtin_cache_credentials_and_linked_files(self):
        system = self.root / '.system/builtin'; system.mkdir(parents=True)
        (system / 'SKILL.md').write_text('Builtin')
        (self.skill / '.env').write_text('PRIVATE=not-for-backup')
        (self.skill / 'credentials.json').write_text('credential fixture')
        (self.skill / 'usage.jsonl').write_text('usage fixture')
        (self.skill / 'node_modules').mkdir(); (self.skill / 'node_modules/cache').write_text('cache')
        try:
            (self.skill / 'linked').symlink_to(self.base / 'outside')
        except OSError: pass
        files, summary = self.backup.collect()
        self.assertFalse(any('.system' in p or '.env' in p or 'credentials' in p or 'usage.jsonl' in p or 'node_modules' in p or '/linked' in p for p in files))
        self.assertGreaterEqual(summary['excludedFiles'], 3)

    def test_public_repository_cannot_receive_backup(self):
        self.backup.enable('codex-skills-backup')
        self.remote.private = False
        (self.skill / 'SKILL.md').write_text('changed')
        before = len(self.remote.calls)
        with self.assertRaises(ValueError): self.backup.backup()
        self.assertFalse(any(payload is not None for _, payload, _ in self.remote.calls[before:]))

    def test_unchanged_snapshot_creates_no_commit_or_upload(self):
        self.backup.enable('codex-skills-backup')
        head = self.remote.head; before = len(self.remote.calls)
        self.backup.backup()
        self.assertEqual(self.remote.head, head)
        self.assertFalse(any(payload is not None for _, payload, _ in self.remote.calls[before:]))

    def test_push_failure_keeps_last_success(self):
        self.backup.enable('codex-skills-backup')
        success = self.backup.config()['lastSuccess']; head = self.remote.head
        (self.skill / 'SKILL.md').write_text('new version')
        self.remote.reject_push = True
        with self.assertRaises(services.GitHubError): self.backup.backup()
        self.assertEqual(self.remote.head, head)
        self.assertEqual(self.backup.config()['lastSuccess'], success)

    def test_refuses_existing_unrelated_repository(self):
        self.remote.api('user/repos', {'private': True})
        with self.assertRaises(ValueError): self.backup.enable('codex-skills-backup')
        self.assertEqual(self.remote.files(), {'README.md': b'Initial README'})

    def test_restore_missing_files_and_preserve_existing_conflicts(self):
        self.backup.enable('codex-skills-backup')
        asset = self.skill / 'asset.png'; asset.unlink()
        instructions = self.skill / 'SKILL.md'; instructions.write_text('Keep this local edit')
        self.backup.preview('codex-skills-backup')
        preview = self.backup.status()['preview']
        self.assertIn('skills/codex/example-skill/SKILL.md', preview['conflicts'])
        self.backup.restore(preview['id'], overwrite=False)
        self.assertEqual(asset.read_bytes(), b'\x89PNG\0example')
        self.assertEqual(instructions.read_text(), 'Keep this local edit')

    def test_overwrite_keeps_recovery_copy(self):
        self.backup.enable('codex-skills-backup')
        path = self.skill / 'SKILL.md'; original = path.read_bytes(); path.write_text('Local edit')
        self.backup.preview('codex-skills-backup'); preview = self.backup.status()['preview']
        self.backup.restore(preview['id'], overwrite=True)
        self.assertEqual(path.read_bytes(), original)
        recovery = Path(self.backup.status()['recoveryPath']) / 'skills/codex/example-skill/SKILL.md'
        self.assertEqual(recovery.read_text(), 'Local edit')

    def test_restore_on_another_computer_uses_its_own_roots(self):
        self.backup.enable('codex-skills-backup')
        other = services.GitHubBackup(self.base / 'second-home', self.base / 'second-data',
                                      [('codex', self.base / 'second-skills'), ('agents', self.base / 'second-agents')])
        other.api = self.remote.api
        other.preview('codex-skills-backup')
        other.restore(other.status()['preview']['id'])
        self.assertTrue((self.base / 'second-skills/example-skill/SKILL.md').exists())
        self.assertTrue((self.base / 'second-data/catalog.json').exists())

    def test_restore_rejects_files_changed_after_preview(self):
        self.backup.enable('codex-skills-backup')
        self.backup.preview('codex-skills-backup'); preview = self.backup.status()['preview']
        path = self.skill / 'SKILL.md'; path.write_text('Edit after preview')
        with self.assertRaises(ValueError): self.backup.restore(preview['id'], overwrite=True)
        self.assertEqual(path.read_text(), 'Edit after preview')

    def test_restore_rejects_traversal_system_and_symlink_entries(self):
        self.backup.enable('codex-skills-backup')
        for path, mode in [('skills/codex/example-skill/../../outside', '100644'),
                           ('skills/codex/example-skill/.system/SKILL.md', '100644'),
                           ('skills/codex/example-skill/link', '120000')]:
            before = copy.deepcopy(self.remote.commits[self.remote.head])
            self.remote.inject(path, b'bad', mode)
            with self.assertRaises(ValueError): self.backup.preview('codex-skills-backup')
            self.remote.commits[self.remote.head] = before

    def test_daily_preference_only_changes_when_requested(self):
        self.backup.enable('codex-skills-backup')
        self.backup.submit('configure', {'daily': True}); self.backup.thread.join(timeout=3)
        self.assertTrue(self.backup.config()['daily'])
        self.backup.submit('configure', {'daily': False}); self.backup.thread.join(timeout=3)
        self.assertFalse(self.backup.config()['daily'])

    def test_jobs_report_failures_without_claiming_success(self):
        self.backup.submit('backup'); self.backup.thread.join(timeout=3)
        state = self.backup.status()
        self.assertFalse(state['busy']); self.assertTrue(state['error'])
        self.assertIsNone(state['lastSuccess'])

    def test_command_wrapper_uses_json_stdin_and_explicit_method(self):
        import subprocess
        backup = services.GitHubBackup(self.base / 'h', self.data, [])
        with patch.object(backup, 'cli', return_value='/example/gh'), patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '{}', '')) as run:
            backup.api('repos/example/test/git/blobs', {'content': 'fixture'})
            args = run.call_args.args[0]
            self.assertIn('POST', args)
            self.assertEqual(json.loads(run.call_args.kwargs['input']), {'content': 'fixture'})
            self.assertNotIn('fixture', ' '.join(args))

    def test_api_failure_reports_and_logs_safe_diagnostics(self):
        import subprocess
        backup = services.GitHubBackup(self.base / 'h', self.data, [])
        response = subprocess.CompletedProcess([], 1, '{"message":"Validation Failed"}', 'gh: Validation Failed (HTTP 422)')
        with patch.object(backup, 'cli', return_value='/example/gh'), patch.object(subprocess, 'run', return_value=response):
            with self.assertRaisesRegex(services.GitHubError, 'HTTP 422.*Validation Failed'):
                backup.api('repos/example/test/git/trees', {'tree': 'private skill contents'})
        diagnostic = json.loads((self.data / 'github-backup-errors.jsonl').read_text().splitlines()[-1])
        self.assertEqual(diagnostic['status'], 422)
        self.assertEqual(diagnostic['method'], 'POST')
        self.assertEqual(diagnostic['endpoint'], 'repos/example/test/git/trees')
        self.assertNotIn('private skill contents', json.dumps(diagnostic))

    def test_cli_failure_redacts_tokens_even_if_logging_fails(self):
        import subprocess
        backup = services.GitHubBackup(self.base / 'h', self.data, [])
        response = subprocess.CompletedProcess([], 1, '', 'failed with Bearer secret-token ghp_privateToken')
        with patch.object(backup, 'cli', return_value='/example/gh'), patch.object(subprocess, 'run', return_value=response), patch('portable.log_fd', side_effect=OSError('unwritable')):
            with self.assertRaises(services.GitHubError) as raised:
                backup.api('user')
        self.assertIn('[redacted]', str(raised.exception))
        self.assertNotIn('secret-token', str(raised.exception))
        self.assertNotIn('ghp_privateToken', str(raised.exception))

    def test_daily_scheduler_runs_when_due_and_stays_off_otherwise(self):
        import time
        self.backup.enable('codex-skills-backup')
        config = self.backup.config(); config.update(daily=True, lastSuccess='2000-01-01T00:00:00+00:00')
        self.backup.save_config(config)
        before = len(self.remote.calls)
        self.backup.start_scheduler(interval=.01)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and self.backup.config()['lastSuccess'].startswith('2000'):
            time.sleep(.02)
        self.backup.stop_event.set()
        if self.backup.thread: self.backup.thread.join(timeout=3)
        self.assertFalse(self.backup.config()['lastSuccess'].startswith('2000'))
        self.assertGreater(len(self.remote.calls), before)

    def test_restore_refuses_missing_skill_manifest(self):
        self.backup.enable('codex-skills-backup')
        tree = self.remote.trees[self.remote.commits[self.remote.head]['tree']['sha']]
        tree[:] = [item for item in tree if not item['path'].endswith('/SKILL.md')]
        with self.assertRaises(ValueError): self.backup.preview('codex-skills-backup')

    def test_connect_reuses_cli_login_without_uploading_anything(self):
        import subprocess
        with patch.object(self.backup, 'ensure_cli', return_value='/fixture/gh'), \
             patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)), \
             patch.object(subprocess, 'Popen') as login:
            self.backup.connect()
        login.assert_not_called()
        self.assertEqual(self.backup.config()['account'], 'example')
        self.assertFalse(self.backup.config().get('enabled', False))
        self.assertFalse(any(payload is not None for _, payload, _ in self.remote.calls))

    def test_device_code_is_shown_temporarily_and_not_saved(self):
        import io
        import subprocess
        from unittest.mock import Mock
        process = Mock()
        process.stdin = io.StringIO()
        process.stdout = io.StringIO('First copy your one-time code: ABCD-1234\nOpen https://github.com/login/device\n')
        process.wait.return_value = 0
        process.poll.return_value = 0
        reports = []
        original = self.backup.report
        def report(**values):
            reports.append(values)
            original(**values)
            saved = (self.data / 'github-backup-status.json').read_text()
            self.assertNotIn('ABCD-1234', saved)
        with patch.object(self.backup, 'report', side_effect=report), \
             patch.object(self.backup, 'ensure_cli', return_value='/fixture/gh'), \
             patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess([], 1)), \
             patch.object(subprocess, 'Popen', return_value=process):
            self.backup.connect()
        self.assertTrue(any(v.get('authCode') == 'ABCD-1234' for v in reports))
        self.assertIsNone(self.backup.status().get('authCode'))
        self.assertFalse(self.backup.config().get('enabled', False))

    def test_binary_install_accepts_root_bin_layout_without_other_extraction(self):
        import io
        import zipfile
        import updates
        import urllib.request
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w') as bundle:
            bundle.writestr('bin/' + ('gh.exe' if os.name == 'nt' else 'gh'), b'fixture executable')
            bundle.writestr('../outside', b'do not extract')
        archive.seek(0)
        release = {'assets': [{'name': 'gh_1_' + ('windows' if os.name == 'nt' else 'macOS') + '_arm64.zip', 'browser_download_url': 'https://github.com/cli/cli/releases/download/v1/gh.zip'}]}
        with patch.object(self.backup, 'cli', return_value=None), patch.object(services.sys, 'platform', 'win32' if os.name == 'nt' else 'darwin'), \
             patch('platform.machine', return_value='arm64'), patch.object(updates, 'fetch_json', return_value=release), \
             patch.object(urllib.request, 'urlopen', return_value=archive):
            installed = Path(self.backup.ensure_cli())
        self.assertEqual(installed.read_bytes(), b'fixture executable')
        self.assertFalse((self.base / 'outside').exists())

    def test_large_files_fail_before_uploading_any_content(self):
        with patch.object(self.backup, 'MAX_FILE', 10):
            with self.assertRaises(ValueError): self.backup.collect()
        self.assertFalse(any(payload is not None for _, payload, _ in self.remote.calls))

    @unittest.skipIf(os.name == 'nt', 'POSIX file permissions')
    def test_restore_preserves_private_file_permissions(self):
        path = self.skill / 'SKILL.md'; path.chmod(0o600)
        self.backup.enable('codex-skills-backup')
        path.unlink()
        self.backup.preview('codex-skills-backup')
        self.backup.restore(self.backup.status()['preview']['id'])
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_linked_personal_skill_is_copied_but_builtin_link_is_excluded(self):
        outside = self.base / 'source-skill'; outside.mkdir()
        (outside / 'SKILL.md').write_text('Linked personal skill')
        (outside / 'reference.txt').write_text('Reference')
        builtin = self.root / '.system/private'; builtin.mkdir(parents=True)
        (builtin / 'SKILL.md').write_text('Builtin skill')
        try:
            (self.root / 'linked-personal').symlink_to(outside, target_is_directory=True)
            (self.root / 'linked-builtin').symlink_to(builtin, target_is_directory=True)
        except OSError:
            self.skipTest('Symlinks are unavailable to this user.')
        files, _ = self.backup.collect()
        self.assertEqual(files['skills/codex/linked-personal/SKILL.md'][0], b'Linked personal skill')
        self.assertIn('skills/codex/linked-personal/reference.txt', files)
        self.assertFalse(any('linked-builtin' in path for path in files))

    def test_restore_never_overwrites_a_linked_destination(self):
        self.backup.enable('codex-skills-backup')
        outside = self.base / 'outside'; outside.write_text('untouched')
        asset = self.skill / 'asset.png'; asset.unlink()
        try:
            asset.symlink_to(outside)
        except OSError:
            self.skipTest('Symlinks are unavailable to this user.')
        with self.assertRaises(ValueError): self.backup.preview('codex-skills-backup')
        self.assertEqual(outside.read_text(), 'untouched')

    def test_text_is_batched_while_binary_files_use_blob_uploads(self):
        self.backup.enable('codex-skills-backup')
        tree_calls = [payload for endpoint, payload, _ in self.remote.calls if endpoint.endswith('/git/trees') and payload]
        self.assertEqual(len(tree_calls), 1)
        entries = {item['path']: item for item in tree_calls[0]['tree']}
        self.assertIn('content', entries['skills/codex/example-skill/SKILL.md'])
        self.assertNotIn('sha', entries['skills/codex/example-skill/SKILL.md'])
        self.assertIn('sha', entries['skills/codex/example-skill/asset.png'])
        self.assertNotIn('content', entries['skills/codex/example-skill/asset.png'])

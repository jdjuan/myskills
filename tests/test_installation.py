import io
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import dashboard
import services
import storage
import updates


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='dashboard space ')
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.data = self.home / 'data'

    def test_local_install_launcher_and_repeat_release_install(self):
        dashboard.install(self.home, source=dashboard.HERE, start=False)
        result = subprocess.check_output([sys.executable, str(self.home / 'launcher.py'), 'status'], text=True)
        self.assertEqual(json.loads(result)['version'], (dashboard.HERE / 'VERSION').read_text().strip())
        original = storage.catalog(self.data); original['groups'] = [{'id': 'own', 'label': 'Own', 'section': 'Own', 'skills': []}]
        storage.atomic_json(self.data / 'catalog.json', original)
        with patch.object(updates, 'latest_release', return_value=('0.1.0', 'unused')):
            dashboard.install(self.home, start=False)
        self.assertEqual(storage.catalog(self.data)['groups'][0]['id'], 'own')

    def test_migration_copies_original_without_modifying_it(self):
        checkout = self.home / 'old'; checkout.mkdir()
        original = storage.read_json(dashboard.HERE / 'catalog.json')
        original['groups'] = [{'id': 'mine', 'label': 'Mine', 'section': 'Mine', 'skills': []}]
        storage.atomic_json(checkout / 'catalog.json', original)
        dashboard.install(self.home, source=dashboard.HERE, migrate=checkout, start=False)
        self.assertEqual(storage.catalog(self.data), original)
        self.assertEqual(storage.read_json(checkout / 'catalog.json'), original)
        self.assertTrue((self.data / 'catalog.before-migration.json').exists())

    def test_mac_service_serializes_absolute_paths(self):
        (self.home / 'python-path.txt').write_text(sys.executable)
        value = plistlib.loads(plistlib.dumps(services.mac_config(self.home)))
        self.assertTrue(value['KeepAlive'])
        self.assertEqual(value['ProgramArguments'][1], str(self.home / 'launcher.py'))
        self.assertEqual(value['WorkingDirectory'], str(self.home))

    def test_windows_task_quotes_paths_and_uses_interactive_user(self):
        (self.home / 'python-path.txt').write_text('C:\\Program Files\\Python\\python.exe')
        with patch.object(services.subprocess, 'check_output', return_value='"Example\\User","S-1-5-21-1000"'):
            xml = services.windows_xml(self.home)
        import xml.etree.ElementTree as ET
        root = ET.fromstring(xml)
        ns = {'t': 'http://schemas.microsoft.com/windows/2004/02/mit/task'}
        self.assertEqual(root.find('.//t:LogonType', ns).text, 'InteractiveToken')
        self.assertEqual(root.find('.//t:RunLevel', ns).text, 'LeastPrivilege')
        self.assertIn('"', root.find('.//t:Arguments', ns).text)

    def test_usage_instruction_has_stable_launcher(self):
        (self.home / 'python-path.txt').write_text(sys.executable)
        text = dashboard.usage_instruction(self.home)
        self.assertIn(str(self.home / 'launcher.py'), text)
        self.assertIn('SKILL_DASHBOARD_USAGE_START', text)
        self.assertNotIn('releases/', text)


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.data = self.home / 'data'
        storage.atomic_json(self.home / 'current.json', {'version': '0.1.0'})
        storage.atomic_json(self.data / 'catalog.json', {'private': 'untouched'})

    def archive(self, extra=None):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w') as bundle:
            for name in updates.FILES: bundle.writestr(name, name)
            if extra: bundle.writestr(extra, 'bad')
        archive.seek(0)
        return archive

    def test_download_and_check_do_not_switch_before_restart(self):
        with patch.object(updates, 'latest_release', return_value=('0.2.0', 'https://example.invalid/app.zip')), \
             patch.object(updates.urllib.request, 'urlopen', return_value=self.archive()):
            self.assertEqual(updates.check_for_update(self.home, self.data), '0.2.0')
        self.assertTrue((self.home / 'releases/0.2.0/server.py').exists())
        self.assertEqual(storage.read_json(self.home / 'current.json')['version'], '0.1.0')
        self.assertEqual(storage.read_json(self.data / 'catalog.json'), {'private': 'untouched'})

    def test_offline_check_preserves_running_version(self):
        with patch.object(updates, 'latest_release', side_effect=OSError('offline')):
            self.assertIsNone(updates.check_for_update(self.home, self.data))
        self.assertEqual(storage.read_json(self.home / 'current.json')['version'], '0.1.0')
        self.assertIn('offline', updates.status(self.data, self.home)['error'])

    def test_same_version_never_downloads(self):
        with patch.object(updates, 'latest_release', return_value=('0.1.0', 'unused')), patch.object(updates, 'download_release') as download:
            self.assertIsNone(updates.check_for_update(self.home, self.data))
            download.assert_not_called()

    def test_incomplete_download_and_paths_cannot_change_active_files(self):
        for archive in [io.BytesIO(b'broken download'), self.archive('../catalog.json')]:
            with patch.object(updates.urllib.request, 'urlopen', return_value=archive):
                with self.assertRaises((ValueError, zipfile.BadZipFile)):
                    updates.download_release(self.home, '0.2.0', 'https://example.invalid/app.zip')
            self.assertFalse((self.home / 'releases/0.2.0').exists())
        self.assertEqual(storage.read_json(self.data / 'catalog.json'), {'private': 'untouched'})

    def test_latest_filters_unpublished_and_wrong_assets(self):
        for release in [{'draft': True}, {'prerelease': True}, {'tag_name': 'v0.2.0', 'assets': []}]:
            with patch.object(updates, 'fetch_json', return_value=release):
                with self.assertRaises(ValueError): updates.latest_release()

    def test_packaged_release_contains_only_application(self):
        archive = self.home / 'app.zip'
        subprocess.run([sys.executable, str(dashboard.HERE / 'scripts/package.py'), str(archive)], check=True, capture_output=True)
        with zipfile.ZipFile(archive) as bundle:
            self.assertEqual(set(bundle.namelist()), updates.FILES)
            self.assertEqual(json.loads(bundle.read('catalog.json'))['groups'], [])

class SupervisorTests(unittest.TestCase):
    def test_release_switch_restart_and_catalog_survival(self):
        """A real child server updates from a local release fixture, never GitHub."""
        import contextlib
        from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
        import functools
        import signal
        import socket
        import threading
        import time
        import urllib.request
        with tempfile.TemporaryDirectory(prefix='dashboard lifecycle ') as temporary:
            home = Path(temporary).resolve() / 'installation'
            dashboard.install(home, source=dashboard.HERE, start=False)
            original = storage.catalog(home / 'data')
            original['groups'] = [{'id': 'custom', 'label': 'Custom', 'section': 'Personal', 'skills': ['fixture']}]
            storage.atomic_json(home / 'data/catalog.json', original)
            # Occupy the requested port to exercise fallback without touching any real listener.
            occupied = socket.socket(); occupied.bind(('127.0.0.1', 0)); occupied.listen()
            self.addCleanup(occupied.close)
            storage.atomic_json(home / 'data/settings.json', {'autoUpdate': False, 'port': occupied.getsockname()[1]})
            assets = home.parent / 'assets'; assets.mkdir()
            with zipfile.ZipFile(assets / 'app.zip', 'w', zipfile.ZIP_DEFLATED) as archive:
                for name in updates.FILES:
                    archive.writestr(name, b'0.2.0\n' if name == 'VERSION' else (dashboard.HERE / name).read_bytes())
            class QuietHandler(SimpleHTTPRequestHandler):
                def log_message(self, *_): pass
            fixture = ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(QuietHandler, directory=str(assets)))
            thread = threading.Thread(target=fixture.serve_forever, daemon=True); thread.start()
            url = f'http://127.0.0.1:{fixture.server_port}/app.zip'
            code = ('import sys; from pathlib import Path; import dashboard, updates; '
                    "updates.latest_release=lambda:('0.2.0',sys.argv[2]); "
                    'dashboard.supervise(Path(sys.argv[1]))')
            env = {**os.environ, 'SKILL_DASHBOARD_HOME': str(home)}
            process = subprocess.Popen([sys.executable, '-c', code, str(home), url], cwd=dashboard.HERE, env=env,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            last_pid = None
            def wait_for(version):
                nonlocal last_pid
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise AssertionError(process.stderr.read().decode())
                    state = storage.read_json(home / 'data/server.json', {})
                    current = storage.read_json(home / 'current.json', {})
                    if state and current.get('version') == version:
                        try:
                            with urllib.request.urlopen(state['url'] + '/api/health', timeout=1) as response:
                                health = json.load(response)
                            if health['pid'] == state['pid'] and (version == '0.1.0' or state['pid'] != last_pid):
                                last_pid = state['pid']
                                return state
                        except OSError: pass
                    time.sleep(.1)
                raise AssertionError('Supervisor did not serve version ' + version)
            try:
                first = wait_for('0.1.0')
                self.assertNotEqual(int(first['url'].rsplit(':', 1)[1]), occupied.getsockname()[1])
                storage.atomic_json(home / 'data/update-request.json', {'requestedAt': updates.now()})
                second = wait_for('0.2.0')
                self.assertNotEqual(first['pid'], second['pid'])
                self.assertEqual(storage.catalog(home / 'data'), original)
                self.assertEqual(storage.read_json(home / 'current.json')['previous'], '0.1.0')
                self.assertFalse((home / 'data/update-request.json').exists())
            finally:
                process.terminate()
                try: process.wait(timeout=15)
                except subprocess.TimeoutExpired: process.kill(); process.wait()
                process.stderr.close()
                if os.name == 'nt' and last_pid:
                    with contextlib.suppress(OSError): os.kill(last_pid, signal.SIGTERM)
                fixture.shutdown(); fixture.server_close(); thread.join()

class RemovalTests(unittest.TestCase):
    def test_uninstall_retains_personal_data_and_external_skills(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / 'installation'
            dashboard.install(home, source=dashboard.HERE, start=False)
            skill = Path(temporary) / 'skills/example/SKILL.md'
            skill.parent.mkdir(parents=True); skill.write_text('untouched')
            original = storage.catalog(home / 'data')
            with patch.object(services, 'stop') as stop:
                if os.name != 'nt':
                    dashboard.uninstall(home)
                    self.assertFalse((home / 'releases').exists())
                    self.assertFalse((home / 'launcher.py').exists())
                else:
                    with patch.object(subprocess, 'Popen') as deferred:
                        dashboard.uninstall(home)
                        self.assertIn('-EncodedCommand', deferred.call_args.args[0])
                stop.assert_called_once_with(remove=True)
            self.assertEqual(storage.catalog(home / 'data'), original)
            self.assertEqual(skill.read_text(), 'untouched')

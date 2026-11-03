"""Search launchers are tested only in disposable directories."""
import json
import os
from pathlib import Path
import plistlib
import tempfile
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

import dashboard
import services
import storage


class ShortcutTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="skills launcher's ")
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name).resolve() / 'dashboard'
        self.home.mkdir()
        (self.home / 'launcher.py').write_text('fixture', encoding='utf-8')
        (self.home / 'python-path.txt').write_text('/a runtime/python', encoding='utf-8')
        self.app = self.home.parent / 'Applications/Skills.app'

    @unittest.skipIf(os.name == 'nt', 'macOS shell syntax test')
    def test_app_uses_stable_command_and_survives_runtime_change(self):
        with patch.object(services.sys, 'platform', 'darwin'):
            state = services.set_shortcut(self.home, True, path=self.app, register=False)
            self.assertTrue(state['installed'])
            info = plistlib.loads((self.app / 'Contents/Info.plist').read_bytes())
            self.assertEqual(info['CFBundleName'], 'Skills')
            script = (self.app / 'Contents/MacOS/launcher').read_text()
            self.assertIn('python-path.txt', script)
            self.assertIn('launcher.py', script)
            self.assertIn('open --home', script)
            (self.home / 'python-path.txt').write_text('/new runtime/python')
            self.assertTrue(services.shortcut_status(self.home, self.app)['installed'])
            self.assertTrue(services.set_shortcut(self.home, True, self.app, register=False)['installed'])
            services.set_shortcut(self.home, False, self.app, register=False)
            self.assertFalse(self.app.exists())

    def test_unrelated_app_and_shortcut_are_never_replaced_or_removed(self):
        self.app.mkdir(parents=True)
        marker = self.app / 'important'; marker.write_text('untouched')
        with patch.object(services.sys, 'platform', 'darwin'):
            for enabled in (True, False):
                with self.assertRaises(ValueError): services.set_shortcut(self.home, enabled, self.app, register=False)
        self.assertEqual(marker.read_text(), 'untouched')

    @unittest.skipIf(os.name == 'nt', 'macOS app ownership test')
    def test_other_installation_cannot_claim_launcher(self):
        with patch.object(services.sys, 'platform', 'darwin'):
            services.set_shortcut(self.home, True, self.app, register=False)
            other = self.home.parent / 'other'; other.mkdir()
            (other / 'python-path.txt').write_text('/other/python')
            self.assertTrue(services.shortcut_status(other, self.app)['conflict'])
            with self.assertRaises(ValueError): services.set_shortcut(other, False, self.app, register=False)
            self.assertTrue(self.app.exists())

    def test_choice_preserves_other_preferences(self):
        data = self.home / 'data'
        storage.atomic_json(data / 'settings.json', {'autoUpdate': False, 'port': 9000})
        services.save_shortcut_choice(self.home, False)
        self.assertEqual(storage.read_json(data / 'settings.json'), {'autoUpdate': False, 'port': 9000, 'launcherEnabled': False})

    def test_reinstall_respects_removed_launcher(self):
        (self.home / 'data').mkdir()
        storage.atomic_json(self.home / 'data/settings.json', {'launcherEnabled': False})
        with patch.object(services, 'configure_shortcut') as create:
            dashboard.install(self.home, source=dashboard.HERE, start=False)
            create.assert_not_called()

    def test_new_install_creates_launcher_by_default(self):
        with patch.object(services, 'configure_shortcut') as create:
            dashboard.install(self.home, source=dashboard.HERE, start=False)
            create.assert_called_once_with(self.home, True, self.home / 'data')

    def test_conflict_does_not_break_installation(self):
        with patch.object(services, 'configure_shortcut', side_effect=ValueError('Name conflict')):
            dashboard.install(self.home, source=dashboard.HERE, start=False)
        self.assertTrue((self.home / 'current.json').exists())

    def test_windows_command_fits_shortcut_argument_limit(self):
        self.assertLessEqual(len(services._windows_shortcut_arguments()), 1023)

    @unittest.skipUnless(os.name == 'nt', 'Native Windows launcher execution')
    def test_native_windows_command_launches_from_its_working_directory(self):
        home = self.home / 'spaces [brackets] café'
        home.mkdir()
        (home / 'python-path.txt').write_text(sys.executable, encoding='utf-8')
        (home / 'launcher.py').write_text(
            "import json, pathlib, sys\n"
            "pathlib.Path('launch-result.json').write_text(json.dumps(sys.argv[1:]))\n",
            encoding='utf-8')
        completed = subprocess.run(['powershell', *services._windows_shortcut_arguments().split()],
                                   cwd=home, timeout=30, capture_output=True, text=True, errors='replace')
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = home / 'launch-result.json'
        deadline = time.monotonic() + 10
        while not result.exists() and time.monotonic() < deadline:
            time.sleep(.1)
        self.assertTrue(result.is_file(), 'The launcher must actually execute Python.')
        self.assertEqual(json.loads(result.read_text()), ['open', '--home', str(home)])

    @unittest.skipUnless(os.name == 'nt', 'Native Windows .lnk test')
    def test_native_windows_link_uses_stable_home_and_removes_cleanly(self):
        target = self.home.parent / 'Start Menu/Skills.lnk'
        replies = []
        powershell = services._powershell
        def capture(script):
            result = powershell(script)
            replies.append(json.loads(result.lstrip('\ufeff')))
            return result
        with patch.object(services, '_powershell', side_effect=capture):
            state = services.set_shortcut(self.home, True, target)
        self.assertTrue(state['installed'], {'state': state, 'shortcutProperties': replies})
        (self.home / 'python-path.txt').write_text('C:\\new runtime\\python.exe')
        self.assertTrue(services.shortcut_status(self.home, target)['installed'])
        # Another working directory would execute a different launcher: refuse it.
        services._powershell(f"$p={services._ps_text(target)}; $s=(New-Object -ComObject WScript.Shell).CreateShortcut($p); "
                             f"$s.WorkingDirectory={services._ps_text(self.home.parent)}; $s.Save()")
        self.assertTrue(services.shortcut_status(self.home, target)['conflict'])
        with self.assertRaises(ValueError):
            services.set_shortcut(self.home, False, target)
        services._powershell(f"$p={services._ps_text(target)}; $s=(New-Object -ComObject WScript.Shell).CreateShortcut($p); "
                             f"$s.WorkingDirectory={services._ps_text(self.home)}; $s.Save()")
        services.set_shortcut(self.home, False, target)
        self.assertFalse(target.exists())

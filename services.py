"""Per-user login startup. No administrator permissions or shell profiles."""
import os
from pathlib import Path
import plistlib
import subprocess
import sys
from xml.sax.saxutils import escape

LABEL = 'io.skilldashboard.server'
LEGACY_LABEL = 'io.jdjuan.skills-library'
TASK = 'SkillDashboard'


def service_command(home):
    home = Path(home)
    python = (home / 'python-path.txt').read_text(encoding='utf-8').strip()
    if sys.platform == 'win32':
        pythonw = Path(python).with_name('pythonw.exe')
        if pythonw.exists():
            python = str(pythonw)
    return [python, str(home / 'launcher.py'), 'supervise', '--home', str(home)]


def mac_config(home):
    home = Path(home)
    return {'Label': LABEL, 'ProgramArguments': service_command(home),
            'WorkingDirectory': str(home), 'RunAtLoad': True, 'KeepAlive': True,
            'ThrottleInterval': 10,
            'EnvironmentVariables': {'PYTHONUNBUFFERED': '1'},
            'StandardOutPath': str(home / 'logs/service.log'),
            'StandardErrorPath': str(home / 'logs/error.log')}


def windows_xml(home):
    command = service_command(home)
    account = subprocess.check_output(['whoami', '/user', '/fo', 'csv', '/nh'], text=True).strip()
    import csv
    sid = next(csv.reader([account]))[1]
    return f'''<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
<Triggers><LogonTrigger><Enabled>true</Enabled><UserId>{escape(sid)}</UserId></LogonTrigger></Triggers>
<Principals><Principal id="Author"><UserId>{escape(sid)}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
<Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><ExecutionTimeLimit>PT0S</ExecutionTimeLimit><RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure></Settings>
<Actions Context="Author"><Exec><Command>{escape(command[0])}</Command><Arguments>{escape(subprocess.list2cmdline(command[1:]))}</Arguments><WorkingDirectory>{escape(str(home))}</WorkingDirectory></Exec></Actions>
</Task>'''


def start(home):
    home = Path(home)
    (home / 'logs').mkdir(parents=True, exist_ok=True)
    if sys.platform == 'darwin':
        path = Path.home() / 'Library/LaunchAgents' / (LABEL + '.plist')
        path.parent.mkdir(parents=True, exist_ok=True)
        previous = path.read_bytes() if path.exists() else None
        if previous is not None:
            path.with_suffix('.plist.backup').write_bytes(previous)
        config = plistlib.dumps(mac_config(home))
        subprocess.run(['launchctl', 'bootout', f'gui/{os.getuid()}/{LABEL}'], capture_output=True)
        try:
            path.write_bytes(config)
            subprocess.run(['plutil', '-lint', str(path)], check=True, capture_output=True)
            try:
                subprocess.run(['launchctl', 'bootstrap', f'gui/{os.getuid()}', str(path)], check=True, capture_output=True)
            except subprocess.CalledProcessError:
                # bootout can complete asynchronously.
                import time
                time.sleep(1)
                subprocess.run(['launchctl', 'bootstrap', f'gui/{os.getuid()}', str(path)], check=True, capture_output=True)
        except Exception:
            subprocess.run(['launchctl', 'bootout', f'gui/{os.getuid()}/{LABEL}'], capture_output=True)
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(previous)
                subprocess.run(['launchctl', 'bootstrap', f'gui/{os.getuid()}', str(path)], capture_output=True)
            raise
    elif sys.platform == 'win32':
        xml = home / 'task.xml'
        xml.write_text(windows_xml(home), encoding='utf-16')
        try:
            subprocess.run(['schtasks', '/End', '/TN', TASK], capture_output=True)
            subprocess.run(['schtasks', '/Create', '/TN', TASK, '/XML', str(xml), '/F'], check=True, capture_output=True)
            subprocess.run(['schtasks', '/Run', '/TN', TASK], check=True, capture_output=True)
        finally:
            xml.unlink(missing_ok=True)
    else:
        raise ValueError('Login startup is supported on macOS and Windows.')


def stop(remove=False):
    if sys.platform == 'darwin':
        subprocess.run(['launchctl', 'bootout', f'gui/{os.getuid()}/{LABEL}'], capture_output=True)
        if remove:
            (Path.home() / 'Library/LaunchAgents' / (LABEL + '.plist')).unlink(missing_ok=True)
    elif sys.platform == 'win32':
        subprocess.run(['schtasks', '/End', '/TN', TASK], capture_output=True)
        if remove:
            subprocess.run(['schtasks', '/Delete', '/TN', TASK, '/F'], capture_output=True)


def remove_legacy(checkout):
    if sys.platform != 'darwin':
        return
    path = Path.home() / 'Library/LaunchAgents' / (LEGACY_LABEL + '.plist')
    if not path.exists():
        return
    config = plistlib.loads(path.read_bytes())
    if str(Path(checkout).resolve() / 'server.py') not in config.get('ProgramArguments', []):
        raise ValueError('Legacy service points to another checkout; it was left unchanged.')
    path.with_suffix('.plist.backup').write_bytes(path.read_bytes())
    subprocess.run(['launchctl', 'bootout', f'gui/{os.getuid()}/{LEGACY_LABEL}'], capture_output=True)
    path.unlink()


# Search launchers live outside application releases and always call the stable launcher.
def shortcut_path():
    if sys.platform == 'darwin':
        return Path.home() / 'Applications/Skills.app'
    if sys.platform == 'win32':
        return Path(os.environ.get('APPDATA', str(Path.home() / 'AppData/Roaming'))) / 'Microsoft/Windows/Start Menu/Programs/Skills.lnk'
    return None


def _powershell(script):
    import base64
    encoded = base64.b64encode(script.encode('utf-16le')).decode()
    return subprocess.check_output(['powershell', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
                                   text=True, encoding='utf-8', creationflags=0x08000000 if os.name == 'nt' else 0).strip()


def _ps_text(value):
    import base64
    return "[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('" + base64.b64encode(str(value).encode()).decode() + "'))"


def _windows_shortcut_arguments():
    """Keep the encoded command below WScript.Shell's 1,023-character limit."""
    import base64
    # The shortcut's verified WorkingDirectory supplies the installation path.
    # Embedding that path in an already encoded command can exceed the limit.
    launch_script = ("$ErrorActionPreference='Stop'; $h=[Environment]::CurrentDirectory; Set-Location -LiteralPath $h; "
                     "$py=(Get-Content -LiteralPath (Join-Path $h 'python-path.txt') -Raw -Encoding UTF8).Trim(); "
                     "$gui=Join-Path (Split-Path $py) 'pythonw.exe'; if(Test-Path -LiteralPath $gui){$py=$gui}; "
                     "& $py (Join-Path $h 'launcher.py') open --home $h")
    encoded = base64.b64encode(launch_script.encode('utf-16le')).decode()
    return subprocess.list2cmdline(['-NoProfile', '-NonInteractive', '-WindowStyle', 'Hidden', '-EncodedCommand', encoded])


def _windows_shortcut(home, path, write=False):
    """Use Windows' native .lnk format without another Python dependency."""
    import json
    home = Path(home).resolve()
    executable = str(Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe')
    args = _windows_shortcut_arguments()
    description = 'SkillDashboard:' + str(home)
    script = ("$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[Text.Encoding]::UTF8; "
              f"$p={_ps_text(path)}; $s=(New-Object -ComObject WScript.Shell).CreateShortcut($p); ")
    if write:
        script += (f"$s.TargetPath={_ps_text(executable)}; $s.Arguments={_ps_text(args)}; "
                   f"$s.WorkingDirectory={_ps_text(home)}; $s.Description={_ps_text(description)}; "
                   '$s.Save(); ')
    script += ('@{target=$s.TargetPath;arguments=$s.Arguments;description=$s.Description;'
               'workingDirectory=$s.WorkingDirectory}|ConvertTo-Json -Compress')
    actual = json.loads(_powershell(script).lstrip('\ufeff'))
    return (os.path.normcase(actual['target']) == os.path.normcase(executable) and
            actual['arguments'] == args and actual['description'] == description and
            os.path.normcase(actual['workingDirectory']) == os.path.normcase(str(home)))



def _mac_shortcut_script(home):
    import shlex
    home = Path(home).resolve()
    return ('#!/bin/sh\nDASH_PYTHON=$(/bin/cat ' + shlex.quote(str(home / 'python-path.txt')) + ')\n'
            + 'exec "$DASH_PYTHON" ' + ' '.join(shlex.quote(part) for part in
                [str(home / 'launcher.py'), 'open', '--home', str(home)]) + '\n')



def shortcut_status(home, path=None):
    from portable import linked
    home = Path(home).resolve()
    target = Path(path) if path is not None else shortcut_path()
    state = {'supported': target is not None, 'installed': False, 'conflict': False,
             'path': str(target) if target is not None else None,
             'label': 'Spotlight launcher' if sys.platform == 'darwin' else 'Start menu shortcut'}
    if target is None or not (target.exists() or target.is_symlink()):
        return state
    try:
        if linked(target):
            raise ValueError('Linked shortcut location')
        if sys.platform == 'darwin':
            info_path = target / 'Contents/Info.plist'
            script_path = target / 'Contents/MacOS/launcher'
            # Never follow linked files inside another app to classify it as ours.
            if any(linked(p) for p in (target / 'Contents', target / 'Contents/MacOS', info_path, script_path)):
                raise ValueError('Linked application contents')
            info = plistlib.loads(info_path.read_bytes())
            owned = (info.get('SkillDashboardManagedBy') == LABEL and
                     info.get('SkillDashboardHome') == str(home) and
                     script_path.read_text(encoding='utf-8') == _mac_shortcut_script(home))
        else:
            owned = _windows_shortcut(home, target)
        state['installed'] = owned
        state['conflict'] = not owned
    except (OSError, ValueError, KeyError, subprocess.SubprocessError, plistlib.InvalidFileException):
        state['conflict'] = True
    return state


def set_shortcut(home, enabled, path=None, register=True):
    import shutil
    import tempfile
    home = Path(home).resolve()
    state = shortcut_status(home, path)
    if not state['supported']:
        raise ValueError('Search launchers are supported on macOS and Windows.')
    if state['conflict']:
        raise ValueError('A different Skills app or shortcut already exists. It was left unchanged: ' + state['path'])
    target = Path(state['path'])
    if enabled:
        if not state['installed']:
            target.parent.mkdir(parents=True, exist_ok=True)
            if sys.platform == 'darwin':
                with tempfile.TemporaryDirectory(prefix='.skills-launcher-', dir=target.parent) as temporary:
                    bundle = Path(temporary) / 'Skills.app'
                    executable = bundle / 'Contents/MacOS/launcher'
                    executable.parent.mkdir(parents=True)
                    info = {'CFBundleName': 'Skills', 'CFBundleDisplayName': 'Skills',
                            'CFBundleIdentifier': 'io.skilldashboard.launcher',
                            'CFBundlePackageType': 'APPL', 'CFBundleExecutable': 'launcher',
                            'CFBundleVersion': '1', 'CFBundleShortVersionString': '1.0', 'LSUIElement': True,
                            'SkillDashboardManagedBy': LABEL, 'SkillDashboardHome': str(home)}
                    (bundle / 'Contents/Info.plist').write_bytes(plistlib.dumps(info))
                    executable.write_text(_mac_shortcut_script(home), encoding='utf-8')
                    executable.chmod(0o755)
                    subprocess.run(['/bin/sh', '-n', str(executable)], check=True)
                    if target.exists() or target.is_symlink():
                        raise ValueError('The Skills app location changed. It was left unchanged.')
                    bundle.rename(target)
            else:
                # Create into a temporary file, then publish without replacing an existing shortcut.
                fd, name = tempfile.mkstemp(suffix='.lnk', dir=target.parent)
                os.close(fd)
                temporary = Path(name)
                temporary.unlink()
                try:
                    _windows_shortcut(home, temporary, write=True)
                    if target.exists() or target.is_symlink():
                        raise ValueError('The Skills shortcut location changed. It was left unchanged.')
                    temporary.rename(target)
                finally:
                    temporary.unlink(missing_ok=True)
        if register and sys.platform == 'darwin':
            subprocess.run(['/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister', '-f', str(target)], check=True, capture_output=True)
            subprocess.run(['/usr/bin/mdimport', str(target)], check=True, capture_output=True)
    elif state['installed']:
        if sys.platform == 'darwin':
            if register:
                subprocess.run(['/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister', '-u', str(target)], capture_output=True)
            shutil.rmtree(target)
        else:
            target.unlink()
    return shortcut_status(home, path)


def save_shortcut_choice(home, enabled, directory=None):
    import storage
    directory = Path(directory or (Path(home) / 'data'))
    with storage.state_lock(directory):
        settings = storage.read_json(directory / 'settings.json', {})
        settings['launcherEnabled'] = enabled
        storage.atomic_json(directory / 'settings.json', settings)


def configure_shortcut(home, enabled, directory=None):
    if type(enabled) is not bool:
        raise ValueError('Choose whether the Skills launcher is enabled.')
    home = Path(home)
    if not (home / 'launcher.py').is_file() or not (home / 'python-path.txt').is_file():
        raise ValueError('Install the dashboard before adding its search launcher.')
    state = set_shortcut(home, enabled)
    save_shortcut_choice(home, enabled, directory)
    return state


# GitHub backup stays in this existing release file so older installed updaters
# can still accept the release's unchanged application-file allowlist.
class GitHubError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class GitHubBackup:
    MAX_FILE = 20 * 1024 * 1024
    MAX_TOTAL = 100 * 1024 * 1024
    SKIP_DIRS = {'.git', '.system', 'node_modules', '__pycache__', '.venv', 'venv', '.ssh', '.aws'}
    SKIP_FILES = {'.DS_Store', 'usage.jsonl', 'credentials.json', 'auth.json'}
    DESCRIPTION = 'Private Skill Dashboard backups'

    def __init__(self, home, directory, roots):
        import threading
        self.home = Path(home).resolve()
        self.directory = Path(directory).resolve()
        self.roots = {name: Path(path).expanduser().absolute() for name, path in roots}
        self.lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.state = {'busy': False}
        import storage
        prior = storage.read_json(self.directory / 'github-backup-status.json', {})
        if prior.get('running'):
            self.state.update(message='The previous operation was interrupted. Retry it to continue.', running=False, preview=None)
        self.stop_event = threading.Event()
        self.thread = None

    def config(self):
        import storage
        return storage.read_json(self.directory / 'github-backup.json', {})

    def save_config(self, value):
        import storage
        storage.atomic_json(self.directory / 'github-backup.json', value)

    def report(self, **values):
        import storage
        with self.state_lock:
            self.state.update(values)
            # Device codes are temporary and never written to disk.
            storage.atomic_json(self.directory / 'github-backup-status.json',
                                {k: v for k, v in self.state.items() if k not in ('authCode', 'authUrl', 'busy')})

    def status(self):
        import storage
        config = self.config()
        saved = storage.read_json(self.directory / 'github-backup-status.json', {})
        with self.state_lock:
            return {'message': 'GitHub backup is not connected.', **saved, **self.state, 'account': config.get('account'),
                    'repository': config.get('repository'),
                    'repositoryUrl': ('https://github.com/' + config['repository']) if config.get('repository') else None,
                    'enabled': config.get('enabled', False), 'daily': config.get('daily', False),
                    'lastSuccess': config.get('lastSuccess')}

    def cli(self):
        import shutil
        executable = 'gh.exe' if os.name == 'nt' else 'gh'
        private = self.home / 'tools/github-cli' / executable
        if private.is_file():
            return str(private)
        discovered = shutil.which('gh')
        if discovered:
            return discovered
        for directory in (Path.home() / '.local/bin', Path('/opt/homebrew/bin'), Path('/usr/local/bin')):
            if (directory / executable).is_file():
                return str(directory / executable)
        return None

    def ensure_cli(self):
        import io
        import platform
        import urllib.request
        import zipfile
        import updates
        import storage
        existing = self.cli()
        if existing:
            return existing
        self.report(message='Downloading GitHub CLI…')
        system = 'macOS' if sys.platform == 'darwin' else 'windows' if os.name == 'nt' else None
        machine = platform.machine().lower()
        architecture = 'arm64' if machine in ('arm64', 'aarch64') else 'amd64' if machine in ('x86_64', 'amd64') else None
        if not system or not architecture:
            raise ValueError('Install GitHub CLI for this platform, then connect again.')
        release = updates.fetch_json('https://api.github.com/repos/cli/cli/releases/latest')
        suffix = f'_{system}_{architecture}.zip'
        asset = next((a for a in release['assets'] if a['name'].endswith(suffix)), None)
        if not asset or not asset['browser_download_url'].startswith('https://github.com/cli/cli/releases/download/'):
            raise ValueError('A supported GitHub CLI download could not be found.')
        request = urllib.request.Request(asset['browser_download_url'], headers={'User-Agent': 'SkillDashboard'})
        with urllib.request.urlopen(request, timeout=90) as response:
            archive = response.read(80 * 1024 * 1024)
        executable = 'gh.exe' if os.name == 'nt' else 'gh'
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            paths = [name for name in bundle.namelist() if name == 'bin/' + executable or name.endswith('/bin/' + executable)]
            if len(paths) != 1:
                raise ValueError('GitHub CLI download was incomplete.')
            content = bundle.read(paths[0])
        target = self.home / 'tools/github-cli' / executable
        target.parent.mkdir(parents=True, exist_ok=True)
        import tempfile
        fd, temporary = tempfile.mkstemp(dir=target.parent)
        try:
            with os.fdopen(fd, 'wb') as output:
                output.write(content)
            Path(temporary).chmod(0o755)
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return str(target)

    def _run(self, arguments, payload=None):
        import json
        import re
        cli = self.cli()
        if not cli:
            raise ValueError('Connect GitHub first.')
        result = subprocess.run([cli, *arguments], input=json.dumps(payload) if payload is not None else None,
                                text=True, encoding='utf-8', capture_output=True, timeout=120,
                                env={**os.environ, 'GH_PROMPT_DISABLED': '1'},
                                creationflags=0x08000000 if os.name == 'nt' else 0)
        if result.returncode:
            import updates
            code = re.search(r'HTTP (\d{3})', result.stderr)
            status = int(code.group(1)) if code else None
            try:
                response = json.loads(result.stdout)
            except (ValueError, TypeError):
                response = {}
            detail = response.get('message') if isinstance(response, dict) else None
            if not isinstance(detail, str) or not detail.strip():
                detail = result.stderr.strip().splitlines()[0] if result.stderr.strip() else 'GitHub CLI could not complete the request.'
            detail = re.sub(r'\x1b\[[0-9;]*m', '', detail)
            detail = re.sub(r'(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|Bearer\s+\S+)', '[redacted]', detail)[:350]
            endpoint = arguments[3] if len(arguments) > 3 and arguments[0] == 'api' else 'GitHub'
            method = arguments[arguments.index('--method') + 1] if '--method' in arguments else 'GET'
            diagnostic = {'timestamp': updates.now(), 'method': method, 'endpoint': endpoint,
                          'status': status, 'message': detail}
            # Log metadata only: never payloads, skill contents, authentication output or headers.
            try:
                from portable import log_fd
                self.directory.mkdir(parents=True, exist_ok=True)
                path = self.directory / 'github-backup-errors.jsonl'
                with os.fdopen(log_fd(path, write=True), 'a', encoding='utf-8') as stream:
                    stream.write(json.dumps(diagnostic) + '\n')
            except OSError:
                pass  # A diagnostic write must not hide GitHub's actual error.
            label = f'HTTP {status}' if status else 'CLI error'
            raise GitHubError(status, f'GitHub request failed ({label}): {detail}')
        return result.stdout

    def api(self, endpoint, payload=None, method=None):
        import json
        arguments = ['api', '--hostname', 'github.com', endpoint]
        arguments += ['--method', method or ('POST' if payload is not None else 'GET')]
        if payload is not None:
            arguments += ['--input', '-']
        raw = self._run(arguments, payload)
        return json.loads(raw) if raw.strip() else None

    def connect(self):
        import re
        import threading
        cli = self.ensure_cli()
        authenticated = subprocess.run([cli, 'auth', 'status', '--hostname', 'github.com'],
                                       capture_output=True, timeout=30,
                                       creationflags=0x08000000 if os.name == 'nt' else 0).returncode == 0
        if not authenticated:
            self.report(message='Sign in to GitHub to continue.', authUrl='https://github.com/login/device')
            process = subprocess.Popen([cli, 'auth', 'login', '--hostname', 'github.com', '--git-protocol', 'https', '--web'],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       text=True, encoding='utf-8', bufsize=1,
                                       env={**os.environ, 'GH_PROMPT_DISABLED': '1'},
                                       creationflags=0x08000000 if os.name == 'nt' else 0)
            timer = threading.Timer(600, process.terminate)
            timer.daemon = True; timer.start()
            try:
                process.stdin.write('\n'); process.stdin.flush(); process.stdin.close()
                for line in process.stdout:
                    code = re.search(r'\b[A-Z0-9]{4}-[A-Z0-9]{4}\b', line)
                    if code:
                        self.report(authCode=code.group(), message='Enter this code on GitHub to finish sign-in.')
                if process.wait() != 0:
                    raise ValueError('GitHub sign-in was not completed. Connect again to retry.')
            finally:
                timer.cancel()
                if process.poll() is None:
                    process.terminate(); process.wait(timeout=10)
                process.stdout.close()
                self.report(authCode=None, authUrl=None)
        account = self.api('user')['login']
        config = self.config()
        if config.get('repository') and config['repository'].split('/')[0].lower() != account.lower():
            raise ValueError('GitHub is signed in to a different account. Switch back to the backup owner first.')
        config['account'] = account
        self.save_config(config)
        self.report(message='Connected to GitHub as ' + account + '.', error=None)

    @staticmethod
    def safe_relative(name):
        from pathlib import PurePosixPath
        if not isinstance(name, str) or not name or '\\' in name or ':' in name or any(ord(c) < 32 for c in name):
            raise ValueError('Backup contains an invalid path.')
        path = PurePosixPath(name)
        if path.is_absolute() or any(part in ('.', '..', '.system', '.git') for part in path.parts) or str(path) != name:
            raise ValueError('Backup contains an unsafe path.')
        return path

    @classmethod
    def excluded(cls, path):
        path = Path(path)
        return (path.name in cls.SKIP_FILES or path.name == '.env' or path.name.startswith('.env.') or
                path.suffix.lower() in ('.pem', '.key', '.p12', '.pfx', '.log'))

    @staticmethod
    def blob_sha(content):
        import hashlib
        return hashlib.sha1(b'blob ' + str(len(content)).encode() + b'\0' + content).hexdigest()

    def collect(self):
        """Copy personal skills, including linked skill folders; exclude nested links and caches."""
        import json
        import storage
        from portable import linked
        files, skills, seen, permissions = {}, [], set(), {}
        visited = set()
        protected = [root.resolve() / '.system' for root in self.roots.values()]
        if 'codex' in self.roots:
            protected.append(self.roots['codex'].parent.resolve() / 'plugins/cache')
        total = 0
        skipped = 0
        for origin, root in self.roots.items():
            if not root.exists():
                continue
            for current, directories, names in os.walk(root, followlinks=True):
                resolved = Path(current).resolve()
                if resolved in visited or any(resolved.is_relative_to(path) for path in protected):
                    directories[:] = []
                    continue
                visited.add(resolved)
                directories[:] = [d for d in directories if d not in self.SKIP_DIRS and
                                  (not linked(Path(current) / d) or (Path(current) / d / 'SKILL.md').is_file())]
                if 'SKILL.md' not in names or Path(current) == root:
                    continue
                folder = Path(current)
                if linked(folder / 'SKILL.md') or not (folder / 'SKILL.md').is_file() or folder.resolve() in seen:
                    continue
                seen.add(folder.resolve())
                relative = folder.relative_to(root).as_posix()
                self.safe_relative(relative)
                skills.append({'root': origin, 'folder': relative})
                for nested, children, entries in os.walk(folder, followlinks=False):
                    children[:] = [d for d in children if d not in self.SKIP_DIRS and not linked(Path(nested) / d)]
                    for name in entries:
                        path = Path(nested) / name
                        if linked(path) or not path.is_file() or self.excluded(path):
                            skipped += 1; continue
                        if path.stat().st_size > self.MAX_FILE:
                            raise ValueError('A skill file exceeds the 20 MB backup limit. Nothing was uploaded.')
                        with path.open('rb') as stream:
                            content = stream.read(self.MAX_FILE + 1)
                        total += len(content)
                        if len(content) > self.MAX_FILE or total > self.MAX_TOTAL:
                            raise ValueError('Backup exceeds 20 MB per file or 100 MB total. No backup was uploaded.')
                        location = f'skills/{origin}/{relative}/{path.relative_to(folder).as_posix()}'
                        self.safe_relative(location)
                        mode = '100755' if path.stat().st_mode & 0o111 else '100644'
                        files[location] = (content, mode)
                        permissions[location] = (0o755 if mode == '100755' else 0o644) if os.name == 'nt' else path.stat().st_mode & 0o777
                directories[:] = []
        if (self.directory / 'catalog.json').exists():
            raw = (self.directory / 'catalog.json').read_bytes()
            storage.validate_catalog(json.loads(raw))
            files['catalog.json'] = (raw, '100644')
            permissions['catalog.json'] = 0o600 if os.name == 'nt' else (self.directory / 'catalog.json').stat().st_mode & 0o777
        manifest = {'application': 'skill-dashboard', 'version': 1, 'skills': sorted(skills, key=lambda s: (s['root'], s['folder'])), 'permissions': permissions}
        files['backup.json'] = (json.dumps(manifest, sort_keys=True, indent=2).encode(), '100644')
        return files, {'skills': len(skills), 'files': len(files), 'bytes': total, 'excludedFiles': skipped}

    def private_repo(self, repository):
        metadata = self.api('repos/' + repository)
        if not metadata.get('private'):
            raise ValueError('Backup requires a private repository. Nothing was uploaded or restored.')
        account = self.api('user')['login']
        if metadata['owner']['login'].lower() != account.lower():
            raise ValueError('Choose a backup repository in the signed-in account.')
        return metadata

    def remote(self, repository, initial=None):
        from urllib.parse import quote
        metadata = self.private_repo(repository)
        branch = metadata['default_branch']
        reference = self.api(f'repos/{repository}/git/ref/heads/' + quote(branch, safe=''))
        head = reference['object']['sha']
        commit = self.api(f'repos/{repository}/git/commits/{head}')
        tree = self.api(f'repos/{repository}/git/trees/{commit["tree"]["sha"]}?recursive=1')
        if tree.get('truncated'):
            raise ValueError('The backup repository is too large to restore completely.')
        entries = {item['path']: item for item in tree['tree'] if item['type'] == 'blob'}
        if head != initial:
            if 'backup.json' not in entries:
                raise ValueError('This repository is not a Skill Dashboard backup. Choose a different repository name.')
            self.validate_manifest(self.read_blob(repository, entries['backup.json']['sha']))
        return head, branch, entries

    def read_blob(self, repository, sha):
        import base64
        blob = self.api(f'repos/{repository}/git/blobs/{sha}')
        if blob.get('encoding') != 'base64' or blob.get('size', 0) > self.MAX_FILE:
            raise ValueError('Unsupported or oversized backup file.')
        return base64.b64decode(blob['content'])

    def validate_manifest(self, raw):
        import json
        manifest = json.loads(raw)
        if manifest.get('application') != 'skill-dashboard' or manifest.get('version') != 1 or not isinstance(manifest.get('skills'), list):
            raise ValueError('Unsupported skill backup format.')
        permissions = manifest.get('permissions', {})
        if not isinstance(permissions, dict) or any(type(mode) is not int or mode < 0 or mode > 0o777 for mode in permissions.values()):
            raise ValueError('Invalid backup file permissions.')
        seen = set()
        for skill in manifest['skills']:
            if skill.get('root') not in self.roots:
                raise ValueError('Unknown backup skill location.')
            self.safe_relative(skill['folder'])
            identity = (skill['root'], skill['folder'])
            if identity in seen:
                raise ValueError('Duplicate skill location in backup.')
            seen.add(identity)
        return manifest

    def enable(self, name):
        import re
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}', name):
            raise ValueError('Use a repository name with letters, numbers, dots, dashes or underscores.')
        config = self.config()
        account = self.api('user')['login']
        if config.get('account') and config['account'].lower() != account.lower():
            raise ValueError('GitHub changed accounts. Connect again before enabling backup.')
        repository = account + '/' + name
        try:
            metadata = self.api('repos/' + repository)
        except GitHubError as error:
            if error.code != 404:
                raise
            self.report(message='Creating your private backup repository…')
            metadata = self.api('user/repos', {'name': name, 'private': True,
                                             'description': self.DESCRIPTION, 'auto_init': True})
            from urllib.parse import quote
            reference = self.api(f'repos/{repository}/git/ref/heads/' + quote(metadata['default_branch'], safe=''))
            config['initialCommit'] = reference['object']['sha']
        # Only a new repository or a verified existing backup may be initialized.
        self.remote(repository, config.get('initialCommit') if config.get('repository') in (None, repository) else None)
        if config.get('repository') != repository:
            config.pop('lastSuccess', None)
            config.pop('commit', None)
        config.update(account=account, repository=repository, enabled=True, daily=config.get('daily', False))
        self.save_config(config)
        self.backup()

    def backup(self):
        import base64
        import updates
        config = self.config()
        if not config.get('enabled') or not config.get('repository'):
            raise ValueError('Enable GitHub backup first.')
        repository = config['repository']
        self.report(message='Reading your personal skill folders…', error=None)
        files, summary = self.collect()
        head, branch, previous = self.remote(repository, config.get('initialCommit'))
        desired = {path: self.blob_sha(content) for path, (content, _) in files.items()}
        if set(previous) == set(files) and all(previous[path]['sha'] == sha and (os.name == 'nt' or previous[path]['mode'] == files[path][1]) for path, sha in desired.items()):
            config['lastSuccess'] = updates.now(); self.save_config(config)
            self.report(message='Everything is already backed up.', summary=summary)
            return
        tree = []
        inline_budget = 4 * 1024 * 1024
        for index, (path, (content, mode)) in enumerate(sorted(files.items()), 1):
            self.report(message=f'Backing up file {index} of {len(files)}…')
            sha = desired[path]
            if os.name == 'nt' and path in previous and previous[path]['sha'] == sha:
                mode = previous[path]['mode']
            item = {'path': path, 'mode': mode, 'type': 'blob'}
            if path not in previous or previous[path]['sha'] != sha:
                # Batch ordinary UTF-8 instructions/scripts into one tree request.
                # Binary and large files use the blob endpoint instead.
                import json
                try:
                    text = content.decode('utf-8') if b'\0' not in content else None
                except UnicodeDecodeError:
                    text = None
                cost = len(json.dumps(text).encode()) if text is not None else inline_budget + 1
                if text is not None and cost <= min(inline_budget, 1024 * 1024):
                    item['content'] = text
                    inline_budget -= cost
                else:
                    blob = self.api(f'repos/{repository}/git/blobs', {'content': base64.b64encode(content).decode(), 'encoding': 'base64'})
                    item['sha'] = blob['sha']
            else:
                item['sha'] = sha
            tree.append(item)
        created = self.api(f'repos/{repository}/git/trees', {'tree': tree})
        commit = self.api(f'repos/{repository}/git/commits',
                          {'message': 'Back up personal skills and dashboard catalog', 'tree': created['sha'], 'parents': [head]})
        from urllib.parse import quote
        self.api(f'repos/{repository}/git/refs/heads/' + quote(branch, safe=''), {'sha': commit['sha'], 'force': False}, 'PATCH')
        config.pop('initialCommit', None)
        config.update(lastSuccess=updates.now(), commit=commit['sha'])
        self.save_config(config)
        self.report(message='Skills and catalog backed up to your private GitHub repository.', summary=summary)

    def destination(self, relative):
        """Map a backed-up file to a personal root, refusing linked ancestors."""
        from portable import linked
        self.safe_relative(relative)
        parts = relative.split('/')
        if len(parts) < 4 or parts[0] != 'skills' or parts[1] not in self.roots:
            raise ValueError('Unexpected file in the backup repository.')
        root = self.roots[parts[1]]
        target = root.joinpath(*parts[2:])
        for path in (target, *target.parents):
            if path.exists() or path.is_symlink():
                if linked(path):
                    raise ValueError('Restore would follow a linked location. It was left unchanged.')
        return target

    @staticmethod
    def local_sha(path):
        if path.exists() and not path.is_file():
            raise ValueError('A restore destination is not a regular file.')
        if path.is_file() and path.stat().st_size > GitHubBackup.MAX_FILE:
            raise ValueError('An existing restore destination exceeds the 20 MB file limit.')
        return GitHubBackup.blob_sha(path.read_bytes()) if path.is_file() else None

    def preview(self, name):
        import json
        import re
        import uuid
        import time
        import storage
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}', name):
            raise ValueError('Choose a valid backup repository name.')
        account = self.api('user')['login']
        repository = account + '/' + name
        head, branch, entries = self.remote(repository)
        manifest = self.validate_manifest(self.read_blob(repository, entries['backup.json']['sha']))
        prefixes = ['skills/' + item['root'] + '/' + item['folder'] + '/' for item in manifest['skills']]
        if any(prefix + 'SKILL.md' not in entries for prefix in prefixes):
            raise ValueError('The backup is missing a skill manifest.')
        items, conflicts, total = [], [], 0
        for path, entry in entries.items():
            if path == 'backup.json':
                continue
            if entry['mode'] not in ('100644', '100755'):
                raise ValueError('Backup contains a linked or unsupported file.')
            if path == 'catalog.json':
                destination = self.directory / 'catalog.json'
            else:
                if not any(path.startswith(prefix) for prefix in prefixes):
                    raise ValueError('A backup file is outside its declared skill folder.')
                destination = self.destination(path)
            size = entry.get('size', 0)
            total += size
            if size > self.MAX_FILE or total > self.MAX_TOTAL:
                raise ValueError('The backup is too large to restore safely.')
            old = self.local_sha(destination)
            if old is not None and old != entry['sha']:
                conflicts.append(path)
            items.append({'path': path, 'sha': entry['sha'], 'mode': entry['mode'], 'old': old, 'permissions': manifest.get('permissions', {}).get(path)})
        plan = {'id': uuid.uuid4().hex, 'expires': time.time() + 900, 'repository': repository,
                'commit': head, 'items': items, 'skills': len(manifest['skills']), 'conflicts': conflicts}
        storage.atomic_json(self.directory / 'restore-plan.json', plan)
        self.report(message='Restore preview is ready. Review it before restoring.',
                    preview={'id': plan['id'], 'skills': plan['skills'], 'files': len(items),
                             'conflicts': conflicts, 'repository': repository})

    def restore(self, identifier, overwrite=False):
        import json
        import tempfile
        import time
        import storage
        import updates
        from portable import linked
        plan = storage.read_json(self.directory / 'restore-plan.json', {})
        if plan.get('id') != identifier or plan.get('expires', 0) < time.time() or type(overwrite) is not bool:
            raise ValueError('Restore preview expired. Preview the backup again.')
        self.private_repo(plan['repository'])
        prepared = []
        for item in plan['items']:
            target = self.directory / 'catalog.json' if item['path'] == 'catalog.json' else self.destination(item['path'])
            if (target.exists() or target.is_symlink()) and linked(target):
                raise ValueError('A restore destination became linked. Preview again.')
            if self.local_sha(target) != item['old']:
                raise ValueError('Local files changed after the preview. Preview again before restoring.')
            if item['old'] == item['sha'] or (item['old'] is not None and not overwrite):
                continue
            content = self.read_blob(plan['repository'], item['sha'])
            if self.blob_sha(content) != item['sha']:
                raise ValueError('A backup file could not be read completely.')
            if item['path'] == 'catalog.json':
                storage.validate_catalog(json.loads(content))
            prepared.append((item, target, content))
        recovery = self.directory / 'restore-history' / plan['id']
        applied = []
        try:
            for item, target, content in prepared:
                # Recheck immediately before writing; never overwrite a newer local edit.
                if item['path'] != 'catalog.json':
                    target = self.destination(item['path'])
                if self.local_sha(target) != item['old']:
                    raise ValueError('A destination changed during restore.')
                if target.exists():
                    saved = recovery / item['path']; saved.parent.mkdir(parents=True, exist_ok=True)
                    import shutil
                    shutil.copy2(target, saved)
                target.parent.mkdir(parents=True, exist_ok=True)
                fd, temporary = tempfile.mkstemp(dir=target.parent)
                try:
                    with os.fdopen(fd, 'wb') as output:
                        output.write(content)
                    permissions = item.get('permissions')
                    if permissions is None:
                        permissions = 0o755 if item['mode'] == '100755' else 0o644
                    if target.exists():
                        permissions &= (target.stat().st_mode & 0o777) | 0o700
                    Path(temporary).chmod(permissions)
                    os.replace(temporary, target)
                finally:
                    Path(temporary).unlink(missing_ok=True)
                applied.append((item, target))
        except Exception:
            for item, target in reversed(applied):
                if self.local_sha(target) != item['sha']:
                    continue
                saved = recovery / item['path']
                if saved.exists():
                    os.replace(saved, target)
                else:
                    target.unlink(missing_ok=True)
            raise
        (self.directory / 'restore-plan.json').unlink(missing_ok=True)
        self.report(message=f'Restored {len(applied)} file' + ('' if len(applied) == 1 else 's') + '. Unselected conflicts were kept.', preview=None,
                    lastRestore=updates.now(), recoveryPath=str(recovery) if recovery.exists() else None)

    def submit(self, action, payload=None):
        import threading
        payload = payload or {}
        if action not in ('connect', 'enable', 'backup', 'preview', 'restore', 'configure'):
            raise ValueError('Unknown backup action.')
        if not self.lock.acquire(blocking=False):
            raise ValueError('A GitHub backup operation is already running.')
        with self.state_lock:
            self.state.update(busy=True, running=True, message='Starting GitHub backup operation…', error=None, preview=None)
        self.report(running=True)
        def execute():
            try:
                if action == 'connect': self.connect()
                elif action == 'enable': self.enable(payload.get('name'))
                elif action == 'backup': self.backup()
                elif action == 'preview': self.preview(payload.get('name'))
                elif action == 'restore': self.restore(payload.get('id'), payload.get('overwrite', False))
                else:
                    if type(payload.get('daily')) is not bool:
                        raise ValueError('Choose whether daily backup is enabled.')
                    config = self.config()
                    if not config.get('enabled'):
                        raise ValueError('Enable backup before scheduling it.')
                    config['daily'] = payload['daily']; self.save_config(config)
                    self.report(message='Daily backup enabled.' if config['daily'] else 'Daily backup disabled.')
            except Exception as error:
                self.report(error=str(error), message='The operation did not complete. Your last successful backup is unchanged.',
                            authCode=None, authUrl=None)
            finally:
                try:
                    self.report(running=False)
                finally:
                    with self.state_lock:
                        self.state['busy'] = False
                    self.lock.release()
        self.thread = threading.Thread(target=execute, daemon=True)
        self.thread.start()
        return {'message': 'GitHub backup operation started.'}

    def start_scheduler(self, interval=30):
        import threading
        from datetime import datetime, timezone
        last_attempt = [0.0]
        def schedule():
            import time
            while not self.stop_event.wait(interval):
                config = self.config()
                if not config.get('enabled') or not config.get('daily'):
                    continue
                try:
                    last = datetime.fromisoformat(config['lastSuccess']).timestamp() if config.get('lastSuccess') else 0
                    if time.time() - max(last, last_attempt[0]) >= 86400:
                        self.submit('backup')
                        last_attempt[0] = time.time()
                except ValueError:
                    pass
        threading.Thread(target=schedule, daemon=True).start()

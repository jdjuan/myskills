#!/usr/bin/env python3
"""Install, supervise, update and open a personal MySkills."""
import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
import webbrowser

import services
import storage
import updates

HERE = Path(__file__).resolve().parent
LAUNCHER = '''from pathlib import Path
import json, os, runpy, sys
home = Path(__file__).resolve().parent
os.environ['SKILL_DASHBOARD_HOME'] = str(home)
current = json.loads((home / 'current.json').read_text(encoding='utf-8'))
release = home / 'releases' / current['version']
sys.path.insert(0, str(release))
runpy.run_path(str(release / 'dashboard.py'), run_name='__main__')
'''


def python_executable(home):
    return (Path(home) / 'python-path.txt').read_text(encoding='utf-8').strip()


def install(home, source=None, migrate=None, start=True, open_browser=True, shortcut=True):
    home = Path(home).resolve()
    directory = home / 'data'
    directory.mkdir(parents=True, exist_ok=True)
    # The service must discover the same Codex home as the installing session.
    settings = updates.preferences(directory)
    settings.setdefault('codexHome', str(Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))).expanduser().resolve()))
    storage.atomic_json(directory / 'settings.json', settings)
    if migrate:
        original = Path(migrate).resolve() / 'catalog.json'
        if original.exists() and not (directory / 'catalog.json').exists():
            config = storage.validate_catalog(storage.read_json(original))
            shutil.copy2(original, directory / 'catalog.before-migration.json')
            storage.atomic_json(directory / 'catalog.json', config)
    if source:
        source = Path(source).resolve()
        version = (source / 'VERSION').read_text().strip()
        updates.version_tuple(version)
        release = home / 'releases' / version
        # Local installs are explicit, not automatic updates of a development checkout.
        if release.exists():
            raise ValueError('This local version is already installed. Bump VERSION or use a separate test home.')
        release.mkdir(parents=True)
        for name in updates.FILES:
            target = release / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / name, target)
    else:
        version, url = updates.latest_release()
        updates.download_release(home, version, url)
    storage.atomic_json(home / 'current.json', {'version': version})
    (home / 'launcher.py').write_text(LAUNCHER, encoding='utf-8')
    (home / 'python-path.txt').write_text(sys.executable, encoding='utf-8')
    if not (directory / 'catalog.json').exists():
        storage.atomic_json(directory / 'catalog.json', storage.read_json(home / 'releases' / version / 'catalog.json'))
    if not shortcut:
        services.save_shortcut_choice(home, False, directory)
    elif settings.get('launcherEnabled', True):
        try:
            services.configure_shortcut(home, True, directory)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            # A launcher is convenience; a naming conflict must not break installation.
            print(f'MySkills launcher was not changed: {error}', file=sys.stderr)
    if start:
        if migrate:
            services.remove_legacy(migrate)
        services.start(home)
        if open_browser:
            open_dashboard(home)
    print(json.dumps({'home': str(home), 'catalog': str(directory / 'catalog.json'),
                      'launcher': str(home / 'launcher.py'), 'version': version}))


def dashboard_url(home):
    info = storage.read_json(Path(home) / 'data/server.json', {})
    return info.get('url')


def open_dashboard(home):
    for _ in range(100):
        url = dashboard_url(home)
        if url:
            try:
                with urllib.request.urlopen(url + '/api/health', timeout=1) as response:
                    if json.load(response).get('application') == 'skill-dashboard':
                        webbrowser.open(url)
                        return url
            except (OSError, ValueError):
                pass
        time.sleep(.1)
    raise ValueError('The dashboard has not started. Check the installation logs.')


def terminate(child):
    if child and child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill(); child.wait()


def supervise(home):
    """One foreground supervisor owns the server and periodic release checks."""
    home = Path(home)
    directory = home / 'data'
    stopped = False
    child = None
    def stop(*_):
        nonlocal stopped
        stopped = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    directory.mkdir(parents=True, exist_ok=True)
    # OS-level lock prevents a manual launcher and login startup running duplicate supervisors.
    from contextlib import ExitStack
    with ExitStack() as stack:
        lock = stack.enter_context((directory / 'supervisor.lock').open('a+b'))
        try:
            if os.name == 'nt':
                import msvcrt
                lock.write(b'\0'); lock.flush(); lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError('The dashboard service is already running.')
        storage.atomic_json(directory / 'supervisor.json', {'pid': os.getpid()})
        (home / 'logs').mkdir(exist_ok=True)
        log = stack.enter_context((home / 'logs/server.log').open('a', encoding='utf-8'))
        last_check = 0
        last_request = None
        try:
            while not stopped:
                if child is None or child.poll() is not None:
                    version = storage.read_json(home / 'current.json')['version']
                    command = [python_executable(home), str(home / 'releases' / version / 'server.py'),
                               '--data-dir', str(directory), '--managed']
                    env = {**os.environ, 'SKILL_DASHBOARD_HOME': str(home), 'SKILL_DASHBOARD_DATA_DIR': str(directory),
                           'CODEX_HOME': updates.preferences(directory).get('codexHome', str(Path.home() / '.codex'))}
                    child = subprocess.Popen(command, env=env, stdout=log, stderr=log,
                                             creationflags=0x08000000 if os.name == 'nt' else 0)
                request = storage.read_json(directory / 'update-request.json')
                manual = bool(request and request != last_request)
                if manual or (updates.preferences(directory)['autoUpdate'] and time.time() - last_check >= 86400):
                    last_check = time.time()
                    last_request = request
                    (directory / 'update-request.json').unlink(missing_ok=True)
                    newer = updates.check_for_update(home, directory)
                    if newer:
                        terminate(child)
                        child = None
                        previous = storage.read_json(home / 'current.json')['version']
                        storage.atomic_json(home / 'current.json', {'version': newer, 'previous': previous})
                        # Keep current + previous; never prune user/runtime directories.
                        for path in (home / 'releases').iterdir():
                            if path.is_dir() and path.name not in (newer, previous):
                                shutil.rmtree(path)
                for _ in range(20):
                    if stopped:
                        break
                    time.sleep(.25)
        finally:
            terminate(child)
            (directory / 'supervisor.json').unlink(missing_ok=True)
            (directory / 'server.json').unlink(missing_ok=True)


def record_usage(home, path):
    # Resolve the current implementation each invocation; the command stays stable across updates.
    version = storage.read_json(Path(home) / 'current.json')['version']
    subprocess.run([python_executable(home), str(Path(home) / 'releases' / version / 'usage.py'), path], check=True)


def usage_instruction(home):
    prefix = '& ' if os.name == 'nt' else ''
    return ('<!-- SKILL_DASHBOARD_USAGE_START -->\n'
            'Whenever you apply a skill, record one use at the start by running:\n\n'
            f'`{prefix}"{python_executable(home)}" "{Path(home) / "launcher.py"}" record /absolute/path/to/SKILL.md`\n\n'
            'Count once per actual invocation, including supporting skills. Inspection alone does not count. '
            'Do not log prompts, credentials or project contents. Do not commit usage logs. '
            'If recording fails, continue the task; recording is best effort.\n'
            '<!-- SKILL_DASHBOARD_USAGE_END -->')


def uninstall(home, remove_data=False):
    home = Path(home)
    directory = home / 'data'
    if services.shortcut_status(home)['installed']:
        services.set_shortcut(home, False)
    services.stop(remove=True)
    for _ in range(50):
        if not (directory / 'supervisor.json').exists():
            break
        time.sleep(.2)
    if os.name == 'nt':
        # Windows cannot unlink the running bundled Python DLLs. Clean up after this process exits.
        import base64
        encoded_home = base64.b64encode(str(home).encode()).decode()
        script = ("$ErrorActionPreference='Stop'; "
                  f"$h=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded_home}')); "
                  f"Wait-Process -Id {os.getpid()} -ErrorAction SilentlyContinue; "
                  "$items=@('releases','runtime','uv','tools','logs','current.json','launcher.py','python-path.txt'); "
                  + ("$items+=@('data'); " if remove_data else '') +
                  "foreach($item in $items){$p=Join-Path $h $item; if(Test-Path -LiteralPath $p){Remove-Item -LiteralPath $p -Recurse -Force}}")
        encoded_script = base64.b64encode(script.encode('utf-16le')).decode()
        subprocess.Popen(['powershell', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded_script],
                         creationflags=0x00000008 | 0x00000200,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         cwd=str(home.parent))
    else:
        for name in ('releases', 'runtime', 'uv', 'tools', 'logs'):
            if (home / name).exists():
                shutil.rmtree(home / name)
        for name in ('current.json', 'launcher.py', 'python-path.txt'):
            (home / name).unlink(missing_ok=True)
        if remove_data:
            shutil.rmtree(directory)
    print('Dashboard removed. Installed skills were left untouched.' +
          (' Windows will finish removing runtime files when this process exits.' if os.name == 'nt' else ''))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['install', 'open', 'start', 'stop', 'status', 'update',
                                         'uninstall', 'supervise', 'record', 'usage-instruction', 'rollback'])
    parser.add_argument('skill_path', nargs='?')
    parser.add_argument('--home', type=Path, default=storage.install_home())
    parser.add_argument('--source', type=Path)
    parser.add_argument('--migrate-checkout', type=Path)
    parser.add_argument('--no-start', action='store_true')
    parser.add_argument('--no-open', action='store_true')
    parser.add_argument('--no-shortcut', action='store_true')
    parser.add_argument('--remove-data', action='store_true')
    args = parser.parse_args()
    home = args.home.expanduser().resolve()
    os.environ['SKILL_DASHBOARD_HOME'] = str(home)
    directory = home / 'data'
    if args.action == 'install':
        install(home, args.source, args.migrate_checkout, not args.no_start, not args.no_open, not args.no_shortcut)
    elif args.action == 'supervise':
        supervise(home)
    elif args.action == 'start':
        services.start(home)
    elif args.action == 'stop':
        services.stop(remove=True)
    elif args.action == 'open':
        print(open_dashboard(home))
    elif args.action == 'status':
        print(json.dumps({**updates.status(directory, home), 'server': storage.read_json(directory / 'server.json'),
                          'supervisor': storage.read_json(directory / 'supervisor.json')}, indent=2))
    elif args.action == 'update':
        print(updates.request_update(directory)['message'])
    elif args.action == 'rollback':
        current = storage.read_json(home / 'current.json')
        previous = current.get('previous')
        if not previous or not (home / 'releases' / previous).is_dir():
            raise ValueError('No previous release is available.')
        services.stop(remove=True)
        updates.set_preferences({'autoUpdate': False}, directory)
        storage.atomic_json(home / 'current.json', {'version': previous, 'previous': current['version']})
        services.start(home)
    elif args.action == 'uninstall':
        uninstall(home, args.remove_data)
    elif args.action == 'usage-instruction':
        print(usage_instruction(home))
    elif args.action == 'record':
        if not args.skill_path:
            parser.error('record requires a SKILL.md path')
        record_usage(home, args.skill_path)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f'MySkills: {error}', file=sys.stderr)
        sys.exit(1)

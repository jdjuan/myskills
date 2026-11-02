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

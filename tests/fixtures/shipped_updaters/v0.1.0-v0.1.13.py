"""GitHub stable-release downloads and local update preferences."""
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shutil
import tempfile
import urllib.request
import zipfile

import storage

REPOSITORY = 'jdjuan/skill-dashboard'
ASSET = 'skill-dashboard.zip'
FILES = {'server.py', 'usage.py', 'portable.py', 'storage.py', 'updates.py', 'dashboard.py',
         'services.py', 'catalog.json', 'VERSION', 'dist/index.html', 'README.md'}


def now():
    return datetime.now(timezone.utc).isoformat()


def preferences(directory=None):
    return {'autoUpdate': True, **storage.read_json(Path(directory or storage.data_home()) / 'settings.json', {})}


def set_preferences(values, directory=None):
    if set(values) - {'autoUpdate'} or type(values.get('autoUpdate')) is not bool:
        raise ValueError('Choose whether automatic updates are enabled.')
    directory = Path(directory or storage.data_home())
    with storage.state_lock(directory):
        config = preferences(directory)
        config.update(values)
        storage.atomic_json(directory / 'settings.json', config)
    return config


def status(directory=None, home=None):
    directory = Path(directory or storage.data_home())
    home = Path(home or storage.install_home())
    installed = storage.read_json(home / 'current.json', {})
    return {**preferences(directory), **storage.read_json(directory / 'update-status.json', {}),
            'version': installed.get('version', 'development'), 'managed': bool(installed),
            'catalogPath': str(directory / 'catalog.json')}


def request_update(directory=None):
    directory = Path(directory or storage.data_home())
    if not (storage.install_home() / 'current.json').exists():
        raise ValueError('Automatic updates are available in managed installations. This is a development checkout.')
    storage.atomic_json(directory / 'update-request.json', {'requestedAt': now()})
    return {'message': 'Checking for updates. The dashboard will reconnect if it restarts.'}


def version_tuple(version):
    match = re.fullmatch(r'v?(\d+)\.(\d+)\.(\d+)', version)
    if not match:
        raise ValueError('Release version must be a stable major.minor.patch version.')
    return tuple(map(int, match.groups()))


def fetch_json(url):
    request = urllib.request.Request(url, headers={'Accept': 'application/vnd.github+json',
                                                  'User-Agent': 'SkillDashboard'})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read(1024 * 1024))


def latest_release():
    release = fetch_json(f'https://api.github.com/repos/{REPOSITORY}/releases/latest')
    if release.get('draft') or release.get('prerelease'):
        raise ValueError('No stable release is available.')
    version_tuple(release['tag_name'])
    asset = next((a for a in release.get('assets', []) if a['name'] == ASSET), None)
    if not asset:
        raise ValueError('The release has no dashboard download.')
    url = asset['browser_download_url']
    if not url.startswith(f'https://github.com/{REPOSITORY}/releases/download/'):
        raise ValueError('Unexpected release download location.')
    return release['tag_name'].lstrip('v'), url


def download_release(home, version, url):
    home = Path(home)
    destination = home / 'releases' / version
    if destination.is_dir():
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent, prefix='.download-') as temporary:
        work = Path(temporary)
        archive = work / 'release.zip'
        request = urllib.request.Request(url, headers={'User-Agent': 'SkillDashboard'})
        with urllib.request.urlopen(request, timeout=60) as source, archive.open('wb') as output:
            shutil.copyfileobj(source, output)
        payload = work / 'app'
        payload.mkdir()
        with zipfile.ZipFile(archive) as bundle:
            # Only application files can be extracted, never user data or paths outside the release.
            names = bundle.namelist()
            if set(names) != FILES or len(names) != len(FILES):
                raise ValueError('Incomplete dashboard release archive.')
            for name in names:
                target = payload / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(bundle.read(name))
        payload.rename(destination)
    return destination


def check_for_update(home=None, directory=None):
    home = Path(home or storage.install_home())
    directory = Path(directory or storage.data_home())
    installed = storage.read_json(home / 'current.json', {})
    result = {'lastChecked': now(), 'error': None, 'message': 'Up to date.'}
    try:
        version, url = latest_release()
        result['latestVersion'] = version
        if not installed or version_tuple(version) > version_tuple(installed['version']):
            download_release(home, version, url)
            result['message'] = 'Update downloaded.'
            storage.atomic_json(directory / 'update-status.json', result)
            return version
    except Exception as error:
        result.update(error=str(error), message='Update unavailable; keeping the installed version.')
    storage.atomic_json(directory / 'update-status.json', result)
    return None

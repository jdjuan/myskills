"""Every installation updates itself with the updater it already has, not the one being released.

tests/fixtures/shipped_updaters holds a verbatim copy of each distinct updates.py ever released.
This test serves the release exactly as the release workflow publishes it and makes each of those
updaters fetch and unpack it, so a release can never silently strand existing installations.
"""
import importlib.util
import io
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch
import urllib.request
import zipfile

import updates

BASE = Path(__file__).resolve().parents[1]
SHIPPED = BASE / 'tests/fixtures/shipped_updaters'
# These updaters reject every release from the renamed repository: they require download URLs under
# jdjuan/skill-dashboard, and v0.1.0-v0.1.13 also require the older 11-file archive. They can only
# move forward by running install.sh / install.ps1 again. Never add to this list to make a release pass.
# v0.1.16-v0.1.22 required the archive to match its exact file list, so it rejects releases that
# bundle icons/fluent. Accepted deliberately (Juan, 14 Nov 2026): affected installs rerun install.sh.
STRANDED = {'v0.1.0-v0.1.13', 'v0.1.14-v0.1.15', 'v0.1.16-v0.1.22'}
TAG = 'v999.0.0'


def published_assets():
    """ZIP asset names exactly as the release workflow uploads them."""
    workflow = (BASE / '.github/workflows/release.yml').read_text(encoding='utf-8')
    create = next(line for line in workflow.splitlines() if 'gh release create' in line)
    names = re.findall(r'release-dist/([\w.-]+\.zip)', create)
    for source, copy in re.findall(r'cp release-dist/([\w.-]+\.zip) release-dist/([\w.-]+\.zip)', workflow):
        assert source in names
    return names


def release_archive():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(updates.FILES):
            archive.write(BASE / name, name)
    return buffer.getvalue()


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def fake_github(archive):
    download = f'https://github.com/{updates.REPOSITORY}/releases/download/{TAG}/'
    release = {'tag_name': TAG, 'draft': False, 'prerelease': False,
               'assets': [{'name': name, 'browser_download_url': download + name} for name in published_assets()]}

    def urlopen(request, timeout=None):
        url = request.full_url if isinstance(request, urllib.request.Request) else request
        if url.startswith('https://api.github.com/repos/') and url.endswith('/releases/latest'):
            # GitHub redirects a renamed repository's API to its current name.
            return Response(json.dumps(release).encode())
        if url.startswith(download):
            return Response(archive)
        raise AssertionError(f'Unexpected request: {url}')
    return urlopen


def load(path):
    spec = importlib.util.spec_from_file_location(f'shipped_updater_{path.stem.replace("-", "_").replace(".", "_")}', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ShippedUpdaterTest(unittest.TestCase):
    def update_with(self, path):
        updater = load(path)
        with tempfile.TemporaryDirectory() as home, patch('urllib.request.urlopen', fake_github(release_archive())):
            version, url = updater.latest_release()
            destination = updater.download_release(home, version, url)
            self.assertEqual(version, TAG.lstrip('v'))
            self.assertEqual((destination / 'VERSION').read_text(encoding='utf-8'),
                             (BASE / 'VERSION').read_text(encoding='utf-8'))

    def test_every_supported_installation_accepts_this_release(self):
        shipped = sorted(SHIPPED.glob('*.py'))
        self.assertTrue(shipped)
        self.assertTrue(STRANDED <= {path.stem for path in shipped})
        for path in shipped:
            if path.stem in STRANDED:
                continue
            with self.subTest(installed=path.stem):
                self.update_with(path)

    def test_stranded_list_is_still_accurate(self):
        # If one of these starts working again, take it off the list so it stays protected.
        for name in STRANDED:
            with self.subTest(installed=name), self.assertRaises(Exception):
                self.update_with(SHIPPED / f'{name}.py')

    def test_the_updater_being_released_is_snapshotted(self):
        current = (BASE / 'updates.py').read_bytes().replace(b'\r\n', b'\n')
        snapshots = {path.read_bytes().replace(b'\r\n', b'\n') for path in SHIPPED.glob('*.py')}
        self.assertIn(current, snapshots,
                      'updates.py changed: copy it to tests/fixtures/shipped_updaters/<first version>-<last version>.py '
                      'so future releases are checked against it too.')


if __name__ == '__main__':
    unittest.main()

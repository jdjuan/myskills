#!/usr/bin/env python3
"""Local and CI release preflight. Never deploys or changes personal data."""
from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
import updates


def main():
    for name in updates.FILES:
        if name.endswith('.py'):
            compile((BASE / name).read_text(encoding='utf-8'), name, 'exec')
    html = (BASE / 'dist/index.html').read_text(encoding='utf-8')
    javascript = html.split('<script>', 1)[1].split('</script>', 1)[0]
    node = shutil.which('node')
    if node:
        subprocess.run([node, '--check'], input=javascript, encoding='utf-8', check=True)
    elif os.environ.get('CI'):
        raise RuntimeError('CI requires Node for frontend syntax and behavior checks.')
    else:
        print('Node unavailable: skipping optional JavaScript checks.', flush=True)
    subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-v'],
                   cwd=BASE, check=True)
    with tempfile.TemporaryDirectory(prefix='skill-dashboard-preflight-') as directory:
        archive = Path(directory) / 'myskills.zip'
        subprocess.run([sys.executable, str(BASE / 'scripts/package.py'), str(archive)],
                       cwd=BASE, check=True)
        with zipfile.ZipFile(archive) as package:
            if set(package.namelist()) != updates.FILES or package.testzip():
                raise ValueError('The release archive failed its allowlist or integrity check.')
            if package.read('VERSION').decode('utf-8').strip() != (BASE / 'VERSION').read_text(encoding='utf-8').strip():
                raise ValueError('The packaged version does not match VERSION.')
    print(f'Release preflight passed on {sys.platform}: syntax, tests and release archive.', flush=True)


if __name__ == '__main__':
    main()

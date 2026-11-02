"""Select a stable release version without committing generated version bumps."""
import os
from pathlib import Path
import re
import subprocess
import sys

STABLE_TAG = re.compile(r'v(\d+)\.(\d+)\.(\d+)$')


def stable_tags(tags):
    return {tuple(map(int, match.groups())): tag for tag in tags
            if (match := STABLE_TAG.fullmatch(tag))}


def release_version(base, tags, head_tags, requested_tag=None):
    match = STABLE_TAG.fullmatch('v' + base)
    if not match:
        raise ValueError('VERSION must contain a stable major.minor.patch version.')
    minimum = tuple(map(int, match.groups()))
    if requested_tag:
        if requested_tag != 'v' + base:
            raise ValueError('An explicit release tag must match VERSION.')
        return base
    current = stable_tags(head_tags)
    if current:
        # Branch and tag events, or retries, reuse the same immutable release.
        version = max(current)
        if version < minimum:
            raise ValueError('The tag on this commit is older than VERSION.')
    else:
        existing = stable_tags(tags)
        latest = max(existing, default=(-1, -1, -1))
        version = minimum if minimum > latest else (*latest[:2], latest[2] + 1)
    return '.'.join(map(str, version))


def main():
    base = Path(__file__).resolve().parents[1]
    def git(*args):
        return subprocess.check_output(['git', *args], cwd=base, text=True).splitlines()
    requested = os.environ.get('GITHUB_REF_NAME') if os.environ.get('GITHUB_REF_TYPE') == 'tag' else None
    version = release_version((base / 'VERSION').read_text().strip(),
                              git('tag', '--list'), git('tag', '--points-at', 'HEAD'), requested)
    # Only the CI checkout and packaged VERSION change. The source tree keeps a minimum version.
    (base / 'VERSION').write_text(version + '\n')
    if output := os.environ.get('GITHUB_OUTPUT'):
        with open(output, 'a', encoding='utf-8') as stream:
            stream.write(f'version={version}\ntag=v{version}\n')
    print('v' + version)


if __name__ == '__main__':
    main()

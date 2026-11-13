"""Personal dashboard state; application releases never own these files."""
import contextlib
import hashlib
import json
import re
import os
from pathlib import Path
import sys
import tempfile
import threading

_LOCK = threading.RLock()


def install_home():
    if os.environ.get('SKILL_DASHBOARD_HOME'):
        return Path(os.environ['SKILL_DASHBOARD_HOME']).expanduser().resolve()
    if sys.platform == 'win32':
        return Path(os.environ.get('LOCALAPPDATA', str(Path.home() / 'AppData/Local'))) / 'SkillDashboard'
    if sys.platform == 'darwin':
        return Path.home() / 'Library/Application Support/SkillDashboard'
    return Path.home() / '.local/share/SkillDashboard'


def data_home():
    return Path(os.environ.get('SKILL_DASHBOARD_DATA_DIR', str(install_home() / 'data'))).expanduser().resolve()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except FileNotFoundError:
        return default


@contextlib.contextmanager
def state_lock(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with _LOCK, (directory / '.state.lock').open('a+b') as stream:
        if os.name == 'nt':
            import msvcrt
            stream.seek(0)
            if not stream.read(1):
                stream.write(b'\0'); stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == 'nt':
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def revision(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def validate_catalog(config):
    if not isinstance(config, dict) or config.get('version') != 1:
        raise ValueError('Unsupported catalog version.')
    if not isinstance(config.get('groups'), list):
        raise ValueError('Catalog requires a list of categories.')
    fallback = config.get('fallback_group')
    if not isinstance(fallback, dict) or not all(isinstance(fallback.get(k), str) and fallback[k].strip() for k in ('id', 'label', 'section')):
        raise ValueError('Catalog requires an uncategorized group.')
    identifiers = {fallback['id']}
    def text(value):
        return isinstance(value, str) and 0 < len(value.strip()) <= 200
    for group in config['groups']:
        if not isinstance(group, dict) or not all(text(group.get(k)) for k in ('id', 'label', 'section')):
            raise ValueError('Each category needs an id, label and section.')
        if group['id'] in identifiers or group['id'].startswith(('plugin-', 'toolkit-')):
            raise ValueError('Category ids must be unique and not reserved.')
        identifiers.add(group['id'])
        for key in ('prefixes', 'excluded_skills'):
            values = group.get(key, [])
            if not isinstance(values, list) or not all(text(v) for v in values):
                raise ValueError('Invalid category matching rule.')
        members = group.get('skills', [])
        if not isinstance(members, list):
            raise ValueError('Category skills must be a list.')
        names = []
        for member in members:
            name = member.get('name') if isinstance(member, dict) else member
            if not text(name):
                raise ValueError('Invalid skill member.')
            if isinstance(member, dict) and 'step' in member and (type(member['step']) is not int or member['step'] < 1):
                raise ValueError('Steps must be positive integers.')
            names.append(name)
        if len(names) != len(set(names)):
            raise ValueError('A category cannot contain duplicate skill members.')
    if not isinstance(config.get('plugin_labels', {}), dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in config.get('plugin_labels', {}).items()):
        raise ValueError('Invalid plugin labels.')
    customized = config.get('customized_skills', [])
    if not isinstance(customized, list) or not all(isinstance(path, str) and 0 < len(path) <= 4096 for path in customized):
        raise ValueError('Invalid customized skills.')
    credits = config.get('installed_skills', {})
    def credit(value):
        if text(value):
            return True
        return (isinstance(value, dict) and text(value.get('credit')) and set(value) <= {'credit', 'source', 'skillPath'}
                and ('source' not in value or (isinstance(value['source'], str) and re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', value['source'])))
                and ('skillPath' not in value or (text(value['skillPath']) and value['skillPath'].endswith('SKILL.md') and '..' not in value['skillPath'].split('/'))))
    if not isinstance(credits, dict) or not all(text(k) and credit(v) for k, v in credits.items()):
        raise ValueError('Invalid installed skills.')
    sections = config.get('compact_sections', [])
    if not isinstance(sections, list):
        raise ValueError('Invalid compact sections.')
    for section in sections:
        if not isinstance(section, dict) or not all(text(section.get(k)) for k in ('id', 'label')):
            raise ValueError('Invalid compact section.')
        for key in ('skills', 'prefixes', 'sources', 'excluded_skills'):
            if not isinstance(section.get(key, []), list) or not all(text(v) for v in section.get(key, [])):
                raise ValueError('Invalid compact matching rule.')
    return config


def ensure_catalog(directory=None):
    directory = Path(directory or data_home())
    path = directory / 'catalog.json'
    with state_lock(directory):
        if not path.exists():
            template = read_json(Path(__file__).parent / 'catalog.json')
            atomic_json(path, validate_catalog(template))
    return path


def catalog(directory=None):
    return validate_catalog(read_json(ensure_catalog(directory)))


class ConflictError(ValueError):
    pass


def save_catalog(config, expected, directory=None):
    directory = Path(directory or data_home())
    validate_catalog(config)
    path = ensure_catalog(directory)
    with state_lock(directory):
        current = validate_catalog(read_json(path))
        if expected != revision(current):
            raise ConflictError('The catalog changed. Reload it before saving again.')
        atomic_json(directory / 'catalog.json', config)
    return revision(config)

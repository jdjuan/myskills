"""Manual, reviewed updates for standalone GitHub skills. No timers or dependencies."""
from datetime import datetime, timezone
import difflib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import stat
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import unicodedata

import storage
from portable import linked

MAX_FILE = 5 * 1024 * 1024
MAX_TOTAL = 30 * 1024 * 1024
MAX_FILES = 500
REPO = re.compile(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z')
SHA = re.compile(r'[a-f0-9]{40}\Z')
TOKEN = re.compile(r'[a-f0-9]{32}\Z')
SKIP_FILES = {'usage.jsonl', '.DS_Store'}
SKIP_DIRS = {'.git', '__pycache__', '.venv', 'node_modules'}


def now():
    return datetime.now(timezone.utc).isoformat()


def relative_path(value, empty=False):
    """Use portable paths, including on Windows; never normalize traversal away."""
    if not isinstance(value, str) or len(value) > 1000:
        raise ValueError('Choose a valid repository folder.')
    if value == '' and empty:
        return ''
    parts = value.split('/')
    for part in parts:
        if (part in ('', '.', '..') or part[-1:] in (' ', '.') or
                re.search(r'[<>:"\\|?*\x00-\x1f]', part) or
                re.fullmatch(r'(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', part)):
            raise ValueError('The skill contains an unsafe or unsupported file path.')
    return value


def source_spec(repository, path=None, ref=''):
    if not isinstance(repository, str) or not REPO.fullmatch(repository) or any(p in ('.', '..') for p in repository.split('/')):
        raise ValueError('Use a GitHub repository in owner/repository format.')
    if path is not None:
        if path == 'SKILL.md':
            path = ''
        elif isinstance(path, str) and path.endswith('/SKILL.md'):
            path = path[:-9]
        relative_path(path, empty=True)
    if not isinstance(ref, str) or len(ref) > 200 or any(ord(c) < 32 for c in ref):
        raise ValueError('Choose a valid branch or tag.')
    return {'repository': repository, 'path': path, 'ref': ref}


def ignored(name):
    parts = PurePosixPath(name).parts
    return any(p in SKIP_DIRS for p in parts) or parts[-1] in SKIP_FILES


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('The source moved. Update its GitHub repository reference first.')


def download(url, maximum):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != 'https' or parsed.hostname not in ('api.github.com', 'raw.githubusercontent.com'):
        raise ValueError('Unsupported download source.')
    request = urllib.request.Request(url, headers={'User-Agent': 'SkillDashboard', 'Accept': 'application/vnd.github+json'})
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=30) as response:
            body = response.read(maximum + 1)
        if len(body) > maximum:
            raise ValueError('The skill exceeds the download size limit.')
        return body
    except urllib.error.HTTPError as error:
        if error.code in (403, 429):
            raise ValueError('GitHub limited this request. Try again later; no files were changed.') from error
        if error.code == 404:
            raise ValueError('GitHub source not found or private. Check the repository, folder and branch.') from error
        raise ValueError('GitHub could not provide this update. Try again later.') from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise ValueError('Could not reach GitHub. Check your connection and try again.') from error


def github_json(endpoint):
    try:
        return json.loads(download('https://api.github.com' + endpoint, 12 * 1024 * 1024))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError('GitHub returned an invalid response.') from error


def manifest(folder):
    """Hash every supported skill file; usage logs are deliberately separate."""
    folder = Path(folder)
    if linked(folder) or not folder.is_dir():
        raise ValueError('The skill folder must be a regular directory.')
    result, total = {}, 0
    for parent, dirs, files in os.walk(folder, followlinks=False):
        for name in dirs + files:
            if name in {'.git', '.venv', 'node_modules'}:
                raise ValueError('This skill includes a development checkout or dependency folder. Update it with its original tool.')
            path = Path(parent) / name
            if linked(path):
                raise ValueError('Linked files inside a skill cannot be updated safely.')
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            path = Path(parent) / name
            relative = path.relative_to(folder).as_posix()
            if ignored(relative):
                continue
            relative_path(relative)
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE:
                raise ValueError('Unsupported file type or file larger than 5 MB in this skill.')
            total += info.st_size
            if total > MAX_TOTAL or len(result) >= MAX_FILES:
                raise ValueError('The skill is too large to update safely.')
            body = path.read_bytes()
            result[relative] = {'hash': hashlib.sha256(body).hexdigest(),
                                'executable': bool(info.st_mode & 0o111) if os.name != 'nt' else False}
    if 'SKILL.md' not in result:
        raise ValueError('The skill must contain SKILL.md.')
    return result


def content_equal(left, right):
    # Executable modes are not meaningful on Windows.
    return left == right


class Updater:
    def __init__(self, directory, roots, lookup, mutation_lock=None):
        self.directory = Path(directory)
        self.roots = roots
        self.lookup = lookup
        self.lock = threading.RLock()
        self.mutation_lock = mutation_lock or threading.RLock()
        self.busy = False
        self.message = ''
        self.thread = None
        self.state = storage.read_json(self.directory / 'skill-updates.json', {})
        if not isinstance(self.state, dict):
            self.state = {}
        self.recover()

    def save(self):
        storage.atomic_json(self.directory / 'skill-updates.json', self.state)

    def key(self, item):
        return hashlib.sha256('\n'.join(sorted(str(p) for p in self.targets(item))).encode()).hexdigest()

    def targets(self, item):
        if item.get('source') != 'Mine' or item.get('plugin') or item.get('origin', {}).get('kind') != 'installed':
            raise ValueError('Choose an installed standalone skill.')
        trusted = [Path(root).resolve() for root, kind in self.roots() if kind == 'Personal']
        result = []
        for raw in item.get('copies', []):
            path = Path(raw).resolve()
            root = next((root for root in trusted if path != root and root in path.parents), None)
            if root is None or '.system' in path.relative_to(root).parts or path == self.directory.resolve() or path in self.directory.resolve().parents:
                raise ValueError('System and external skill folders cannot be updated here.')
            cursor = path
            while cursor != root:
                if linked(cursor):
                    raise ValueError('The skill moved to a linked location. Check its installation.')
                cursor = cursor.parent
            if path not in result:
                result.append(path)
        if not result:
            raise ValueError('The installed skill folder is missing.')
        return sorted(result, key=str)

    def record(self, item):
        return self.state.get(self.key(item), {})

    def describe(self, item):
        try:
            record = self.record(item)
            source = record.get('source') or source_spec(item['origin']['source'], item['origin'].get('skillPath'), item['origin'].get('ref', ''))
            return {key: record[key] for key in ('status', 'lastChecked', 'message', 'review', 'backup', 'installedCommit') if key in record} | {'source': source}
        except (ValueError, OSError, KeyError):
            return {'status': 'unavailable', 'message': 'This installation needs a valid GitHub source and regular skill folder.'}

    def status(self):
        with self.lock:
            return {'busy': self.busy, 'message': self.message}

    def submit(self, action, items, token=None):
        if action not in ('check', 'apply', 'restore') or not items:
            raise ValueError('Choose a skill and an update action.')
        if action in ('apply', 'restore') and (len(items) != 1 or not isinstance(token, str) or not TOKEN.fullmatch(token)):
            raise ValueError('Review this update first.')
        with self.lock:
            if self.busy:
                raise ValueError('Another skill update operation is running. Wait for it to finish.')
            self.busy = True
            self.message = 'Checking installed skills…' if action == 'check' else 'Applying skill update…' if action == 'apply' else 'Restoring skill backup…'
            self.thread = threading.Thread(target=self.run, args=(action, [item['id'] for item in items], token), daemon=True)
            self.thread.start()
        return self.status()

    def run(self, action, identifiers, token):
        errors = 0
        cache = {}
        try:
            for identifier in identifiers:
                item = None
                try:
                    item = self.lookup(identifier)
                    if item.get('customized'):
                        raise ValueError('This skill is in My skills. Its customizations are not updated automatically.')
                    if action == 'check':
                        self.check(item, cache)
                    elif action == 'apply':
                        self.apply(item, token)
                    else:
                        self.restore(item, token)
                except Exception as error:
                    errors += 1
                    if item:
                        with self.lock:
                            try:
                                key = self.key(item)
                                record = self.state.setdefault(key, {})
                                record.update(status='error', message=str(error))
                                record.pop('review', None)
                                self.save()
                            except (ValueError, OSError):
                                pass
                    self.message = str(error)
        finally:
            with self.lock:
                if not errors:
                    self.message = 'Checks complete.' if action == 'check' else 'Skill updated. A backup is available.' if action == 'apply' else 'Previous version restored.'
                elif len(identifiers) > 1:
                    self.message = f'Checks complete. {errors} skill(s) need attention; open their details.'
                self.busy = False

    def set_source(self, item, repository, path, ref):
        source = source_spec(repository, path, ref)
        if source['path'] is None:
            raise ValueError('Enter the repository folder; leave it empty for its root.')
        with self.lock:
            if self.busy:
                raise ValueError('Wait for the running update operation to finish.')
            record = self.state.setdefault(self.key(item), {})
            record['source'] = source
            record.pop('review', None)
            record.pop('baseline', None)
            record.update(status='unchecked', message='Source saved. Check for updates to compare files.')
            self.save()
        return self.describe(item)

    def remote(self, source, item, cache):
        repo, ref = source['repository'], source['ref']
        cache_key = (repo, ref)
        if cache_key not in cache:
            branch = ref or github_json(f'/repos/{repo}')['default_branch']
            commit = github_json(f'/repos/{repo}/commits/{urllib.parse.quote(branch, safe="")}')
            sha = commit.get('sha', '')
            if not SHA.fullmatch(sha):
                raise ValueError('GitHub returned an invalid commit.')
            tree = github_json(f'/repos/{repo}/git/trees/{sha}?recursive=1')
            if tree.get('truncated') or not isinstance(tree.get('tree'), list):
                raise ValueError('GitHub could not provide the complete repository tree. Use the update prompt for this source.')
            cache[cache_key] = (branch, sha, tree['tree'])
        branch, sha, tree = cache[cache_key]
        folder = source['path']
        if folder is None:
            candidates = [entry['path'][:-9] if entry['path'] != 'SKILL.md' else '' for entry in tree
                          if entry.get('type') == 'blob' and (entry.get('path') == 'SKILL.md' or entry.get('path', '').endswith('/SKILL.md'))]
            names = {item['name'], *(Path(p).name for p in item['copies'])}
            matches = [path for path in candidates if PurePosixPath(path).name in names]
            if len(matches) == 1:
                folder = matches[0]
            elif len(candidates) == 1:
                folder = candidates[0]
            else:
                raise ValueError('The original skill folder is ambiguous. Set its repository folder in Source first.')
        relative_path(folder, empty=True)
        prefix = folder + '/' if folder else ''
        entries, total, folded = [], 0, {}
        for entry in tree:
            path = entry.get('path', '')
            if not path.startswith(prefix) or path == folder:
                continue
            name = path[len(prefix):]
            relative_path(name)
            if ignored(name):
                continue
            if entry.get('type') == 'tree':
                continue
            if entry.get('type') != 'blob' or entry.get('mode') not in ('100644', '100755'):
                raise ValueError('The upstream skill contains a link or submodule; it cannot be updated safely.')
            if not SHA.fullmatch(entry.get('sha', '')) or type(entry.get('size')) is not int or not 0 <= entry['size'] <= MAX_FILE:
                raise ValueError('The upstream skill contains an unsupported file.')
            total += entry['size']
            if total > MAX_TOTAL or len(entries) >= MAX_FILES:
                raise ValueError('The upstream skill exceeds the update size limit.')
            for index in range(1, len(name.split('/')) + 1):
                part = '/'.join(name.split('/')[:index])
                canonical = unicodedata.normalize('NFC', part).casefold()
                if canonical in folded and folded[canonical] != part:
                    raise ValueError('The upstream skill contains conflicting file names.')
                folded[canonical] = part
            entries.append((name, entry))
        if not any(name == 'SKILL.md' for name, _ in entries):
            raise ValueError('No SKILL.md exists in that repository folder.')
        return {'repository': repo, 'path': folder, 'ref': branch}, sha, entries

    def check(self, item, cache=None):
        targets = self.targets(item)
        current = manifest(targets[0])
        if any(manifest(path) != current for path in targets[1:]):
            raise ValueError('The installed copies differ. Keep customized copies in My skills before updating.')
        key = self.key(item)
        with self.lock:
            record = self.state.setdefault(key, {})
            record.update(lastChecked=now(), status='checking', message='Checking GitHub…')
            record.pop('review', None)
            self.save()
            baseline = record.get('baseline')
            source = record.get('source') or source_spec(item['origin']['source'], item['origin'].get('skillPath'), item['origin'].get('ref', ''))
        if baseline is not None and not content_equal(current, baseline):
            with self.lock:
                record.update(status='customized', message='Local customizations found. Move this skill to My skills to keep them.')
                self.save()
            return
        source, commit, entries = self.remote(source, item, cache if cache is not None else {})
        if baseline is not None and record.get('installedCommit') == commit and source == record.get('source'):
            with self.lock:
                record.update(status='current', message='Up to date.')
                self.save()
            return
        token = secrets.token_hex(16)
        work = self.directory / 'skill-update-reviews' / token
        payload = work / 'files'
        payload.mkdir(parents=True)
        try:
            for name, entry in entries:
                url = f'https://raw.githubusercontent.com/{source["repository"]}/{commit}/' + urllib.parse.quote((source['path']+'/' if source['path'] else '')+name, safe='/')
                body = download(url, MAX_FILE)
                if len(body) != entry['size'] or hashlib.sha1(b'blob '+str(len(body)).encode()+b'\0'+body).hexdigest() != entry['sha']:
                    raise ValueError('Downloaded files did not match the reviewed GitHub commit.')
                path = payload / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(body)
                path.chmod(0o755 if entry['mode'] == '100755' else 0o644)
            (payload / 'SKILL.md').read_text(encoding='utf-8')
            proposed = manifest(payload)
            changes = self.diff(targets[0], payload, current, proposed)
            with self.lock:
                record['source'] = source
                if not changes:
                    record.update(status='current', message='Up to date.', baseline=current, installedCommit=commit)
                else:
                    plan = {'token': token, 'created': time.time(), 'targets': [str(p) for p in targets],
                            'current': current, 'proposed': proposed, 'source': source, 'commit': commit,
                            'firstReview': baseline is None, 'changes': changes}
                    storage.atomic_json(work / 'review.json', plan)
                    record.update(status='available', message='Changes available. Review before updating.', review=token)
                self.save()
            if not changes:
                shutil.rmtree(work)
        except Exception:
            shutil.rmtree(work, ignore_errors=True)
            raise

    def diff(self, before, after, current, proposed):
        changes, budget = [], 400000
        for name in sorted(current.keys() | proposed.keys()):
            if current.get(name) == proposed.get(name):
                continue
            kind = 'added' if name not in current else 'removed' if name not in proposed else 'modified'
            detail = ''
            try:
                old = (before / name).read_text(encoding='utf-8') if name in current else ''
                new = (after / name).read_text(encoding='utf-8') if name in proposed else ''
                if '\0' in old + new:
                    raise UnicodeError()
                detail = ''.join(difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True), fromfile='Installed/'+name, tofile='Upstream/'+name))
            except UnicodeError:
                detail = 'Binary file changed.'
            limit = min(budget, 50000)
            shortened = len(detail) > limit
            detail = detail[:limit]
            budget -= len(detail)
            changes.append({'path': name, 'kind': kind, 'diff': detail, 'shortened': shortened})
        return changes

    def review(self, item, token):
        with self.lock:
            if not isinstance(token, str) or not TOKEN.fullmatch(token) or self.record(item).get('review') != token:
                raise ValueError('This review is no longer current. Check for updates again.')
        plan = storage.read_json(self.directory / 'skill-update-reviews' / token / 'review.json')
        if not plan or time.time() - plan['created'] > 86400:
            with self.lock:
                record = self.record(item)
                record.pop('review', None)
                record.update(status='unchecked', message='This review expired. Check for updates again.')
                self.save()
            raise ValueError('This review expired. Check for updates again.')
        if plan['targets'] != [str(p) for p in self.targets(item)]:
            raise ValueError('The skill installation changed. Check for updates again.')
        return plan

    def apply(self, item, token):
        with self.mutation_lock:
            if self.lookup(item['id']).get('customized'):
                raise ValueError('This skill is now in My skills. Check its customizations first.')
            plan = self.review(item, token)
            payload = self.directory / 'skill-update-reviews' / token / 'files'
            if manifest(payload) != plan['proposed']:
                raise ValueError('The downloaded update changed. Check for updates again.')
            targets = self.targets(item)
            if any(manifest(path) != plan['current'] for path in targets):
                raise ValueError('Local files changed after this review. Check again; nothing was overwritten.')
            record = self.record(item)
            if record.get('baseline') is not None and plan['current'] != record['baseline']:
                raise ValueError('Local customizations found. Move this skill to My skills.')
            backup = self.replace(item, payload, plan['current'], plan['proposed'])
            self.commit_record(item, backup, dict(status='current', message='Updated. A recoverable backup is available.',
                                                 baseline=plan['proposed'], installedCommit=plan['commit'], source=plan['source'], backup=backup))

    def commit_record(self, item, token, values):
        work = self.directory / 'skill-update-backups' / token
        journal = storage.read_json(work / 'transaction.json')
        with self.lock:
            record = self.record(item)
            record.update(values)
            record.pop('review', None)
            try:
                self.save()
            except BaseException:
                self.rollback(journal)
                self.state[self.key(item)] = journal['previousRecord']
                raise
        self.finish(token)

    def replace(self, item, payload, expected, proposed):
        """Stage beside each target, journal, then swap. Never extract remote paths."""
        token = secrets.token_hex(16)
        work = self.directory / 'skill-update-backups' / token
        work.mkdir(parents=True)
        targets = self.targets(item)
        journal = {'phase': 'preparing', 'key': self.key(item), 'expected': expected, 'proposed': proposed,
                   'previousRecord': dict(self.record(item)), 'targets': []}
        storage.atomic_json(work / 'transaction.json', journal)
        try:
            for index, target in enumerate(targets):
                if any((target / name).exists() for name in SKIP_DIRS):
                    raise ValueError('This skill includes a development checkout or dependency folder. Update it with its original tool.')
                stage = target.parent / ('.skill-dashboard-update-' + token + '-' + str(index))
                old = target.parent / ('.skill-dashboard-old-' + token + '-' + str(index))
                backup = work / str(index)
                journal['targets'].append({'target': str(target), 'stage': str(stage), 'old': str(old), 'backup': str(backup)})
                storage.atomic_json(work / 'transaction.json', journal)
                shutil.copytree(target, backup, symlinks=True)
                shutil.copytree(payload, stage)
                self.preserve_usage(target, stage)
            storage.atomic_json(work / 'transaction.json', journal)
            if any(manifest(path) != expected for path in targets):
                raise ValueError('Local files changed during preparation. Check again.')
            journal['phase'] = 'swapping'
            storage.atomic_json(work / 'transaction.json', journal)
            for entry in journal['targets']:
                target, stage, old = map(Path, (entry['target'], entry['stage'], entry['old']))
                self.preserve_usage(target, stage)
                os.replace(target, old)
                try:
                    os.replace(stage, target)
                except BaseException:
                    os.replace(old, target)
                    raise
            journal['phase'] = 'installed'
            storage.atomic_json(work / 'transaction.json', journal)
            # Old folders stay until state is saved; the journal can recover a crash.
            return token
        except BaseException:
            self.rollback(journal)
            raise

    def preserve_usage(self, source, destination):
        for path in Path(source).rglob('usage.jsonl'):
            if linked(path):
                raise ValueError('Usage logs cannot be links.')
            relative = path.relative_to(source)
            target = Path(destination) / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() and os.path.samefile(path, target):
                continue
            if target.exists():
                target.unlink()
            try:
                os.link(path, target)
            except OSError:
                shutil.copy2(path, target)

    def finish(self, token):
        work = self.directory / 'skill-update-backups' / token
        journal = storage.read_json(work / 'transaction.json')
        journal['phase'] = 'complete'
        storage.atomic_json(work / 'transaction.json', journal)
        for entry in journal['targets']:
            shutil.rmtree(entry['old'], ignore_errors=True)
            shutil.rmtree(entry['stage'], ignore_errors=True)

    def rollback(self, journal):
        for entry in reversed(journal['targets']):
            target, old = Path(entry['target']), Path(entry['old'])
            if old.exists():
                if target.exists():
                    self.preserve_usage(target, old)
                    shutil.rmtree(target)
                os.replace(old, target)
            shutil.rmtree(entry['stage'], ignore_errors=True)

    def recover(self):
        for path in (self.directory / 'skill-update-backups').glob('*/transaction.json'):
            journal = storage.read_json(path)
            if not journal or journal.get('phase') == 'complete':
                continue
            # Journals are local trusted state, but validate all installation targets again.
            paths = [entry['target'] for entry in journal['targets']]
            if not paths:
                continue
            # A target can temporarily be absent after its first rename.
            for raw in paths:
                target = Path(raw)
                roots = [Path(root).resolve() for root, kind in self.roots() if kind == 'Personal']
                if not any(root in target.parents and '.system' not in target.relative_to(root).parts for root in roots):
                    raise ValueError('Invalid skill update recovery target.')
            record = self.state.get(journal['key'], {})
            if journal['phase'] == 'installed' and record.get('backup') == path.parent.name:
                self.finish(path.parent.name)
            else:
                self.rollback(journal)
                journal['phase'] = 'complete'
                storage.atomic_json(path, journal)

    def restore(self, item, token):
        with self.mutation_lock:
            record = self.record(item)
            if record.get('backup') != token:
                raise ValueError('Choose the available backup for this skill.')
            work = self.directory / 'skill-update-backups' / token
            journal = storage.read_json(work / 'transaction.json')
            targets = self.targets(item)
            if not journal or [str(p) for p in targets] != [e['target'] for e in journal['targets']]:
                raise ValueError('The backup does not match this installation.')
            if any(manifest(path) != journal['proposed'] for path in targets):
                raise ValueError('Local files changed after updating. Keep those customizations before restoring.')
            payload = work / '0'
            if manifest(payload) != journal['expected']:
                raise ValueError('The backup changed and cannot be restored safely.')
            new_backup = self.replace(item, payload, journal['proposed'], journal['expected'])
            previous = journal.get('previousRecord', {})
            self.commit_record(item, new_backup, dict(status='restored', message='Previous version restored.',
                                                     baseline=previous.get('baseline'), installedCommit=previous.get('installedCommit'), backup=new_backup))

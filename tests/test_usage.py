import concurrent.futures
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import server
from usage import read_usage, record


class UsageTests(unittest.TestCase):
    def test_concurrent_invocations_and_invalid_records(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            manifest = folder / 'SKILL.md'
            manifest.write_text('Fixture')
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(record, [manifest] * 20))
            with (folder / 'usage.jsonl').open('a') as stream:
                stream.write('broken\nnull\n{}\n')
                stream.write(json.dumps({'id': 'invalid', 'timestamp': '2026-10-15'}) + '\n')
            usage = read_usage([folder])
            self.assertEqual(usage['count'], 20)
            self.assertEqual(len(usage['recent']), 10)
            self.assertEqual(usage['lastUsed'], max(usage['recent']))

    def test_discovery_aggregates_identical_copies_without_double_counting(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / 'a'; first.mkdir()
            second = root / 'b'; second.mkdir()
            for folder in (first, second):
                (folder / 'SKILL.md').write_text('---\nname: fixture\n---\nBody')
            record(first / 'SKILL.md')
            (second / 'usage.jsonl').write_bytes((first / 'usage.jsonl').read_bytes())
            record(second / 'SKILL.md')
            with patch.object(server, 'ROOTS', [(root, 'Personal')]):
                entries = server.skills()
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]['usage']['count'], 2)
            self.assertEqual(read_usage([root]), {'count': 0, 'lastUsed': None, 'recent': []})

    def test_symlink_logs_are_not_read_or_written(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'SKILL.md').write_text('Fixture')
            target = root / 'private'; target.write_text('untouched')
            try:
                (root / 'usage.jsonl').symlink_to(target)
            except OSError:
                self.skipTest('Creating symlinks is unavailable to this user.')
            with self.assertRaises(OSError):
                record(root / 'SKILL.md')
            self.assertEqual(read_usage([root])['count'], 0)
            self.assertEqual(target.read_text(), 'untouched')

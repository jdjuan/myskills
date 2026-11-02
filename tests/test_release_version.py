import unittest
from scripts.release_version import release_version


class ReleaseVersionTests(unittest.TestCase):
    def test_first_release_uses_minimum(self):
        self.assertEqual(release_version('0.1.3', [], []), '0.1.3')

    def test_each_new_commit_advances_without_a_source_version_bump(self):
        self.assertEqual(release_version('0.1.3', ['v0.1.3', 'v0.1.9', 'v0.1.10'], []), '0.1.11')

    def test_explicit_minor_version_and_prerelease_tags(self):
        self.assertEqual(release_version('0.2.0', ['v0.1.9', 'v9.0.0-beta', 'other'], []), '0.2.0')

    def test_branch_tag_and_retry_share_version(self):
        self.assertEqual(release_version('0.1.3', ['v0.1.3'], ['v0.1.3']), '0.1.3')
        self.assertEqual(release_version('0.1.3', ['v0.1.4'], ['v0.1.4']), '0.1.4')
        self.assertEqual(release_version('0.1.3', ['v0.1.3'], ['v0.1.3'], 'v0.1.3'), '0.1.3')

    def test_reject_invalid_and_mismatched_versions(self):
        for args in [('invalid', [], []), ('0.1.3', [], [], 'v0.1.2'),
                     ('0.1.3', ['v0.1.2'], ['v0.1.2'])]:
            with self.assertRaises(ValueError):
                release_version(*args)

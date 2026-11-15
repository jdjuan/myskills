import json
from pathlib import Path
import struct
import tempfile
import unittest

from server import skill_icon


class SkillIconTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'skill'
        (self.root / 'agents').mkdir(parents=True)
        (self.root / 'assets').mkdir()
        self.metadata = self.root / 'agents/openai.yaml'
        self.metadata.write_text('interface:\n  icon_large: "./assets/icon.png"\n')
        self.png = self.root / 'assets/icon.png'
        self.png.write_bytes(b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR' + struct.pack('>II', 64, 64) + bytes(9))

    def test_reads_local_png_and_credit_and_refreshes(self):
        credit = self.png.with_suffix('.credit.json')
        credit.write_text(json.dumps({'author': 'Artist', 'authorUrl': 'https://example.com/artist',
                                     'sourceUrl': 'https://example.com/icon'}))
        first = skill_icon(self.root)
        self.assertTrue(first['icon'].startswith('data:image/png;base64,'))
        self.assertEqual(first['iconCredit']['author'], 'Artist')
        self.png.write_bytes(self.png.read_bytes() + b'changed')
        self.assertNotEqual(first['icon'], skill_icon(self.root)['icon'])

    def test_rejects_escaping_paths_invalid_images_and_large_files(self):
        outside = self.root.parent / 'outside.png'
        outside.write_bytes(self.png.read_bytes())
        for path in ('../outside.png', str(outside), './assets/icon.svg'):
            with self.subTest(path=path):
                self.metadata.write_text('interface:\n  icon_large: ' + json.dumps(path) + '\n')
                self.assertEqual(skill_icon(self.root), {})
        self.metadata.write_text('interface:\n  icon_large: "./assets/icon.png"\n')
        for data in (b'<svg/>', b'x' * (512 * 1024 + 1)):
            self.png.write_bytes(data)
            self.assertEqual(skill_icon(self.root), {})

    def test_credit_cannot_inject_executable_links(self):
        self.png.with_suffix('.credit.json').write_text(json.dumps({
            'author': '<img onerror=alert(1)>', 'authorUrl': 'javascript:alert(1)',
            'sourceUrl': 'https://example.com/icon'}))
        result = skill_icon(self.root)
        self.assertIn('icon', result)
        self.assertNotIn('iconCredit', result)

    def test_metadata_must_be_in_interface(self):
        self.metadata.write_text('other:\n  icon_large: "./assets/icon.png"\n')
        self.assertEqual(skill_icon(self.root), {})


class SuggestedIconTests(unittest.TestCase):
    def test_matches_semantically_and_credits_fluent(self):
        from server import suggest_icon
        result = suggest_icon('security-audit', 'Audit an application for vulnerabilities and protect secrets.')
        self.assertTrue(result['icon'].startswith('data:image/png;base64,'))
        self.assertIn('fluentui-emoji', result['iconCredit']['sourceUrl'])
        self.assertTrue(result['iconSuggested'])

    def test_leaves_unrelated_skills_without_icon(self):
        from server import suggest_icon
        self.assertEqual(suggest_icon('zzqx', 'Qwrt vbnm plokij.'), {})

    def test_prefers_unused_icons_over_repeating_one(self):
        from server import suggest_icon
        used = {}
        first = suggest_icon('video-editor', 'Edit video clips into a reel.', used)
        repeated = {name: 20 for name in used}
        second = suggest_icon('video-editor', 'Edit video clips into a reel.', repeated)
        self.assertNotEqual(first['icon'], second['icon'])


class IconPickerTests(unittest.TestCase):
    def test_writes_and_resets_icon_metadata(self):
        import server
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            (folder / 'agents').mkdir()
            (folder / 'agents/openai.yaml').write_text('interface:\n  display_name: "X"\n  icon_large: "./old.png"\nother: 1\n')
            server.write_icon_metadata(folder, './assets/icon.png')
            text = (folder / 'agents/openai.yaml').read_text()
            self.assertIn('display_name: "X"', text)
            self.assertNotIn('old.png', text)
            self.assertEqual(text.count('./assets/icon.png'), 2)
            self.assertIn('other: 1', text)
            server.write_icon_metadata(folder, None)
            self.assertNotIn('icon_', (folder / 'agents/openai.yaml').read_text())

    def test_creates_interface_when_missing(self):
        import server
        with tempfile.TemporaryDirectory() as temp:
            server.write_icon_metadata(Path(temp), './assets/icon.png')
            self.assertTrue((Path(temp) / 'agents/openai.yaml').read_text().startswith('interface:\n'))

    def test_icon_choices_are_inline_pngs(self):
        import server
        choices = server.icon_choices()
        self.assertGreater(len(choices), 200)
        self.assertTrue(all(c['icon'].startswith('data:image/png;base64,') for c in choices))

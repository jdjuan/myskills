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

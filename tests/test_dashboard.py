import copy
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server


def entry(name, source='Mine', plugin=''):
    return dict(name=name, source=source, plugin=plugin)


class CatalogTests(unittest.TestCase):
    def test_product_order_without_any_installed_skills(self):
        names=['product-build','explore-design','product-definition','product-setup','product-design','google-oauth-integration']
        entries=[entry(name) for name in names]
        groups=server.purpose_groups(entries)
        self.assertTrue(any(g['id']=='product' and g['show_steps'] for g in groups))
        ordered=sorted(entries,key=lambda s:s['groupOrder']['product'])
        core=[s['name'] for s in ordered if s['name'].startswith('product-')]
        self.assertEqual(core,['product-definition','product-setup','product-design','product-build'])
        self.assertEqual([s['steps']['product'] for s in ordered],[1,2,3,3,4,4])

    def test_configuration_controls_order_and_membership(self):
        config=copy.deepcopy(server.read_catalog())
        product=next(g for g in config['groups'] if g['id']=='product')
        product['skills'].reverse()
        entries=[entry('product-definition'),entry('product-build')]
        server.purpose_groups(entries,config)
        self.assertLess(entries[1]['groupOrder']['product'],entries[0]['groupOrder']['product'])

    def test_plugin_origin_takes_precedence_over_personal_rules(self):
        entries=[entry('sentry','External','sentry'),entry('sentry')]
        server.purpose_groups(entries)
        self.assertEqual(entries[0]['groups'],['plugin-sentry'])
        self.assertEqual(entries[1]['groups'],['operations'])

    def test_new_hyperframes_skills_join_video_and_compact_toolkit(self):
        entries=[entry('hyperframes-future-tool'),entry('youtube-upload')]
        server.purpose_groups(entries)
        self.assertEqual(entries[0]['groups'],['marketing-video'])
        self.assertEqual(entries[0]['compactSection'],'hyperframes')
        self.assertGreater(entries[0]['compactOrder'],entries[1]['compactOrder'])

    def test_personal_articles_and_new_skills_have_a_home(self):
        entries=[entry('publish-article'),entry('new-unclassified-skill'),entry('imagegen','System')]
        server.purpose_groups(entries)
        self.assertEqual(entries[0]['groups'],['articles'])
        self.assertEqual(entries[1]['groups'],['other'])
        self.assertEqual(entries[2]['groups'],[])

    def test_catalog_has_unique_groups_and_seo_precedes_video(self):
        groups=server.read_catalog()['groups']
        ids=[g['id'] for g in groups]
        self.assertEqual(len(ids),len(set(ids)))
        self.assertEqual(ids.index('marketing-video'),ids.index('marketing-seo')+1)


class DeletionTests(unittest.TestCase):
    def test_only_disposable_skill_moves_to_trash(self):
        with tempfile.TemporaryDirectory() as folder:
            home=Path(folder).resolve();root=home/'skills';root.mkdir()
            own=root/'sample';own.mkdir();(own/'SKILL.md').write_text('---\nname: sample\ndescription: Fixture\n---\nTest')
            system=root/'.system'/'protected';system.mkdir(parents=True);(system/'SKILL.md').write_text('Protected')
            linked=root/'linked';linked.symlink_to(system,target_is_directory=True)
            with patch.object(server,'ROOTS',[(root,'Personal')]),patch.object(Path,'home',return_value=home):
                entries=server.skills();mine=next(s for s in entries if s['name']=='sample')
                protected=next(s for s in entries if s['source']=='System')
                self.assertFalse(server.safe_folder(root))
                self.assertFalse(server.safe_folder(linked))
                with self.assertRaises(ValueError):server.trash_skill(protected['id'])
                server.trash_skill(mine['id'])
                self.assertFalse(own.exists())
                self.assertTrue((system/'SKILL.md').exists())
                self.assertEqual(len(list((home/'.Trash').glob('sample-skill-*/SKILL.md'))),1)


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
        self.thread=threading.Thread(target=self.http.serve_forever,daemon=True);self.thread.start()
        self.base='http://127.0.0.1:'+str(self.http.server_port)
    def tearDown(self):
        self.http.shutdown();self.http.server_close();self.thread.join()
    def test_snapshot_includes_catalog_and_never_serves_source_files(self):
        with patch.object(server,'skills',return_value=[entry('product-build')]):
            with urllib.request.urlopen(self.base+'/api/skills') as response:data=json.load(response)
        self.assertEqual(data['skills'][0]['groups'],['product'])
        self.assertIn('compactSections',data)
        self.assertIn('display',data)
        with self.assertRaises(urllib.error.HTTPError) as error:urllib.request.urlopen(self.base+'/catalog.json')
        self.assertEqual(error.exception.code,404)
        error.exception.close()
    def test_delete_rejects_cross_origin_and_missing_token(self):
        for headers in [{'Origin':'https://example.com','X-Library-Token':server.TOKEN},{'Origin':self.base}]:
            request=urllib.request.Request(self.base+'/api/delete',data=b'{"id":"does-not-exist"}',headers=headers)
            with self.assertRaises(urllib.error.HTTPError) as error:urllib.request.urlopen(request)
            self.assertEqual(error.exception.code,403)
            error.exception.close()


if __name__=='__main__':unittest.main()

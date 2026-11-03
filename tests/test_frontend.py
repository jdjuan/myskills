"""Exercise update/reload behavior from the shipped buildless frontend."""
import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which('node'), 'Node is optional for frontend behavior checks')
class FrontendUpdateTests(unittest.TestCase):
    def test_update_reload_and_draft_protection(self):
        html = (Path(__file__).resolve().parents[1] / 'dist/index.html').read_text()
        code = html[html.index('let pendingUiUpdate=false;'):html.index('async function place(')]
        harness = r'''
const assert=require('node:assert/strict');
let reloads=0,renders=0,notices=0,fail=false;
const elements=new Map();
const $=id=>{if(!elements.has(id))elements.set(id,{open:false,disabled:false,
 querySelector(selector){return selector==='.update-notice'?this.notice:{prepend:()=>{this.notice=true;notices++}}}});return elements.get(id)};
const node=()=>({setAttribute(){}});
const document={querySelector:()=>[...elements.values()].find(x=>x.open),activeElement:{matches:()=>false}};
const location={reload:()=>reloads++};
const notify=()=>{};
const render=()=>renders++;
let data={uiRevision:'new-ui',token:'new-token',catalogRevision:'new-catalog',skills:[],groups:[]};
const fetch=async()=>{if(fail)throw Error('Restarting');return {ok:true,json:async()=>data}};
let loading=false,loaded=true,uiRevision='old-ui',snapshot='',token='old-token',catalogRevision='old-catalog',skills=[],groups=[],compactSections=[];
'''
        cases = r'''
(async()=>{
 // A changed interface reloads even with Settings open.
 $('settingsDialog').open=true;
 await load(true);assert.equal(reloads,1);assert.equal(token,'new-token');
 $('settingsDialog').open=false;pendingUiUpdate=false;reloads=0;
 // Drafts survive polling and still get the replacement server's mutation token.
 $('organizeDialog').open=true;
 await load(true);await load(true);
 assert.equal(reloads,0);assert.equal(notices,1);assert.equal(renders,0);
 assert.equal(token,'new-token');assert.equal(catalogRevision,'old-catalog');
 $('organizeDialog').open=false;reloadUpdatedUi();assert.equal(reloads,1);
 // Backup inputs and delete confirmations also finish before reloading.
 for(const id of ['backupDialog','deleteDialog']){
  reloads=0;$(id).open=true;await load(true);assert.equal(reloads,0);
  $(id).open=false;reloadUpdatedUi();assert.equal(reloads,1);
 }
 // The restart's temporary connection failure cannot reload an unavailable page.
 reloads=0;pendingUiUpdate=false;fail=true;
 assert.equal(await load(true),false);assert.equal(reloads,0);assert.equal(loading,false);
 fail=false;uiRevision='new-ui';snapshot='';$('settingsDialog').open=true;
 await load(true);assert.equal(renders,0);assert.equal(reloads,0);
 $('settingsDialog').open=false;
 await load(true);assert.equal(renders,1);
})().catch(error=>{console.error(error);process.exitCode=1});
'''
        result = subprocess.run(['node'], input=harness + code + cases, text=True,
                                capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

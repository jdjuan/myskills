"""Exercise update controls using the actual shipped frontend functions."""
from pathlib import Path
import shutil
import subprocess
import unittest


@unittest.skipUnless(shutil.which('node'), 'Node optional locally')
class SkillUpdateFrontendTests(unittest.TestCase):
    def test_review_acknowledgment_and_prepared_token_submission(self):
        html=(Path(__file__).resolve().parents[1]/'dist/index.html').read_text(encoding='utf-8')
        code=html[html.index('let skillUpdateState='):html.index('function showInstalledDetails(')]
        events=html[html.index("$('checkAllSkillUpdates').onclick="):html.index("$('refresh').onclick=")]
        harness=r'''
const assert=require('node:assert/strict');
const elements=new Map();
function node(tag,cls,text){return {tag,cls,text,children:[],append(...children){this.children.push(...children)},setAttribute(){}}}
const $=id=>{if(!elements.has(id))elements.set(id,{open:false,checked:false,hidden:false,disabled:false,children:[],replaceChildren(){this.children=[]},append(child){this.children.push(child)},showModal(){this.open=true},close(){this.open=false}});return elements.get(id)};
let requests=[],loads=0,renders=0,notifications=[],reject=false;
const review={token:'abc',firstReview:true,source:{repository:'owner/repo',ref:'main',path:'skills/example'},commit:'a'.repeat(40),changes:[{kind:'modified',path:'SKILL.md',diff:'-old\n+new'}]};
const api=async(path,payload)=>{requests.push({path,payload});if(reject)throw Error('Expired');return payload.action==='review'?review:{busy:true,message:'Started'}};
const nice=s=>s.name;
const notify=message=>notifications.push(message);
const load=async()=>loads++;
const render=()=>renders++;
let catalogRevision='catalog',activeSource='Installed';
'''
        cases=r'''
(async()=>{
const skill={id:'example',name:'Example',skillUpdate:{review:'abc'}};
await skillUpdateAction(skill);
assert.equal($('skillUpdateDialog').open,true);
assert.equal($('applySkillUpdate').disabled,true);
assert.equal($('approveFirstUpdate').hidden,false);
assert.equal($('firstUpdateNotice').hidden,false);
assert.equal($('skillUpdateDiffs').children.length,1);
assert.match($('skillUpdateSource').href,/\/tree\/aaaa/);
$('firstUpdateApproved').checked=true;$('firstUpdateApproved').onchange();
assert.equal($('applySkillUpdate').disabled,false);
await startSkillUpdate('apply',skill,{review:updateReview.review.token});
assert.deepEqual(requests.at(-1).payload,{action:'apply',id:'example',review:'abc'});
assert.equal($('skillUpdateDialog').open,false);assert.equal(loads,1);
skillUpdateState={busy:false};review.firstReview=false;
await openSkillUpdateReview(skill);
assert.equal($('applySkillUpdate').disabled,false);
assert.equal($('approveFirstUpdate').hidden,true);
reject=true;await startSkillUpdate('apply',skill,{review:'expired'});
assert.equal(notifications.at(-1),'Expired');
assert.equal($('skillUpdateDialog').open,true);
assert.equal(updateActionLabel({skillUpdate:{status:'customized'}}),'Keep in My skills');
})().catch(error=>{console.error(error);process.exitCode=1});
'''
        result=subprocess.run(['node'],input=harness+code+events+cases,text=True,capture_output=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)

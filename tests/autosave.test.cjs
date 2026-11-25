const {test}=require('node:test');
const assert=require('node:assert/strict');
const {DraftAutosave}=require('../static/autosave.js');
function timers(){let id=0;const jobs=new Map();return {jobs,setTimeout(fn){jobs.set(++id,fn);return id;},clearTimeout(i){jobs.delete(i);},run(){const values=[...jobs.values()];jobs.clear();values.forEach(fn=>fn());}};}
function doc(id='a'){return {id,version:1,draft:{sales:[{name:''}]},results:[],confirmedVersions:[]};}
function response(body){return {version:body.version+1,results:['new-result'],confirmedVersions:[1]};}
test('debounces rapid edits to a single write',async()=>{
  const clock=timers(),sent=[];const save=new DraftAutosave({timers:clock,send:async(id,b)=>{sent.push(b);return response(b);}});
  const d=doc();save.bind(d);for(const v of ['a','ab','abc']){d.draft.sales[0].name=v;save.changed();}
  assert.equal(clock.jobs.size,1);await save.flush();assert.equal(sent.length,1);assert.equal(sent[0].draft.sales[0].name,'abc');assert.equal(save.dirty,false);
});
test('new keystrokes during save survive and use the next version',async()=>{
  let finish;const sent=[];const save=new DraftAutosave({timers:timers(),send:(id,b)=>{sent.push(b);return sent.length===1?new Promise(r=>finish=r):Promise.resolve(response(b));}});
  const d=doc();save.bind(d);d.draft.sales[0].name='first';save.changed();const pending=save.flush();
  d.draft.sales[0].name='latest';save.changed();finish(response(sent[0]));await pending;
  assert.equal(sent.length,2);assert.equal(sent[0].draft.sales[0].name,'first');assert.equal(sent[1].version,2);assert.equal(d.draft.sales[0].name,'latest');assert.equal(d.version,3);assert.equal(save.dirty,false);
});
test('manual save joins auto save without duplicate requests',async()=>{
  let finish,count=0;const save=new DraftAutosave({timers:timers(),send:(id,b)=>{count++;return new Promise(r=>finish=()=>r(response(b)));}});
  save.bind(doc());save.changed();const a=save.flush(),b=save.flush();assert.equal(a,b);finish();await a;assert.equal(count,1);
});
test('network failure keeps inputs dirty and retries',async()=>{
  const clock=timers();let count=0;const save=new DraftAutosave({timers:clock,send:async(id,b)=>{if(++count===1)throw new Error('offline');return response(b);}});
  const d=doc();save.bind(d);d.draft.sales[0].name='kept';save.changed();await assert.rejects(save.flush());assert.equal(save.dirty,true);assert.equal(d.draft.sales[0].name,'kept');assert.equal(clock.jobs.size,1);
  await save.flush();assert.equal(save.dirty,false);
});
test('409 never overwrites or automatically retries; local values retained',async()=>{
  let count=0;const clock=timers();const save=new DraftAutosave({timers:clock,send:async()=>{count++;throw Object.assign(new Error('conflict'),{status:409});}});
  const d=doc();save.bind(d);save.changed();await assert.rejects(save.flush());d.draft.sales[0].name='more';save.changed();await assert.rejects(save.flush());
  assert.equal(count,1);assert.equal(save.dirty,true);assert.equal(clock.jobs.size,0);assert.equal(d.draft.sales[0].name,'more');assert.equal(save.phase,'conflict');
});
test('auth and validation errors do not endlessly retry',async()=>{
  for(const status of [400,403]){const clock=timers();const save=new DraftAutosave({timers:clock,send:async()=>{throw Object.assign(new Error('bad'),{status});}});save.bind(doc());save.changed();await assert.rejects(save.flush());assert.equal(clock.jobs.size,0);assert.equal(save.dirty,true);}
});
test('switching document while write in flight is rejected',async()=>{
  let finish;const save=new DraftAutosave({timers:timers(),send:(id,b)=>new Promise(r=>finish=()=>r(response(b)))});save.bind(doc());save.changed();const p=save.flush();assert.throws(()=>save.bind(doc('b')));finish();await p;save.bind(doc('b'));assert.equal(save.doc.id,'b');assert.equal(save.dirty,false);
});
test('clean document does not increment DB version',async()=>{let n=0;const save=new DraftAutosave({send:async()=>{n++;}});save.bind(doc());await save.flush();assert.equal(n,0);});
test('settle stops retry before explicit discard/reload',async()=>{const clock=timers();const save=new DraftAutosave({timers:clock,send:async()=>{throw new Error('offline');}});save.bind(doc());save.changed();await assert.rejects(save.flush());await save.settle();assert.equal(clock.jobs.size,0);save.bind(doc('b'));assert.equal(save.dirty,false);});

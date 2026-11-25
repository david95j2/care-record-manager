const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const source=fs.readFileSync(require.resolve('../static/theme.js'),'utf8');
function fixture(saved,dark=false,denied=false){
  const callbacks={},selector={value:null,addEventListener:(name,fn)=>callbacks.change=fn};
  const state={dataset:{}},media={matches:dark,addEventListener:(name,fn)=>callbacks.media=fn};
  const storage={value:saved,getItem(){if(denied)throw Error('denied');return this.value;},setItem(k,v){if(denied)throw Error('denied');this.value=v;}};
  vm.runInNewContext(source,{localStorage:storage,matchMedia:()=>media,document:{documentElement:state,addEventListener:(name,fn)=>callbacks.ready=fn,getElementById:()=>selector}});
  callbacks.ready();return {state,media,storage,selector,callbacks};
}
test('system dark preference followed on first load',()=>{const f=fixture(null,true);assert.equal(f.state.dataset.theme,'dark');assert.equal(f.selector.value,'system');});
test('explicit light overrides OS dark and persists',()=>{const f=fixture('dark',true);f.selector.value='light';f.callbacks.change();f.callbacks.media();assert.equal(f.state.dataset.theme,'light');assert.equal(f.storage.value,'light');});
test('system changes update theme without reload',()=>{const f=fixture(null,false);f.media.matches=true;f.callbacks.media();assert.equal(f.state.dataset.theme,'dark');});
test('blocked browser storage does not break theme switching',()=>{const f=fixture(null,false,true);f.selector.value='dark';f.callbacks.change();assert.equal(f.state.dataset.theme,'dark');});
test('invalid saved theme falls back to system',()=>{const f=fixture('bad',false);assert.equal(f.selector.value,'system');assert.equal(f.state.dataset.theme,'light');});

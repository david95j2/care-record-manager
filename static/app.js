const $ = id => document.getElementById(id);
let current = null, dirty = false, busy = false;
const show = text => { $('message').textContent = text; };
const saver=new DraftAutosave({
  send:(id,data)=>api(`/api/documents/${id}/save`,data),
  onState:state=>{
    dirty=state.dirty;
    const label={idle:'문서 선택 전',pending:'입력 변경 · 저장 대기',saving:'서버에 저장 중',saved:'서버 저장 완료',conflict:'충돌 · 입력 보존 중',error:'미저장 · 다시 시도 필요'};
    $('save-state').textContent=label[state.phase];$('save-state').dataset.phase=state.phase;
    if(state.error)show(state.error.status===409?'다른 화면에서 수정되었거나 저장 응답을 받지 못했습니다. 현재 입력을 내보낸 뒤 다시 불러와 비교하세요. 덮어쓰지 않았습니다.':`${state.error.message} 입력은 이 화면에 남아 있습니다. 페이지를 닫지 마세요.`);
  },
  onSaved:(_,upToDate)=>{if(upToDate){updateResults();show('');}}
});
const changed=()=>saver.changed();
async function exclusive(fn){
  if(busy)throw new Error('다른 작업이 끝날 때까지 기다려 주세요.');
  busy=true;$('workspace').inert=true;
  try{return await fn();}finally{busy=false;$('workspace').inert=false;}
}
async function api(path, data) {
  const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),15000);
  let response;
  try{response = await fetch(path, {...(data===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)}),signal:controller.signal});}
  catch{throw new Error('서버 연결을 확인하세요.');}finally{clearTimeout(timer);}
  const value = await response.json();
  if (!response.ok) {const error=new Error(value.error||'요청 실패');error.status=response.status;throw error;}
  return value;
}
function action(id, fn) { $(id).addEventListener('click', async () => { $(id).disabled = true; try { await fn(); } catch(e){show(e.message);} finally {$(id).disabled=false;} }); }
function element(tag, text) { const node = document.createElement(tag); if(text !== undefined) node.textContent=text; return node; }
function field(parent, text, value, onChange, type='text') {
  const label=element('label',text), input=element('input'); input.type=type;
  if(type==='checkbox') input.checked=value===true; else input.value=value ?? '';
  if(type==='number'){input.min='0';input.step='1';}
  input.addEventListener('input',()=>{onChange(type==='checkbox'?input.checked:type==='number'?(input.value===''?null:Number(input.value)):input.value);changed();});
  label.append(input);parent.append(label);return input;
}
function select(parent,text,value,options,onChange){
  const label=element('label',text), input=element('select');
  for(const [v,t] of options){const o=element('option',t);o.value=v;input.append(o);}input.value=value??'';
  input.addEventListener('change',()=>{onChange(input.value);changed();});label.append(input);parent.append(label);
}
function updateResults(){
  if(!current)return;
  const meta=$('version-info');if(meta)meta.textContent=`저장 버전 ${current.version} · 확정 버전 ${current.confirmedVersions.join(', ')||'없음'}`;
  document.querySelectorAll('[data-result-id]').forEach(node=>{
    const index=current.draft.sales.findIndex(s=>s.id===node.dataset.resultId),r=current.results[index];
    node.textContent=r?`${{numeric_match:'입력된 숫자 일치 · 최종 판정 전',review_required:'확인 필요',not_provided:'반납 · 미실시'}[r.status]}\n실제 ${r.actualMinutes??'-'}분 / 인정 ${r.recognizedMinutes??'-'}분\n${r.findings.join('\n')}`:'아직 대조하지 않았습니다.';
    node.dataset.status=r?.status||'';
  });
}
function render(){
  const container=$('review');container.replaceChildren();
  if(!current)return;
  $('title').textContent=current.name;
  const meta=element('p');meta.id='version-info';container.append(meta);
  container.append(element('small','대조 결과는 마지막 서버 저장 기준입니다. 입력 중에는 이전 결과가 표시될 수 있습니다.'));
  current.draft.sales.forEach((sale,index)=>{
    const card=element('article');card.className='sale';card.append(element('h2',`매출 ${index+1}`));
    const fields=element('div');fields.className='fields';card.append(fields);
    field(fields,'매출 승인번호',sale.approval,v=>sale.approval=v);
    field(fields,'대응 계획 시간 (분)',sale.planMinutes,v=>sale.planMinutes=v,'number');
    select(fields,'결제 구분',sale.paymentKind,[['normal','일반'],['bulk','일괄결제'],['unknown','예외·확인 필요']],v=>sale.paymentKind=v);
    field(fields,'반납 · 서비스 미실시',sale.returned,v=>sale.returned=v,'checkbox');
    card.append(element('h2','실제 서비스 구간'));
    card.append(element('small','동일 매출 안의 구간만 입력하세요. 휴게는 제외하고, 다른 결제는 별도 매출로 입력합니다.'));
    sale.intervals.forEach((interval,i)=>{
      const grid=element('div');grid.className='fields';
      field(grid,`구간 ${i+1} 시작`,interval.start,v=>interval.start=v,'datetime-local');
      field(grid,`구간 ${i+1} 종료`,interval.end,v=>interval.end=v,'datetime-local');
      const remove=element('button','구간 삭제');remove.onclick=()=>{sale.intervals.splice(i,1);changed();render();};grid.append(remove);card.append(grid);
    });
    const add=element('button','같은 매출에 구간 추가');add.onclick=()=>{sale.intervals.push({start:'',end:''});changed();render();};card.append(add);
    card.append(element('h2','기록지 상단 값'));
    const record=element('div');record.className='fields';card.append(record);
    field(record,'기록지 승인번호',sale.record.approval,v=>sale.record.approval=v);
    field(record,'기록지 실제 제공시간 (분)',sale.record.minutes,v=>sale.record.minutes=v,'number');
    field(record,'기록지 시작',sale.record.start,v=>sale.record.start=v,'datetime-local');
    field(record,'기록지 종료',sale.record.end,v=>sale.record.end=v,'datetime-local');
    select(record,'소급 체크',sale.record.retroactive===true?'yes':sale.record.retroactive===false?'no':'', [['','미확인'],['yes','체크됨'],['no','체크 안 됨']],v=>sale.record.retroactive=v===''?null:v==='yes');
    const result=element('p');result.className='result';result.dataset.resultId=sale.id;card.append(result);
    select(card,'사용자 최종 판정',sale.decision,[['','미판정·보류'],['accepted','사용자 인정'],['rejected','사용자 불인정'],['not_provided','서비스 미실시']],v=>sale.decision=v);
    const remove=element('button','이 매출 입력 제거');remove.onclick=()=>{if(confirm('초안에서 이 매출을 제거할까요? 기존 확정 버전은 유지됩니다.')){current.draft.sales.splice(index,1);current.results=[];changed();render();}};card.append(remove);container.append(card);
  });
  updateResults();
}
async function list(){
  const docs=await api('/api/documents');$('documents').replaceChildren();
  if(!docs.length)$('documents').append(element('p','등록된 문서가 없습니다.'));
  for(const doc of docs){const b=element('button',doc.name);b.dataset.docId=doc.id;b.onclick=async()=>{try{await exclusive(async()=>{await saver.flush();await open(doc.id);});}catch(e){show(e.message);}};$('documents').append(b);}
}
async function open(id){const loaded=await api(`/api/documents/${id}`);saver.bind(loaded);current=loaded;$('original').src=`/api/documents/${id}/original`;document.querySelectorAll('[data-doc-id]').forEach(n=>n.setAttribute('aria-current',String(n.dataset.docId===id)));render();}
async function enter(){const me=await api('/api/me');$('login').hidden=true;$('workspace').hidden=false;$('logout').hidden=false;$('navigation').hidden=false;$('users-nav').hidden=!me.administrator;await list();}
$('login').onsubmit=async e=>{e.preventDefault();const form=new FormData(e.target);try{await api('/api/login',Object.fromEntries(form));e.target.reset();show('');await enter();}catch(err){show(err.message);}};
$('upload').onchange=async e=>{const f=e.target.files[0];if(!f)return;try{await exclusive(async()=>{await saver.flush();if(f.size>20*1024*1024)throw new Error('20MB 이하 파일을 선택하세요.');const data=await new Promise((resolve,reject)=>{const r=new FileReader();r.onload=()=>resolve(r.result.split(',')[1]);r.onerror=reject;r.readAsDataURL(f);});const added=await api('/api/documents',{name:f.name,data});await list();await open(added.id);show('원본 저장 완료. AI 미연결: 검토 항목을 직접 입력하세요.');});}catch(err){show(err.message);}finally{e.target.value='';}};
action('logout',()=>exclusive(async()=>{if(window.salesUnsaved&&!confirm('매출 검토의 미저장 입력을 버리고 로그아웃할까요?'))return;await saver.flush();await api('/api/logout',{});location.reload();}));
action('reload',()=>exclusive(async()=>{if(!current)return;await saver.settle();if(dirty&&!confirm('미저장 입력을 버리고 서버 값을 불러올까요? 필요하면 취소 후 현재 입력을 내보내세요.'))return;await open(current.id);}));
action('add',async()=>{if(!current)throw new Error('먼저 문서를 선택하세요.');current.draft.sales.push({id:crypto.randomUUID(),approval:'',planMinutes:null,paymentKind:'normal',returned:false,intervals:[{start:'',end:''}],record:{approval:'',start:'',end:'',minutes:null,retroactive:null},decision:''});changed();render();});
action('save',async()=>{if(!current)throw new Error('문서를 선택하세요.');await saver.flush();show('서버 저장 및 대조 완료.');});
action('confirm',()=>exclusive(async()=>{if(!current)throw new Error('문서를 선택하세요.');await saver.flush();if(!confirm('현재 매출별 사용자 판정을 확정 버전으로 보관할까요?'))return;const result=await api(`/api/documents/${current.id}/confirm`,{version:current.version});current.confirmedVersions=result.confirmedVersions;updateResults();show('확정 버전 저장 완료. 이후 수정은 새 버전으로 남습니다.');}));
action('export',async()=>{if(!current)throw new Error('문서를 선택하세요.');const blob=new Blob([JSON.stringify({documentId:current.id,version:current.version,draft:current.draft},null,2)],{type:'application/json'});const url=URL.createObjectURL(blob),a=element('a');a.href=url;a.download=`검토초안-${current.id}.json`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);});
window.addEventListener('online',()=>saver.flush().catch(()=>{}));
document.addEventListener('visibilitychange',()=>{if(document.hidden&&dirty)saver.flush().catch(()=>{});});
window.addEventListener('beforeunload',e=>{if(dirty){e.preventDefault();e.returnValue='';}});
enter().catch(()=>{});

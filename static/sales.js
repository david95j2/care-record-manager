// Original evidence is never edited. DOM textContent avoids rendering workbook HTML.
let salesPage=1, selectedImport='', selectedSale=null, salesSaving=false;
window.salesUnsaved=false;
const kindLabel=s=>s.returned?'반납 · 미실시':s.paymentKind==='bulk'?'일괄결제 · 소급 확인':s.paymentKind==='normal'?'정상결제':'예외 · 확인 필요';
function canLeaveSale(){return !window.salesUnsaved||confirm('매출 검토의 미저장 입력을 버릴까요?');}
async function loadImports(){
  const imports=await api('/api/sales-imports'),select=$('sales-imports');select.replaceChildren();
  for(const item of imports){const option=element('option',`${item.name} · 원본 ${item.rowCount}행`);option.value=item.id;select.append(option);}
  if(!imports.some(i=>i.id===selectedImport))selectedImport=imports[0]?.id||'';
  select.value=selectedImport;await loadSales();
}
async function loadSales(){
  $('sales-body').replaceChildren();
  $('sales-original').hidden=!selectedImport;
  if(!selectedImport){$('sales-count').textContent='가져온 파일 없음';return;}
  $('sales-original').href=`/api/sales-imports/${selectedImport}/original`;
  const params=new URLSearchParams({page:salesPage,q:$('sales-query').value,category:$('sales-category').value});
  const data=await api(`/api/sales-imports/${selectedImport}?${params}`);
  $('sales-count').textContent=`원본 ${data.total}행 · ${salesPage}/${Math.max(1,Math.ceil(data.total/50))} 페이지`;
  $('sales-prev').disabled=salesPage<=1;$('sales-next').disabled=salesPage*50>=data.total;
  for(const row of data.rows){
    const s=row.summary,tr=element('tr');
    for(const value of [row.row_number,s.beneficiary,s.worker,s.approval,s.paymentAt,`${s.start||'-'} / ${s.end||'-'}`,kindLabel(s)])tr.append(element('td',value));
    const td=element('td'),button=element('button','검토');button.onclick=async()=>{try{if(!canLeaveSale())return;selectedSale=await api(`/api/sales-rows/${row.id}`);window.salesUnsaved=false;renderSale();$('sales-detail').scrollIntoView({behavior:'smooth'});}catch(e){show(e.message);}};td.append(button);tr.append(td);$('sales-body').append(tr);
  }
}
function saleInput(parent,label,value,set,type='text'){
  const wrap=element('label',label),input=element('input');input.type=type;input.value=value??'';
  if(type==='number'){input.min='0';input.step='1';}
  input.oninput=()=>{set(type==='number'?(input.value===''?null:Number(input.value)):input.value);window.salesUnsaved=true;};
  wrap.append(input);parent.append(wrap);
}
function saleSelect(parent,label,value,options,set){
  const wrap=element('label',label),select=element('select');
  for(const [v,t] of options){const o=element('option',t);o.value=v;select.append(o);}select.value=value??'';
  select.onchange=()=>{set(select.value);window.salesUnsaved=true;};wrap.append(select);parent.append(wrap);
}
function renderSale(){
  const target=$('sales-detail');target.replaceChildren();if(!selectedSale)return;
  const s=selectedSale.summary,d=selectedSale.draft;
  target.append(element('h2',`원본 ${selectedSale.sheet} · ${selectedSale.row_number}행 / ${s.approval}`));
  target.append(element('p',`${kindLabel(s)} · 원본 시작 ${s.start||'-'}, 종료 ${s.end||'-'} · 원본 제공시간 합계 ${s.reportedHours||'-'}`));
  target.append(element('p',`원본 서비스 항목 합산: ${s.sourceActualMinutes??'확인 필요'}분 · 30분 단위 인정: ${s.returned?'미실시':s.sourceRecognizedMinutes??'확인 필요'}분 (다른 행과 합산하지 않음)`));
  target.append(element('p','원본에 연도가 없는 시각은 임의 추정하지 않습니다. 실제 날짜를 확인하여 입력하세요. 검토 입력은 아래 저장 버튼으로 보관합니다. 매출의 원본 구분·반납 여부는 변경되지 않습니다.'));
  for(const warning of s.flags)target.append(element('p',warning));
  const raw=element('details'),title=element('summary','원본 전체 열 보기 (대상년월 포함, 판정에는 미사용)');raw.append(title);
  const table=element('table');
  selectedSale.raw.forEach((cell,i)=>{const row=element('tr');row.append(element('th',`${i+1}. ${selectedSale.headers[selectedSale.sheet][i]||''}`),element('td',String(cell.value??'')));table.append(row);});raw.append(table);target.append(raw);
  const form=element('form'),fields=element('div');fields.className='fields';form.append(fields);
  saleInput(fields,'대응 계획 시간 (분)',d.planMinutes,v=>d.planMinutes=v,'number');
  d.intervals.forEach((interval,i)=>{
    saleInput(fields,`실제 구간 ${i+1} 시작`,interval.start,v=>interval.start=v,'datetime-local');
    saleInput(fields,`실제 구간 ${i+1} 종료`,interval.end,v=>interval.end=v,'datetime-local');
    const remove=element('button',`구간 ${i+1} 삭제`);remove.type='button';remove.onclick=()=>{d.intervals.splice(i,1);window.salesUnsaved=true;renderSale();};fields.append(remove);
  });
  const add=element('button','휴게 제외 · 같은 매출 구간 추가');add.type='button';add.onclick=()=>{d.intervals.push({start:'',end:''});window.salesUnsaved=true;renderSale();};fields.append(add);
  saleInput(fields,'기록지 시작',d.record.start,v=>d.record.start=v,'datetime-local');
  saleInput(fields,'기록지 종료',d.record.end,v=>d.record.end=v,'datetime-local');
  saleInput(fields,'기록지 제공시간 (분)',d.record.minutes,v=>d.record.minutes=v,'number');
  saleInput(fields,'기록지 승인번호',d.record.approval,v=>d.record.approval=v);
  saleSelect(fields,'기록지 소급 체크',d.record.retroactive===true?'yes':d.record.retroactive===false?'no':'',[['','미확인'],['yes','체크됨'],['no','체크 안 됨']],v=>d.record.retroactive=v===''?null:v==='yes');
  saleSelect(fields,'사용자 판정 (검토 이력으로 저장)',d.decision,s.returned?[['','미판정'],['not_provided','미실시']]:[['','미판정'],['accepted','인정'],['rejected','불인정']],v=>d.decision=v);
  const result=selectedSale.result;
  form.append(element('p',`마지막 저장 결과: ${{not_provided:'미실시',numeric_match:'입력 숫자 일치',review_required:'확인 필요'}[result.status]} · 실제 ${result.actualMinutes??'-'}분 / 인정 ${result.recognizedMinutes??'-'}분\n${result.findings.join('\n')}`));
  const save=element('button','검토 저장·대조');save.className='primary';form.append(save);
  form.onsubmit=async e=>{e.preventDefault();if(salesSaving)return;salesSaving=true;form.inert=true;try{selectedSale=await api(`/api/sales-rows/${selectedSale.id}`,{version:selectedSale.version,draft:d});window.salesUnsaved=false;renderSale();show('검토 이력을 저장했습니다. 원본 행은 그대로 유지됩니다.');}catch(err){show(err.message);}finally{salesSaving=false;form.inert=false;}};
  target.append(form);
}
async function users(){
  const rows=await api('/api/users');$('user-list').replaceChildren();
  for(const row of rows)$('user-list').append(element('li',`${row.username} · ${row.administrator?'관리자':'직원'}`));
}
document.querySelectorAll('[data-page]').forEach(button=>button.onclick=async()=>{
  try{await exclusive(async()=>{if(!canLeaveSale())return;await saver.flush();window.salesUnsaved=false;selectedSale=null;$('sales-detail').replaceChildren();
    const page=button.dataset.page;if(page==='users-page')await users();if(page==='sales-page')await loadImports();
    for(const id of ['workspace','sales-page','users-page'])$(id).hidden=id!==page;
  });}catch(e){show(e.message);}
});
$('user-create').onsubmit=async e=>{e.preventDefault();const form=e.target,data=Object.fromEntries(new FormData(form));if(data.password!==data.confirm){show('비밀번호 확인이 일치하지 않습니다.');return;}form.inert=true;try{await api('/api/users',{username:data.username,password:data.password});form.reset();await users();show('직원 계정을 추가했습니다.');}catch(err){show(err.message);}finally{form.inert=false;}};
$('sales-upload').onchange=async e=>{const file=e.target.files[0];if(!file)return;try{if(!canLeaveSale())return;if(file.size>20*1024*1024)throw new Error('20MB 이하 파일을 선택하세요.');const data=await new Promise((resolve,reject)=>{const r=new FileReader();r.onload=()=>resolve(r.result.split(',')[1]);r.onerror=reject;r.readAsDataURL(file);});show('엑셀 원본을 가져오는 중입니다.');const result=await api('/api/sales-imports',{name:file.name,data});selectedImport=result.id;salesPage=1;window.salesUnsaved=false;selectedSale=null;$('sales-detail').replaceChildren();await loadImports();show(result.duplicate?'이미 보관된 동일 파일을 열었습니다.':'원본 행 가져오기 완료');}catch(err){show(err.message);}finally{e.target.value='';}};
$('sales-imports').onchange=async e=>{if(!canLeaveSale()){e.target.value=selectedImport;return;}selectedImport=e.target.value;salesPage=1;window.salesUnsaved=false;selectedSale=null;$('sales-detail').replaceChildren();try{await loadSales();}catch(err){show(err.message);}};
action('sales-search',async()=>{salesPage=1;await loadSales();});
action('sales-prev',async()=>{if(salesPage>1)salesPage--;await loadSales();});
action('sales-next',async()=>{salesPage++;await loadSales();});
window.addEventListener('beforeunload',e=>{if(window.salesUnsaved){e.preventDefault();e.returnValue='';}});

const diaryState={day:null,week:null,loadedWeek:null,entries:new Map(),loading:false,error:'',request:0,daysSignature:'',storageError:false};
const diaryDraftKey='desk.diary.drafts.v1';
const diaryDateFormat=new Intl.DateTimeFormat('en-GB',{day:'numeric',month:'long',year:'numeric'});
const diaryWeekFormat=new Intl.DateTimeFormat('en-GB',{day:'numeric',month:'short'});
function diaryISO(date){return `${date.getFullYear()}-${String(date.getMonth()+1).padStart(2,'0')}-${String(date.getDate()).padStart(2,'0')}`;}
function diaryToday(){return diaryISO(new Date());}
function diaryOffset(day,offset){const date=new Date(day+'T12:00:00');date.setDate(date.getDate()+offset);return diaryISO(date);}
function diaryMonday(day){return diaryOffset(day,-((new Date(day+'T12:00:00').getDay()+6)%7));}
function diaryDays(){return Array.from({length:7},(_,index)=>diaryOffset(diaryState.week,index));}
function diaryDraftsPending(){return [...diaryState.entries.values()].some(entry=>entry.dirty||entry.saving);}
function persistDiaryDrafts(){
  try {
    const drafts=[...diaryState.entries].filter(([,e])=>e.dirty||e.saving).map(([day,e])=>({day,content:e.content,revision:e.revision}));
    if(drafts.length)window.sessionStorage.setItem(diaryDraftKey,JSON.stringify(drafts));else window.sessionStorage.removeItem(diaryDraftKey);
    diaryState.storageError=false;
  }catch(_){diaryState.storageError=true;}
}
function restoreDiaryDrafts(){
  try {
    const rows=JSON.parse(window.sessionStorage.getItem(diaryDraftKey)||'[]');
    if(!Array.isArray(rows))return;
    for(const row of rows){
      if(!row||typeof row.day!=='string'||!/^\d{4}-\d{2}-\d{2}$/.test(row.day)||!Number.isFinite(Date.parse(row.day+'T12:00:00'))||typeof row.content!=='string'||row.content.length>12000||!Number.isSafeInteger(row.revision)||row.revision<0)continue;
      diaryState.entries.set(row.day,{content:row.content,revision:row.revision,dirty:true,saving:false,error:'',conflict:null});
    }
  }catch(_){diaryState.storageError=true;}
}
function renderDiaryDays(){
  const today=diaryToday(),days=diaryDays();
  const signature=JSON.stringify([diaryState.day,today,days.map(day=>[day,!!diaryState.entries.get(day)?.content.trim()])]);
  if(signature===diaryState.daysSignature)return;diaryState.daysSignature=signature;
  const root=$('#diary-days');root.replaceChildren();
  for(const [index,day] of days.entries()){
    const filled=!!diaryState.entries.get(day)?.content.trim();
    const item=button('','diary-day'+(day===diaryState.day?' selected':'')+(day===today?' today':'')+(filled?' filled':''),safeAction(()=>selectDiaryDay(day)));
    item.dataset.day=day;item.setAttribute('aria-pressed',String(day===diaryState.day));
    item.setAttribute('aria-label',diaryDateFormat.format(new Date(day+'T12:00:00'))+(filled?', entry added':', no entry'));
    item.append(el('span','diary-weekday',['Mon','Tue','Wed','Thu','Fri','Sat','Sun'][index]),el('strong','',new Date(day+'T12:00:00').getDate()),el('small','',day===today?'Today':filled?'Added':'—'));
    root.append(item);
  }
}
function renderDiaryStatus(){
  if(state.view!=='diary'||!diaryState.day)return;
  const entry=diaryState.entries.get(diaryState.day),status=$('#diary-status');
  status.textContent=!entry?(diaryState.loading?'Loading…':'No data'):entry.saving?'Saving…':entry.error?'Not saved':entry.dirty?'Unsaved changes':entry.revision?'Saved':'Ready to write';
  status.classList.toggle('diary-save-error',!!entry?.error);
  $('#diary-count').textContent=`${entry?.content.length||0} / 12,000`;
  $('#diary-progress').textContent=`${diaryDays().filter(day=>diaryState.entries.get(day)?.content.trim()).length} of 7 days filled`;
  const error=$('#diary-error');error.replaceChildren();
  if(diaryState.error)error.append(el('span','',diaryState.error),button('Retry loading','text-button',safeAction(loadDiary)));
  if(entry?.error){
    error.append(el('span','',entry.error));
    if(entry.conflict){
      error.append(button('Keep my version','text-button',()=>resolveDiaryConflict(true)),button('Use saved version','text-button',()=>resolveDiaryConflict(false)));
    }else error.append(button('Retry saving','text-button',()=>saveDiary(diaryState.day)));
  }
  if(diaryState.storageError&&entry?.dirty)error.append(el('span','','Browser draft recovery is unavailable. Wait for your entry to save before closing.'));
  error.classList.toggle('hidden',!error.children.length);
  $('#diary-save').disabled=!entry||entry.saving||!entry.dirty||!!entry.conflict;
  $('#diary-template').disabled=!entry;
  $('#diary-copy-week').disabled=diaryState.loadedWeek!==diaryState.week;
}
function renderDiary(){
  if(state.view!=='diary'||!diaryState.day)return;
  const end=diaryOffset(diaryState.week,6);
  $('#diary-week-title').textContent=diaryWeekFormat.format(new Date(diaryState.week+'T12:00:00'))+' — '+diaryDateFormat.format(new Date(end+'T12:00:00'));
  $('#diary-date').value=diaryState.day;
  $('#diary-entry-date').textContent=diaryDateFormat.format(new Date(diaryState.day+'T12:00:00'));
  const entry=diaryState.entries.get(diaryState.day),editor=$('#diary-editor');
  if(editor.dataset.day!==diaryState.day||(!entry?.dirty&&document.activeElement!==editor))editor.value=entry?.content||'';
  editor.dataset.day=diaryState.day;editor.disabled=!entry;
  renderDiaryDays();renderDiaryStatus();
}
async function loadDiary(){
  if(!diaryState.day){diaryState.day=diaryToday();diaryState.week=diaryMonday(diaryState.day);}
  const week=diaryState.week,request=++diaryState.request;
  diaryState.loading=true;diaryState.error='';renderDiary();
  try{
    const result=await api('/api/diary?week='+week);
    const remote=new Map(result.entries.map(entry=>[entry.entry_date,entry]));
    for(let i=0;i<7;i++){
      const day=diaryOffset(week,i),saved=remote.get(day)||{content:'',revision:0},local=diaryState.entries.get(day);
      if(local?.saving)continue;
      if(local?.dirty&&local.content!==saved.content)continue;
      diaryState.entries.set(day,{content:saved.content,revision:saved.revision,dirty:false,saving:false,error:'',conflict:null});
    }
    persistDiaryDrafts();
    if(request===diaryState.request)diaryState.loadedWeek=week;
  }catch(error){if(request===diaryState.request)diaryState.error=error.message;}
  finally{if(request===diaryState.request){diaryState.loading=false;renderDiary();}}
}
function diaryChanged(){
  const day=diaryState.day,entry=diaryState.entries.get(day);if(!entry)return;
  entry.content=$('#diary-editor').value;entry.dirty=true;entry.error=entry.conflict?entry.error:'';
  persistDiaryDrafts();clearTimeout(entry.timer);
  if(!entry.conflict)entry.timer=setTimeout(()=>saveDiary(day),700);
  renderDiaryDays();renderDiaryStatus();
}
function saveDiary(day){
  const entry=diaryState.entries.get(day);if(!entry)return Promise.resolve();
  clearTimeout(entry.timer);entry.timer=null;
  if(entry.saving)return entry.promise;
  if(!entry.dirty||entry.conflict)return Promise.resolve();
  const content=entry.content;entry.saving=true;entry.error='';renderDiaryStatus();
  entry.promise=(async()=>{
    try{
      const result=await api('/api/diary/'+day,'PATCH',{content,revision:entry.revision});
      entry.revision=result.entry.revision;entry.dirty=entry.content!==content;
    }catch(error){entry.error=error.message;if(error.status===409)entry.conflict=error.details.entry;}
    finally{
      entry.saving=false;entry.promise=null;persistDiaryDrafts();
      if(entry.dirty&&!entry.error)entry.timer=setTimeout(()=>saveDiary(day),100);
      if(state.view==='diary'){renderDiaryDays();renderDiaryStatus();}
    }
  })();return entry.promise;
}
async function selectDiaryDay(day){
  if(!/^\d{4}-\d{2}-\d{2}$/.test(day)||!Number.isFinite(Date.parse(day+'T12:00:00')))return;
  if(diaryState.day)saveDiary(diaryState.day);
  const week=diaryMonday(day);diaryState.day=day;diaryState.week=week;renderDiary();
  if(diaryState.loadedWeek!==week)await loadDiary();
}
function flushDiary(){return Promise.all([...diaryState.entries.keys()].map(saveDiary));}
function resolveDiaryConflict(keepLocal){
  const entry=diaryState.entries.get(diaryState.day);if(!entry?.conflict)return;
  if(!keepLocal&&!window.confirm('Replace your draft with the saved entry from another tab?'))return;
  entry.revision=entry.conflict.revision;
  if(!keepLocal){entry.content=entry.conflict.content;entry.dirty=false;$('#diary-editor').value=entry.content;}
  entry.conflict=null;entry.error='';persistDiaryDrafts();renderDiary();if(keepLocal)return saveDiary(diaryState.day);
}
async function copyDiaryWeek(){
  const text=[`Week ${diaryState.week} — ${diaryOffset(diaryState.week,6)}`,...diaryDays().map(day=>`${diaryDateFormat.format(new Date(day+'T12:00:00'))}\n${diaryState.entries.get(day)?.content||'—'}`)].join('\n\n');
  await copyText(text);toast('Weekly entries copied.');
}
function initDiary(){
  restoreDiaryDrafts();
  $('#diary-prev').replaceChildren(icon('chevronLeft'));$('#diary-next').replaceChildren(icon('chevronRight'));
  $('#diary-prev').addEventListener('click',safeAction(()=>selectDiaryDay(diaryOffset(diaryState.day,-7))));
  $('#diary-next').addEventListener('click',safeAction(()=>selectDiaryDay(diaryOffset(diaryState.day,7))));
  $('#diary-today').addEventListener('click',safeAction(()=>selectDiaryDay(diaryToday())));
  $('#diary-date').addEventListener('change',safeAction(event=>selectDiaryDay(event.target.value)));
  $('#diary-editor').addEventListener('input',diaryChanged);
  $('#diary-editor').addEventListener('keydown',event=>{if((event.ctrlKey||event.metaKey)&&event.key==='Enter'){event.preventDefault();saveDiary(diaryState.day);}});
  $('#diary-save').addEventListener('click',()=>saveDiary(diaryState.day));
  $('#diary-copy-week').addEventListener('click',safeAction(copyDiaryWeek));
  $('#diary-template').addEventListener('click',()=>{
    const editor=$('#diary-editor'),text=(editor.value?'\n\n':'')+'What I did\n\nWhat went well\n\nTomorrow\n';
    if(editor.value.length+text.length>12000)return;
    editor.value+=text;diaryChanged();editor.focus();
  });
  window.addEventListener('pagehide',()=>{persistDiaryDrafts();flushDiary();});
}

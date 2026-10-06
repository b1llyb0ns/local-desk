const taskState = {items: [], loaded: false, editing: null, saving: false, busy: new Set(), filter: 'open'};
function taskToday() {
  const now=new Date();
  return `${now.getFullYear()}-${String(now.getMonth()+1).padStart(2,'0')}-${String(now.getDate()).padStart(2,'0')}`;
}
async function loadTasks() {
  renderTasks();
  try {taskState.items=(await api('/api/tasks')).tasks;taskState.loaded=true;renderTasks();}
  catch(error) {if(state.view==='tasks')$('#tasks-list').replaceChildren(el('p','form-error',error.message),button('Retry','button secondary',safeAction(loadTasks)));}
}
function renderTasks() {
  if(state.view!=='tasks')return;
  const root=$('#tasks-list');root.replaceChildren();
  const open=taskState.items.filter(task=>!task.completed).length;
  $('#tasks-caption').textContent=taskState.loaded?`${open} open · ${taskState.items.length-open} completed`:'Loading…';
  const filters=$('#tasks-filters');filters.replaceChildren();
  for(const [value,label] of [['open','Open'],['all','All'],['completed','Completed']]) {
    const item=button(label,taskState.filter===value?'selected':'',()=>{taskState.filter=value;renderTasks();});
    item.setAttribute('aria-pressed',String(taskState.filter===value));filters.append(item);
  }
  if(!taskState.loaded)return;
  const today=taskToday();
  const rows=taskState.items.filter(task=>taskState.filter==='all'||task.completed===(taskState.filter==='completed'))
    .sort((a,b)=>Number(a.completed)-Number(b.completed)||a.due_date.localeCompare(b.due_date)||a.created_at.localeCompare(b.created_at));
  if(!rows.length)root.append(el('div','empty',taskState.filter==='completed'?'No completed tasks.':taskState.items.length?'No open tasks.':'No tasks yet. Add a task and choose its date.'));
  for(const task of rows) {
    const row=el('article','task-row'+(task.completed?' completed':''));row.dataset.taskId=task.id;
    const check=el('input','task-check');check.type='checkbox';check.checked=task.completed;check.disabled=taskState.busy.has(task.id);
    check.setAttribute('aria-label',(task.completed?'Reopen: ':'Complete: ')+task.title);
    check.addEventListener('change',safeAction(()=>changeTask(task.id,'PATCH',{completed:check.checked})));
    const content=append(el('div','task-content'),el('strong','task-title',task.title));
    if(task.notes)content.append(el('p','task-notes',task.notes));
    const date=el('time','task-date',new Intl.DateTimeFormat(undefined,{day:'numeric',month:'short',year:'numeric'}).format(new Date(task.due_date+'T12:00:00')));date.setAttribute('datetime',task.due_date);
    const timing=append(el('div','task-timing'),date);
    if(!task.completed&&task.due_date<=today)timing.append(el('small',task.due_date<today?'task-overdue':'task-today',task.due_date<today?'Overdue':'Today'));
    const edit=iconButton('note','Edit task',()=>openTask(task));
    const remove=iconButton('trash','Delete task',safeAction(async()=>{if(window.confirm('Delete task “'+task.title+'”?'))await changeTask(task.id,'DELETE',{});}));
    edit.disabled=remove.disabled=taskState.busy.has(task.id);
    root.append(append(row,check,content,timing,append(el('div','task-actions'),edit,remove)));
  }
}
async function changeTask(id,method,body) {
  if(taskState.busy.has(id))return;
  taskState.busy.add(id);renderTasks();
  try {taskState.items=(await api('/api/tasks/'+id,method,body)).tasks;}
  finally {taskState.busy.delete(id);renderTasks();}
}
function taskField(name) {return $('#task-form').elements.namedItem(name);}
function openTask(task=null) {
  if(taskState.saving)return;
  taskState.editing=task?.id||null;
  $('#task-form').reset();
  taskField('title').value=task?.title||'';taskField('due_date').value=task?.due_date||taskToday();taskField('notes').value=task?.notes||'';
  $('#task-form-title').textContent=task?'Edit task':'Add task';$('#task-save').textContent=task?'Save':'Add task';
  $('#task-error').classList.add('hidden');$('#task-modal').showModal();taskField('title').focus();
}
async function saveTask(event) {
  event.preventDefault();if(taskState.saving)return;
  const id=taskState.editing;
  const payload={title:taskField('title').value,due_date:taskField('due_date').value,notes:taskField('notes').value};
  taskState.saving=true;$('#task-save').disabled=true;$('#task-error').classList.add('hidden');
  try {taskState.items=(await api('/api/tasks'+(id?'/'+id:''),id?'PATCH':'POST',payload)).tasks;taskState.loaded=true;taskState.filter='all';$('#task-modal').close();renderTasks();}
  catch(error) {$('#task-error').textContent=error.message;$('#task-error').classList.remove('hidden');}
  finally {taskState.saving=false;$('#task-save').disabled=false;}
}
function initTasks() {
  $('#task-add').addEventListener('click',()=>openTask());$('#task-form').addEventListener('submit',saveTask);
  $('#task-modal').addEventListener('cancel',event=>{if(taskState.saving)event.preventDefault();});
  $('#task-modal').addEventListener('close',()=>{if(!taskState.saving)taskState.editing=null;});
}

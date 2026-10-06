// Local execution of the real frontend with deterministic DOM, fetch and clock doubles.
// No browser, HTTP requests, package operations, or SSH connections are started.
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {runInContext, createContext} from 'node:vm';

const app=(await readFile(new URL('../static/app.js',import.meta.url),'utf8')).replace(/\ninit\(\)\.catch[\s\S]*$/,'');
const desktop=await readFile(new URL('../static/desktop.js',import.meta.url),'utf8');
const diary=await readFile(new URL('../static/diary.js',import.meta.url),'utf8');
const tasks=await readFile(new URL('../static/tasks.js',import.meta.url),'utf8');
const expenses=await readFile(new URL('../static/expenses.js',import.meta.url),'utf8');
const index=await readFile(new URL('../static/index.html',import.meta.url),'utf8');
assert.doesNotMatch(index,/data-view=["']rent["']/,'Rental management has no duplicate navigation page');
assert.match(index,/id=["']rental-alerts["']/,'The rental-alert panel remains available');
assert.match(index,/name=["']lease_end["']/,'Server forms retain rental-expiry editing');
assert.match(index,/name=["']next_charge_date["'] type=["']date["']/,'Expenses allow an optional explicit payment date');
assert.match(index,/data-view=["']expenses["']/,'Expenses has a navigation entry');
assert.doesNotMatch(index,/name=["']price["']/,'Server cost is structured, with the legacy price retained as a hint');
for(const name of ['cost_amount','cost_currency','cost_period'])assert.match(index,new RegExp('name=["\']'+name+'["\']'),'Structured server cost field: '+name);
assert.doesNotMatch(index,/nav-schedule|Weekly/,'Burp navigation has no schedule');
const clone=value=>JSON.parse(JSON.stringify(value));
const gate=()=>{let resolve;const promise=new Promise(done=>{resolve=done;});return{promise,resolve};};

function environment(handler){
  const metrics={created:0,replaced:0},timers=new Map(),requests=[];let timerId=0,now=Date.now();
  class ClockDate extends Date {constructor(...args){super(...(args.length?args:[now]));}static now(){return now;}}
  let document;
  class Text {constructor(value){this.textContent=String(value);this.parentNode=null;}}
  class Element {
    constructor(tag='div',namespaceURI='http://www.w3.org/1999/xhtml'){
      metrics.created++;this.tagName=tag.toUpperCase();this.namespaceURI=namespaceURI;this.childNodes=[];this.parentNode=null;this.attributes={};this.events=new Map();this._classes=new Set();this.value='';this.disabled=false;this.selectionStart=0;this.selectionEnd=0;
      this.dataset=new Proxy({}, {set:(target,key,value)=>{target[key]=String(value);return true;}});
      this.classList={add:(...names)=>names.forEach(name=>this._classes.add(name)),remove:(...names)=>names.forEach(name=>this._classes.delete(name)),contains:name=>this._classes.has(name),toggle:(name,force)=>{const enabled=force??!this._classes.has(name);enabled?this._classes.add(name):this._classes.delete(name);return enabled;}};
    }
    get className(){return[...this._classes].join(' ');}set className(value){this._classes=new Set(String(value).split(/\s+/).filter(Boolean));}
    get children(){return this.childNodes.filter(node=>node instanceof Element);}get firstChild(){return this.childNodes[0]||null;}get firstElementChild(){return this.children[0]||null;}
    get isConnected(){return document.body.contains(this);}
    get textContent(){return this.childNodes.map(node=>node.textContent).join('');}set textContent(value){String(value)===''?this.replaceChildren():this.replaceChildren(new Text(value));}
    setAttribute(name,value){this.attributes[name]=String(value);if(name==='class')this.className=value;if(name==='id')this.id=value;if(name.startsWith('data-'))this.dataset[name.slice(5).replace(/-([a-z])/g,(_,letter)=>letter.toUpperCase())]=value;}
    getAttribute(name){if(name==='class')return this.className;if(name==='id')return this.id;return this.attributes[name]??null;}
    contains(other){return this===other||this.children.some(node=>node.contains(other));}
    detach(node){if(node instanceof Element&&node.contains(document?.activeElement))document.activeElement=document.body;if(node.parentNode){const siblings=node.parentNode.childNodes;siblings.splice(siblings.indexOf(node),1);node.parentNode=null;}}
    insertBefore(node,next){if(!(node instanceof Element||node instanceof Text))node=new Text(node);this.detach(node);const index=next?this.childNodes.indexOf(next):this.childNodes.length;assert.ok(index>=0);this.childNodes.splice(index,0,node);node.parentNode=this;return node;}
    append(...nodes){for(const node of nodes)this.insertBefore(node,null);}prepend(...nodes){for(const node of [...nodes].reverse())this.insertBefore(node,this.firstChild);}
    replaceChildren(...nodes){metrics.replaced++;for(const node of [...this.childNodes])this.detach(node);this.append(...nodes);}
    replaceChild(node,old){this.insertBefore(node,old);this.detach(old);}remove(){this.detach(this);}
    addEventListener(name,callback){if(!this.events.has(name))this.events.set(name,[]);this.events.get(name).push(callback);}
    dispatchEvent(event){for(const callback of this.events.get(event.type)||[])callback({...event,target:this});}
    focus(){document.activeElement=this;}setSelectionRange(start,end){this.selectionStart=start;this.selectionEnd=end;}
    showModal(){this.open=true;}close(){this.open=false;this.dispatchEvent({type:'close'});}
    matches(selector){
      const tag=selector.match(/^[a-z][\w-]*/i)?.[0];if(tag&&this.tagName!==tag.toUpperCase())return false;
      const id=selector.match(/#([\w-]+)/)?.[1];if(id&&this.id!==id)return false;
      for(const match of selector.matchAll(/\.([\w-]+)/g))if(!this.classList.contains(match[1]))return false;
      for(const match of selector.matchAll(/\[([\w-]+)(?:=["']?([^"'\]]+)["']?)?\]/g)){const key=match[1],value=key.startsWith('data-')?this.dataset[key.slice(5).replace(/-([a-z])/g,(_,letter)=>letter.toUpperCase())]:this.getAttribute(key);if(value==null||(match[2]!==undefined&&value!==match[2]))return false;}
      return true;
    }
    querySelectorAll(selector){
      const parts=selector.trim().replaceAll('>',' ').split(/\s+/),matches=[];
      const visit=node=>{for(const child of node.children){if(child.matches(parts.at(-1))){let parent=child.parentNode,index=parts.length-2;while(index>=0&&parent){if(parent.matches(parts[index]))index--;parent=parent.parentNode;}if(index<0)matches.push(child);}visit(child);}};visit(this);return matches;
    }
    querySelector(selector){return this.querySelectorAll(selector)[0]||null;}
  }
  const body=new Element('body');document={body,hidden:false,activeElement:body,addEventListener(){},createElement:tag=>new Element(tag),createElementNS:(ns,tag)=>new Element(tag,ns),querySelector:selector=>body.querySelector(selector),querySelectorAll:selector=>body.querySelectorAll(selector)};
  for(const id of ['server-rows','nav-count','stat-total','stat-online','stat-attention','stat-rent','stat-rent-help','snapshot-caption','refresh-all','check-all','filter-count','empty-state','rental-alerts','notification-status','desktop-section','local-header-actions','pc-update-count','burp-update-count','toasts','job-content','job-modal','drawer','drawer-backdrop','drawer-header','drawer-tabs','drawer-content','page-title','page-context','server-section','events-section','missing-dates','open-trash','trash-count','trash-caption','back-servers','add-server','search','delete-form','delete-modal','delete-title','delete-identity','delete-description','delete-confirmation','delete-expected-alias','delete-alias','delete-error','delete-submit','server-cost-summary','expenses-section','diary-section','diary-days','diary-status','diary-count','diary-progress','diary-error','diary-save','diary-template','diary-copy-week','diary-week-title','diary-date','diary-entry-date','diary-editor','diary-prev','diary-next','diary-today','tasks-section','tasks-list','tasks-filters','tasks-caption','task-modal','task-form','task-form-title','task-error','task-save','task-add','expense-modal','expense-form','expense-form-title','expense-form-error','expense-save','expense-delete-modal','expense-delete-form','expense-delete-name','expense-delete-error','expense-delete-submit']){const node=new Element(id==='server-rows'?'tbody':'div');node.id=id;body.append(node);}
  document.querySelector('#open-trash').append(new Element('span'));
  const dateLabel=new Element('span');dateLabel.id='expense-date-label';body.append(dateLabel);
  const expenseInputs=new Map(['name','amount','currency','period','notes','next_charge_date'].map(name=>[name,new Element('input')]));
  document.querySelector('#expense-form').elements={namedItem:name=>expenseInputs.get(name)};
  document.querySelector('#expense-form').reset=()=>{for(const input of expenseInputs.values())input.value='';};
  for(const name of ['stats','key-card','header-actions']){const node=new Element();node.className=name;body.append(node);}
  const context=createContext({document,Node:Element,console,Intl,URL,Date:ClockDate,Map,Set,Promise,JSON,
    setTimeout:(callback,delay)=>{const id=++timerId;timers.set(id,{callback,delay});return id;},clearTimeout:id=>timers.delete(id),
    fetch:async(path,options)=>{requests.push({path,method:options.method,body:options.body});const result=clone(await handler(path,options));return{ok:true,json:async()=>result};},
    window:{addEventListener(){},sessionStorage:{data:new Map(),getItem(key){return this.data.get(key)||null;},setItem(key,value){this.data.set(key,value);},removeItem(key){this.data.delete(key);}}},navigator:{}});
  runInContext(desktop+'\n'+expenses+'\n'+tasks+'\n'+diary+'\n'+app,context);
  const evaluate=code=>runInContext(code,context);
  return{document,requests,timers,metrics,evaluate,advance:milliseconds=>{now+=milliseconds;},runTimer:async delay=>{for(const [id,timer] of [...timers])if(timer.delay===delay){timers.delete(id);await timer.callback();}},query:selector=>document.querySelector(selector)};
}

const serverFixture=()=>Array.from({length:8},(_,index)=>({id:String(index+1),name:'Berlin '+(index+1),alias:'berlin-'+(index+1),host:'192.0.2.'+(index+1),port:22,ssh_user:'root',provider:'Hosting',provider_url:'',notes:'',short_note:'',archived:false,state:'ready',last_seen:'2026-09-05T12:00:00Z',snapshot:{cpu_pct:2,cpu_count:2,memory:{pct:10,used:100000000,total:1000000000},disk:{pct:20,used:2000000000,total:10000000000}}}));
const pcFixture=()=>({pc:{count:2,checked_at:'2026-09-05T12:00:00Z',managers:[{id:'apt',name:'APT',count:2,actions:['check','refresh-lists','upgrade'],packages:[{name:'openssl',installed:'3.0',candidate:'3.1'},{name:'curl',installed:'8.0',candidate:'8.1'}]}]},jobs:{},pc_error:'',terminal_available:true,revisions:{pc:'pc-1',burp:'burp-1'}});
const burpFixture=()=>({burp:{latest:{},version:'2026.8',running:[],weekly:false,launcher:'Burp.desktop',jar:'Burp.jar',update_available:false},jobs:{},terminal_available:true,revisions:{pc:'pc-1',burp:'burp-1'}});

{
  const pending=gate(),saved=new Map();let writes=0;
  const env=environment(async(path,options)=>{
    if(options.method==='GET')return {entries:[...saved.values()]};
    const payload=JSON.parse(options.body),day=path.split('/').at(-1);
    if(++writes===1)await pending.promise;
    const entry={entry_date:day,content:payload.content,revision:payload.revision+1};saved.set(day,entry);return {entry};
  });
  env.evaluate("diaryState.day='2026-09-07';diaryState.week='2026-09-07';initDiary()");
  await env.evaluate("changeView('diary')");
  assert.equal(env.query('#diary-days').children.length,7);
  assert.equal(env.query('#diary-section').classList.contains('hidden'),false);
  assert.equal(env.evaluate("diaryMonday('2026-01-04')"),'2025-12-29');
  const editor=env.query('#diary-editor');editor.focus();editor.value='Сделал копию';env.evaluate('diaryChanged()');
  const first=env.evaluate("saveDiary('2026-09-07')");
  editor.value='Сделал копию и проверил восстановление';env.evaluate('diaryChanged()');
  await env.evaluate("selectDiaryDay('2026-09-08')");editor.value='Настроил сервер';env.evaluate('diaryChanged()');await env.evaluate("saveDiary('2026-09-08')");
  pending.resolve();await first;
  assert.equal(editor.value,'Настроил сервер','A late Monday save never replaces Tuesday text');
  await env.evaluate("selectDiaryDay('2026-09-07')");
  assert.equal(editor.value,'Сделал копию и проверил восстановление','Typing during an in-flight save is retained');
  await env.evaluate("saveDiary('2026-09-07')");
  assert.equal(saved.get('2026-09-07').content,editor.value);
  assert.equal(saved.get('2026-09-08').content,'Настроил сервер');
  assert.equal(env.evaluate('diaryDraftsPending()'),false);
  assert.equal(env.query('#diary-editor'),editor,'Autosaving preserves the editor node');
  assert.equal(env.evaluate('window.sessionStorage.getItem(diaryDraftKey)'),null,'Saved text leaves no stale recovery draft');
}
{
  let content='Сохранённая запись',revision=1,fail=true;
  const env=environment((path,options)=>{
    if(options.method==='GET')return {entries:[{entry_date:'2026-09-07',content,revision}]};
    if(fail)throw new Error('Сервис недоступен');
    const payload=JSON.parse(options.body);
    if(payload.revision!==revision)throw Object.assign(new Error('Запись изменена в другой вкладке'),{status:409,details:{entry:{content,revision}}});
    content=payload.content;return {entry:{content,revision:++revision}};
  });
  env.evaluate("state.view='diary';diaryState.day='2026-09-07';diaryState.week='2026-09-07'");await env.evaluate('loadDiary()');
  env.query('#diary-editor').value='Мой черновик';env.evaluate('diaryChanged()');await env.evaluate("saveDiary('2026-09-07')");
  assert.match(env.query('#diary-error').textContent,/Сервис недоступен/);
  env.evaluate('diaryState.entries.clear();restoreDiaryDrafts()');env.query('#diary-editor').dataset.day='';await env.evaluate('loadDiary()');
  assert.equal(env.query('#diary-editor').value,'Мой черновик','A failed save survives page reconstruction');
  fail=false;content='Изменение из другой вкладки';revision=2;
  await env.evaluate("saveDiary('2026-09-07')");
  assert.equal(content,'Изменение из другой вкладки','Conflict never silently overwrites remote text');
  assert.equal(env.query('#diary-editor').value,'Мой черновик');
  assert.match(env.query('#diary-error').textContent,/Keep my version/);
  await env.evaluate('resolveDiaryConflict(true)');assert.equal(content,'Мой черновик');assert.equal(revision,3);
}

{
  let rows=[
    {id:'1',title:'Продлить сервер',due_date:'2026-09-25',notes:'Тариф Standard',completed:false,created_at:'2026-09-06'},
    {id:'2',title:'Проверить копию',due_date:'2000-01-01',notes:'<b>Архив</b>',completed:false,created_at:'2026-09-06'},
    {id:'3',title:'Сохранить настройки',due_date:'2000-01-01',notes:'',completed:true,created_at:'2026-09-06'}
  ];
  const env=environment((path,options)=>{
    if(options.method==='PATCH'){assert.equal(path,'/api/tasks/2');rows=rows.map(row=>row.id==='2'?{...row,...JSON.parse(options.body)}:row);}
    else assert.equal(path,'/api/tasks');
    return {tasks:rows};
  });
  await env.evaluate("changeView('tasks')");
  assert.equal(env.query('#page-title').textContent,'Tasks');
  assert.equal(env.query('#tasks-section').classList.contains('hidden'),false);
  assert.equal(env.query('#server-section').classList.contains('hidden'),true);
  assert.equal(env.query('.stats').classList.contains('hidden'),true);
  assert.equal(env.query('#tasks-list').children.length,2);
  assert.equal(env.query('.task-row').dataset.taskId,'2','Open tasks sort by calendar date');
  assert.equal(env.query('.task-overdue').textContent,'Overdue');
  assert.equal(env.query('.task-notes').textContent,'<b>Архив</b>');
  assert.equal(env.query('.task-notes').children.length,0,'Task notes are plain text');
  await env.evaluate("changeTask('2','PATCH',{completed:true})");
  assert.equal(env.query('#tasks-list').children.length,1,'Completed tasks leave the Open filter');
  env.evaluate("taskState.filter='all';renderTasks()");
  assert.equal(env.query('.task-row').dataset.taskId,'1','Completed tasks stay below open tasks');
  assert.equal(env.query('#tasks-list').children.length,3);
  await env.evaluate("changeTask('2','PATCH',{completed:false})");
  assert.equal(env.query('.task-row').dataset.taskId,'2','Reopened tasks regain their date position');
  env.evaluate("state.view='servers';renderTasks()");
  assert.equal(env.requests.length,3,'Task rendering starts no background requests');
}

{
  const pending=gate();
  const env=environment(path=>{assert.equal(path,'/api/server-countries');return pending.promise;});
  env.evaluate('state.servers='+JSON.stringify(serverFixture())+";state.view='servers';renderServers()");
  assert.equal(env.query('.server-flag'),null,'No guessed flags before a lookup');
  const first=env.evaluate('loadCountries()'),second=env.evaluate('loadCountries()');
  assert.equal(env.requests.length,1,'Country lookups are coalesced');
  pending.resolve({countries:{'192.0.2.1':'DE'}});await Promise.all([first,second]);
  assert.equal(env.query('.server-flag').textContent,'🇩🇪');
  assert.match(env.query('.server-flag').title,/Germany/);
  assert.equal(env.query('.server-address').textContent,'192.0.2.1','Flag does not change the displayed IP');
  await env.evaluate('loadCountries()');assert.equal(env.requests.length,1,'Renders and unchanged hosts do not repeat lookups');
  env.metrics.created=0;env.evaluate('renderServers()');assert.equal(env.metrics.created,0,'Country flags retain cached rows');
  assert.equal(env.evaluate("state.countries['192.0.2.1']='bad';countryFlag('192.0.2.1')"),null,'Malformed country codes are not rendered');
}

{
  const pending=gate(),servers=serverFixture(),job={id:'connection-1',server_id:'1',kind:'check',state:'queued'};
  const done={...job,state:'done'};
  const env=environment((path,options)=>{
    if(path==='/api/check-all'){assert.equal(options.method,'POST');assert.deepEqual(JSON.parse(options.body),{});return pending.promise;}
    if(path==='/api/jobs')return{jobs:[done]};
    assert.equal(path,'/api/servers');return{servers:servers.map((s,index)=>index===0?{...s,ssh_latency_ms:42.5,ssh_checked_at:'2026-09-06T12:00:00Z'}:s),jobs:[done],rentals:null};
  });
  env.evaluate('state.servers='+JSON.stringify(servers)+";state.view='servers';renderServers()");
  const first=env.evaluate('checkAll()');await env.evaluate('checkAll()');
  assert.equal(env.requests.length,1,'Repeated lightning clicks share the running request');
  assert.equal(env.query('#check-all').disabled,true);
  pending.resolve({jobs:[job],skipped:[]});await first;
  assert.equal(env.query('#check-all').disabled,true,'Lightning remains disabled while checks are queued');
  assert.match(env.query('#server-rows').children[0].textContent,/Checking SSH/);
  await env.evaluate('pollJobs()');
  assert.equal(env.query('#check-all').disabled,false,'Completion reenables lightning');
  assert.equal(env.query('#server-rows').children[0].querySelector('.ssh-latency').textContent,'SSH 43 ms');
  assert.equal(env.evaluate('state.servers[0].last_seen'),servers[0].last_seen,'Connection checks do not invent fresh metrics');
  env.evaluate("state.servers[0].ssh_latency_ms=null;state.servers[0].state='ready';renderServers()");
  assert.equal(env.query('#server-rows').children[0].querySelector('.ssh-latency'),null,'A successful refresh hides an obsolete failed connection check');
  env.evaluate("state.servers[0].state='credentials_needed';renderServers()");
  assert.equal(env.query('[data-server-id="1"] .ssh-latency').textContent,'SSH check failed','Current failed access remains visible');
  env.evaluate("state.view='trash';renderServers()");assert.equal(env.query('#check-all').classList.contains('hidden'),true);
}

{
  const env=environment(()=>{throw new Error('Unexpected fetch');});
  env.evaluate('state.servers='+JSON.stringify(serverFixture()));
  env.metrics.created=0;env.evaluate('renderServers()');
  assert.equal(env.metrics.created,0,'Hidden server section creates no rows, icons, or rental DOM');
  assert.equal(env.query('#server-rows').children.length,0);
  env.evaluate("state.view='servers';renderServers()");
  const rows=[...env.query('#server-rows').children],initialNodes=env.metrics.created;
  rows[1].querySelector('.copy-ssh').classList.add('is-copied');rows[1].querySelector('.copy-ssh').focus();
  env.metrics.created=0;env.evaluate('renderServers()');
  assert.equal(env.metrics.created,0,'Unchanged snapshot creates no element nodes');
  assert.deepEqual(env.query('#server-rows').children,rows);
  env.evaluate("state.jobs=[{id:'refresh-1',server_id:'1',state:'running',kind:'refresh',message:'Refreshing'}];renderServers()");
  assert.notEqual(env.query('#server-rows').children[0],rows[0]);
  assert.equal(env.query('#server-rows').children[1],rows[1],'Other server rows survive one job changing');
  assert.ok(rows[1].querySelector('.copy-ssh').classList.contains('is-copied'));
  assert.equal(env.document.activeElement,rows[1].querySelector('.copy-ssh'),'Unchanged Copy SSH keeps focus');
  env.evaluate("state.servers=state.servers.map(server=>({...server,identity_key:'current-key'}));renderServers();openForm=(mode,server)=>{globalThis.editedServer=server;}");
  assert.equal(env.query('#server-rows').children[1],rows[1],'Unrendered metadata keeps the keyed row');
  rows[1].querySelector('.actions').children.find(item=>item.title?.startsWith('Edit ')).dispatchEvent({type:'click'});
  assert.equal(env.evaluate('editedServer.identity_key'),'current-key','A retained row edits the current server record');
  env.evaluate('state.servers=state.servers.map(server=>({...server,last_seen:new Date().toISOString()}));renderServers()');
  const freshRows=[...env.query('#server-rows').children];env.metrics.created=0;env.advance(61000);env.evaluate('renderServers()');
  assert.equal(env.metrics.created,0,'Snapshot age updates create no replacement rows');assert.deepEqual(env.query('#server-rows').children,freshRows);assert.equal(freshRows[0].querySelector('.age').textContent,'snapshot 1m ago');
  env.evaluate("state.servers[0].state='timeout';state.servers[2].state='pending';renderServers()");
  const order=()=>[...env.query('#server-rows').children].map(row=>row.dataset.serverId);
  assert.deepEqual(order(),['2','4','5','6','7','8','1','3'],'Available servers come before errors and pending entries');
  env.evaluate("state.servers[0].state='ready';state.servers[1].state='timeout';renderServers()");
  assert.deepEqual(order(),['1','4','5','6','7','8','2','3'],'Status changes reorder rows without duplicates');
  env.evaluate("state.servers[0].provider='Zeta';state.servers[1].provider='Alpha';state.servers[2].provider='Zeta';state.servers[3].provider='Alpha';renderServers()");
  assert.deepEqual(order(),['4','5','6','7','8','1','2','3'],'Providers are grouped within available and unavailable sections');
  env.metrics.created=0;env.evaluate('renderServers()');assert.equal(env.metrics.created,0,'Sorted rows remain reusable');
  env.evaluate("state.search='berlin 2';renderServers()");assert.deepEqual(order(),['2'],'Search still finds unavailable entries');
  console.log('Server DOM: '+initialNodes+' initial nodes; 0 nodes for hidden/unchanged render; available first, then provider grouping.');
}

{
  let attempts=0;
  const env=environment(path=>{
    if(path==='/api/bootstrap')return{csrf:'local-session',servers:[],jobs:[],key_options:[],rentals:null,key:{fingerprint:'Local key'}};
    assert.equal(path,'/api/desktop-updates?view=pc');if(++attempts===1)throw new Error('Local status unavailable');return pcFixture();
  });
  for(const id of ['key-fingerprint','server-modal','server-form','quick-note-form','quick-note-input','desktop-reminders','notification-preview']){const node=env.document.createElement('div');node.id=id;env.document.body.append(node);}
  for(const view of ['servers','expenses','tasks','diary','events','pc','burp','listeners']){const nav=env.document.createElement('button');nav.className='nav-button';nav.dataset.view=view;nav.append(env.document.createElement('span'));env.document.body.append(nav);}
  const search=env.document.createElement('div');search.className='search';search.append(env.document.createElement('span'));env.document.body.append(search);
  const key=env.document.createElement('div');key.className='key-card';key.append(env.document.createElement('strong'));env.document.body.append(key);
  const fields=new Map();env.query('#server-form').elements={namedItem:name=>{if(!fields.has(name))fields.set(name,env.document.createElement('input'));return fields.get(name);}};
  await assert.rejects(env.evaluate('init()'),/Local status unavailable/);
  assert.equal(env.evaluate('desktopState.deferRender'),false,'A rejected first PC load releases deferred rendering');
  await env.evaluate("loadDesktop('pc')");assert.ok(env.query('.package-search'),'A subsequent successful PC load renders after startup failure');
  env.evaluate("state.formServer={id:'1',notes:'Stored'}");env.query('#server-modal').close();assert.equal(env.evaluate('state.formServer'),null,'Closed forms release their server snapshot');
  env.evaluate("state.jobId='setup-1';state.jobs=[{id:'setup-1',state:'running',step:1,message:'Starting'}];renderJob()");assert.equal(env.query('#job-content').children.length,0,'Closed job dialogs are not rendered');
  env.evaluate('renderJob(true)');env.query('#job-modal').showModal();assert.ok(env.query('#job-content').textContent.includes('Starting'));
  env.query('#job-modal').close();assert.equal(env.query('#job-content').children.length,0,'Closing the job dialog releases its content');
}

{
  const fixture=pcFixture();let progress=10,revision='pc-1',jobState='running';
  const env=environment(path=>{
    if(path==='/api/desktop-updates?view=pc')return{...fixture,revisions:{pc:revision,burp:'burp-1'},jobs:{pc:{state:jobState,message:'Package check',percent:progress}}};
    assert.equal(path,'/api/desktop-updates/status');return{jobs:{pc:{state:jobState,message:'Package check',percent:progress}},revisions:{pc:revision,burp:'burp-1'},pc_error:''};
  });
  await env.evaluate("loadDesktop('pc')");
  const search=env.query('.package-search');search.value='openssl';search.dispatchEvent({type:'input'});search.focus();search.setSelectionRange(2,5);
  const row=env.query('#desktop-section tbody tr'),snapshotCount=()=>env.requests.filter(item=>item.path.includes('?view=')).length;
  for(const next of [25,55,90]){progress=next;await env.evaluate('pollDesktop()');assert.equal(env.query('.package-search'),search);assert.equal(env.query('#desktop-section tbody tr'),row);assert.equal(env.document.activeElement,search);assert.deepEqual([search.selectionStart,search.selectionEnd],[2,5]);}
  jobState='done';await env.evaluate('pollDesktop()');
  assert.equal(env.query('#desktop-section tbody tr'),row,'Completion without an inventory revision leaves rows intact');
  assert.equal(env.query('#pc-inventory-refresh').disabled,false,'Completion reenables actions without rebuilding controls');
  assert.equal(snapshotCount(),1,'Four progress/status responses transfer no inventory');
  revision='pc-2';await env.evaluate('pollDesktop()');assert.equal(snapshotCount(),2,'A changed revision fetches one fresh inventory');
  assert.equal(env.document.activeElement,env.query('.package-search'),'Inventory replacement restores search focus');
  assert.deepEqual([env.document.activeElement.selectionStart,env.document.activeElement.selectionEnd],[2,5]);
  assert.equal(env.query('.package-search').value,'openssl');
  console.log('Desktop progress: 4 status reads, 0 inventory reads, stable search/caret/package row.');
}

{
  const waiting=gate();
  const env=environment(path=>{assert.equal(path,'/api/desktop-updates?view=burp');return waiting.promise;});
  const reads=[env.evaluate("loadDesktop('burp')"),env.evaluate("loadDesktop('burp')"),env.evaluate("loadDesktop('burp')")];
  assert.equal(env.requests.length,1,'Concurrent desktop loads share one request');waiting.resolve(burpFixture());await Promise.all(reads);
  assert.ok(env.evaluate('!!desktopState.data.burp'));assert.equal(env.evaluate('state.view'),'pc');
}

{
  let running=[];
  const env=environment(path=>{
    if(path.endsWith('?view=burp')){const fixture=burpFixture();fixture.burp.running=running;return fixture;}
    if(path.endsWith('?view=pc'))return pcFixture();
    assert.equal(path,'/api/desktop-updates/status');return{jobs:{},revisions:{pc:'pc-1',burp:'burp-1'},pc_error:''};
  });
  env.evaluate("state.view='burp'");await env.evaluate('loadDesktop()');
  const reads=()=>env.requests.filter(item=>item.path.endsWith('?view=burp')).length;
  env.advance(1500);await env.evaluate('pollDesktop()');assert.equal(reads(),1,'Fast status polls do not scan Burp processes');
  running=[321];env.advance(60000);await env.evaluate('pollDesktop()');assert.equal(reads(),2,'Visible Burp refreshes external process state after 60 seconds');
  assert.ok(env.query('#desktop-section').textContent.includes('Burp is running.'),'External Burp process changes render even with unchanged persisted revisions');
  env.evaluate("state.view='pc'");await env.evaluate('loadDesktop()');env.advance(60000);await env.evaluate('pollDesktop()');
  assert.equal(reads(),2,'An old cached Burp view causes no process scan on PC');
}

{
  const waiting=gate();let gets=0;
  const env=environment((path,options)=>options.method==='PATCH'?{saved:true}:++gets===1?waiting.promise:{servers:[{id:'1',notes:'Updated'}],jobs:[],rentals:null});
  const first=env.evaluate('reload()'),second=env.evaluate('reload()');
  assert.equal(gets,1,'Concurrent server reloads share one request');
  await env.evaluate("api('/api/servers/1','PATCH',{notes:'Updated'})");
  waiting.resolve({servers:[{id:'1',notes:'Old'}],jobs:[],rentals:null});await Promise.all([first,second]);
  assert.equal(gets,2,'A read crossing a mutation is repeated after the write');
  assert.equal(env.evaluate('state.servers[0].notes'),'Updated','A stale GET cannot overwrite the completed edit');
}

{
  const env=environment(path=>{
    if(path.endsWith('?view=pc'))return pcFixture();
    if(path.endsWith('/status'))return{jobs:{},revisions:{pc:'pc-1',burp:'burp-1'},pc_error:''};
    if(path==='/api/rental-alerts')return{rentals:null};
    if(path==='/api/jobs')return{jobs:[{id:'refresh-1',server_id:'1',state:'done'}]};
    if(path==='/api/servers')return{servers:serverFixture(),jobs:[],rentals:null};
    throw new Error('Unexpected request '+path);
  });
  await env.evaluate("loadDesktop('pc')");env.evaluate("state.jobs=[{id:'refresh-1',server_id:'1',state:'running'}];schedulePoll();scheduleRentalPoll()");
  env.document.hidden=true;env.evaluate('handleVisibility()');assert.equal(env.timers.size,0,'Hiding the page removes every polling timer');
  const reads=env.requests.length;await env.evaluate('Promise.all([pollDesktop(),pollRentals(),poll()])');assert.equal(env.requests.length,reads,'Hidden callbacks make no HTTP requests');
  env.document.hidden=false;env.evaluate('handleVisibility()');await env.evaluate('Promise.all([desktopState.statusPromise,state.rentalPromise,state.pollPromise])');
  assert.ok(env.requests.slice(reads).some(item=>item.path==='/api/desktop-updates/status'));
  assert.ok(env.requests.slice(reads).some(item=>item.path==='/api/rental-alerts'));
  assert.ok(env.requests.slice(reads).some(item=>item.path==='/api/servers'));
  assert.ok(env.requests.slice(reads).some(item=>item.path==='/api/jobs'));
  assert.ok(env.requests.every(item=>!item.path.includes('local-listeners')&&!item.path.includes('port-watch')),'Visibility and polling never read local ports');
  assert.equal(env.query('#server-rows').children.length,0,'VPS completion while PC is shown leaves server DOM unbuilt');
}
{
  const env=environment(()=>{throw new Error('Unexpected request');});
  const server={...serverFixture()[0],snapshot:{packages:Array.from({length:1000},(_,index)=>({name:'library-'+String(index).padStart(4,'0'),version:'1.0'})),manual_tools:[{name:'utility',path:'/usr/bin/utility'}]}};
  env.evaluate('state.detail='+JSON.stringify(server)+';state.selected="1";state.tab="packages";renderDrawerHeader();renderTabs();renderDrawerContent()');
  assert.equal(env.query('#drawer-content').querySelectorAll('tbody tr').length,100,'Software renders at most 100 package/tool rows');
  assert.equal(env.query('#package-range').textContent,'1–100 of 1001');
  env.query('#package-next').dispatchEvent({type:'click'});assert.equal(env.query('#package-range').textContent,'101–200 of 1001');
  const input=env.query('.list-search');env.metrics.created=0;
  for(const value of ['library-0','library-099','library-0999']){input.value=value;input.dispatchEvent({type:'input'});}
  assert.equal([...env.timers.values()].filter(timer=>timer.delay===120).length,1,'Search bursts have one pending callback');
  assert.equal(env.metrics.created,0,'Keystrokes do not build package tables before debounce');
  await env.runTimer(120);assert.equal(env.query('#drawer-content').querySelectorAll('tbody tr').length,1);assert.ok(env.query('#drawer-content').textContent.includes('library-0999'),'Search covers packages beyond the current page');assert.ok(env.query('.software-pagination').classList.contains('hidden'));
  input.value='';input.dispatchEvent({type:'input'});await env.runTimer(120);assert.equal(env.query('#package-range').textContent,'1–100 of 1001','Changing search resets to the first page');
  input.value='library';input.dispatchEvent({type:'input'});const oldCallback=[...env.timers.values()].find(timer=>timer.delay===120).callback;
  env.evaluate('closeDrawer()');assert.equal([...env.timers.values()].filter(timer=>timer.delay===120).length,0,'Closing cancels pending package filtering');
  for(const id of ['drawer-content','drawer-header','drawer-tabs'])assert.equal(env.query('#'+id).children.length,0,'Closed drawer releases '+id);
  assert.equal(env.evaluate('state.detail'),null);oldCallback();assert.equal(env.query('#drawer-content').children.length,0,'An obsolete callback cannot repopulate a closed drawer');
  assert.equal(env.requests.length,0);
  console.log('Software: 100 rows for 1,001 entries; full-inventory debounced search; 0 drawer nodes after close.');
}

{
  const saving=gate();
  const env=environment((path,options)=>{assert.equal(path,'/api/servers/1');assert.equal(options.method,'PATCH');return saving.promise;});
  env.evaluate("state.selected='1';state.detail={id:'1'};state.notes.set('1',{value:'Unsaved note',dirty:true,saving:false,error:false,timer:null});closeDrawer()");
  assert.equal(env.evaluate("state.notes.get('1').value"),'Unsaved note');assert.equal(env.evaluate("state.notes.get('1').saving"),true,'Closing retains a note while its save is in flight');
  saving.resolve({server:{id:'1',notes:'Unsaved note'}});await env.evaluate('Promise.all([...apiWrites])');
  assert.equal(env.evaluate("state.notes.has('1')"),false,'A successfully saved closed draft can be released');
  env.evaluate("state.notes.set('1',{value:'Retry note',dirty:true,saving:false,error:true,timer:null});releaseNotes('1')");
  assert.equal(env.evaluate("state.notes.get('1').value"),'Retry note','Failed dirty drafts remain available for retry');
}

{
  let desktopJob={};
  const env=environment(path=>{
    if(path.endsWith('?view=pc'))return pcFixture();
    if(path==='/api/local-listeners')return{listeners:[],state:'ok',checked_at:'2026-09-05T12:00:00Z'};
    assert.equal(path,'/api/desktop-updates/status');return{jobs:desktopJob,revisions:{pc:'pc-1',burp:'burp-1'},pc_error:''};
  });
  await env.evaluate("loadDesktop('pc')");const workspace=env.query('.pc-workspace'),oldTimer=[...env.timers.values()][0].callback;
  assert.ok(env.evaluate('desktopState.controls.length>0'));
  await env.evaluate("changeView('listeners')");assert.equal(workspace.isConnected,false);
  assert.equal(env.evaluate('desktopState.controls.length'),0);assert.equal(env.evaluate('desktopState.jobSlots.size'),0,'Ports releases all previous desktop DOM registries');
  const requests=env.requests.length;await oldTimer();assert.equal(env.requests.length,requests,'An old desktop timer makes no request on Ports without active jobs');
  env.evaluate("desktopState.data.jobs={pc:{state:'running',message:'Updating packages'}};scheduleDesktopPoll()");desktopJob={pc:{state:'done',message:'Package update complete'}};
  await env.runTimer(1500);assert.equal(env.requests.at(-1).path,'/api/desktop-updates/status','Active jobs continue lightweight progress polling on Ports');assert.ok(env.query('#toasts').textContent.includes('Package update complete'),'Background completion remains visible');
  assert.equal(env.requests.filter(item=>item.path==='/api/local-listeners').length,1,'Job polling never rereads local ports');
}

{
  let step=1,finished=false,inventoryReads=0;
  const job=()=>({id:'refresh-1',server_id:'1',kind:'refresh',state:finished?'done':'running',step,message:'Refresh step '+step});
  const env=environment(path=>{
    if(path==='/api/jobs')return{jobs:[job()]};
    assert.equal(path,'/api/servers');if(++inventoryReads===1)throw new Error('Snapshot temporarily unavailable');return{servers:serverFixture(),jobs:[job()],rentals:null};
  });
  env.evaluate('state.servers='+JSON.stringify(serverFixture())+';state.jobs='+JSON.stringify([job()])+";state.jobId='refresh-1'");
  for(step=2;step<=4;step++)await env.evaluate('poll()');
  assert.equal(env.requests.length,3);assert.ok(env.requests.every(item=>item.path==='/api/jobs'),'Running VPS jobs transfer no server inventory');assert.equal(env.query('#job-content').children.length,0,'Progress does not render a closed job dialog');
  finished=true;await env.evaluate('poll()');assert.equal(env.evaluate('state.completedJobs.size'),1,'A failed final snapshot refresh keeps the completion pending');assert.equal(env.evaluate('hasPendingJobs()'),true);
  await env.evaluate('poll()');assert.equal(inventoryReads,2);assert.equal(env.evaluate('state.completedJobs.size'),0,'Completion settles after the inventory refresh succeeds');assert.equal(env.evaluate('hasPendingJobs()'),false);
  console.log('VPS progress: 3 job-only reads, 0 inventory reads; terminal refresh retries without losing completion.');
}

{
  const completed={id:'refresh-1',server_id:'1',kind:'refresh',state:'done',message:'Refreshed'};
  const server={...serverFixture()[0],notes:'Saved note'};
  const env=environment(path=>path==='/api/jobs'?{jobs:[completed]}:path==='/api/servers'?{servers:[server],jobs:[completed],rentals:null}:{server});
  env.evaluate('state.servers='+JSON.stringify([server])+";state.jobs=[{id:'refresh-1',server_id:'1',state:'running'}];state.selected='1';state.tab='notes';state.detail="+JSON.stringify(server)+";state.notes.set('1',{value:'Current draft',dirty:true,saving:false,error:false,timer:null})");
  const editor=env.document.createElement('textarea');editor.id='notes-editor';editor.value='Current draft';env.query('#drawer-content').append(editor);
  await env.evaluate('poll()');assert.equal(env.query('#notes-editor'),editor,'Job completion does not replace an open notes editor');assert.equal(editor.value,'Current draft');assert.equal(env.evaluate("state.notes.get('1').dirty"),true);assert.ok(env.requests.some(item=>item.path==='/api/servers/1'),'Completion still refreshes the selected detail snapshot');
}
function expenseFixture(currency='USD') {
  const native={USD:{monthly:'5.00',yearly:'60.00',once:'0.00'},RUB:{monthly:'900.00',yearly:'10800.00',once:'0.00'}};
  const empty={monthly:'0.00',yearly:'0.00',once:'0.00',known:{monthly:'0.00',yearly:'0.00',once:'0.00'},native:{USD:{monthly:'0.00',yearly:'0.00',once:'0.00'},RUB:{monthly:'0.00',yearly:'0.00',once:'0.00'}},unknown_count:0,review_count:0,complete:true};
  const total={monthly:null,yearly:null,once:'0.00',known:{monthly:currency==='USD'?'15.00':'1350.00',yearly:currency==='USD'?'180.00':'16200.00',once:'0.00'},native,unknown_count:1,review_count:1,complete:false};
  const servers=serverFixture().slice(0,3).map((server,index)=>({...server,cost_amount:index===0?'5':index===1?'900':null,cost_currency:index===0?'USD':index===1?'RUB':null,cost_period:'monthly',cost_review:index===0,monthly:index===0?(currency==='USD'?'5.00':'450.00'):index===1?(currency==='USD'?'10.00':'900.00'):null,yearly:index===0?(currency==='USD'?'60.00':'5400.00'):index===1?(currency==='USD'?'120.00':'10800.00'):null,converted_amount:index===0?(currency==='USD'?'5.00':'450.00'):index===1?(currency==='USD'?'10.00':'900.00'):null}));
  return {currency,fx:{source:'CBR',usd_rub:'90.00',date:'2026-09-06',checked_at:'2026-09-06T12:00:00Z',stale:false,error:'',estimated:true},servers,items:[],totals:{all:total,servers:total,items:empty}};
}
{
  const original=expenseFixture(),archived=expenseFixture();
  archived.servers=[];archived.totals.all=archived.totals.servers=clone(archived.totals.items);
  let current=archived;
  const env=environment(path=>{
    if(path==='/api/rental-alerts')return {rentals:null};
    assert.equal(path,'/api/expenses');return current;
  });
  env.evaluate('acceptExpenses('+JSON.stringify(original)+");state.view='expenses';renderExpenses()");
  assert.equal(env.query('#expenses-section').querySelectorAll('[data-expense-server-id]').length,3);
  env.document.hidden=true;env.evaluate('handleVisibility()');
  env.document.hidden=false;env.evaluate('handleVisibility()');
  await env.evaluate('Promise.all([state.rentalPromise,expenseState.loadPromise])');
  assert.equal(env.query('#expenses-section').querySelectorAll('[data-expense-server-id]').length,0,'Returning to Expenses removes servers archived in another tab');
  assert.match(env.query('.expense-summary-card strong').textContent,/USD\s*0\.00/,'Totals refresh with the archived server list');
  current=original;await env.evaluate('pollRentals()');
  assert.equal(env.query('#expenses-section').querySelectorAll('[data-expense-server-id]').length,3,'Visible Expenses picks up restoration even when rental alerts did not change');
  env.metrics.created=0;await env.evaluate('pollRentals()');assert.equal(env.metrics.created,0,'Unchanged periodic expense updates preserve the DOM');
  const reads=env.requests.length;env.document.hidden=true;await env.evaluate('pollRentals()');assert.equal(env.requests.length,reads,'Hidden expenses do not poll');
}
{
  const env=environment(()=>{throw new Error('Unexpected request');}),fixture=expenseFixture();
  env.evaluate('acceptExpenses('+JSON.stringify(fixture)+');state.servers='+JSON.stringify(fixture.servers));env.metrics.created=0;env.evaluate('renderExpenses();renderServerCosts()');
  assert.equal(env.metrics.created,0,'Hidden expenses and server-cost summaries create no DOM');assert.equal(env.requests.length,0);
  env.evaluate("state.view='servers';renderServers()");assert.match(env.query('#server-cost-summary').textContent,/Known: USD\s*15\.00 \/ month/,'Known converted costs remain prominent when full totals are incomplete');
  const rows=[...env.query('#server-rows').children];env.metrics.created=0;env.evaluate('renderServers()');assert.equal(env.metrics.created,0,'Unchanged server costs do not replace rows or currency controls');assert.deepEqual(env.query('#server-rows').children,rows);
  env.evaluate("state.view='expenses';renderExpenses()");const card=env.query('.expense-summary-card');assert.match(card.querySelector('strong').textContent,/USD\s*15\.00/);assert.match(card.textContent,/known costs per month.*1 cost not set/);assert.match(card.textContent,/billing period/);
  assert.equal(env.query('#expenses-section').querySelectorAll('[data-expense-server-id]').length,3);assert.match(env.query('.expense-once').textContent,/USD\s*0\.00/,'One-time zero is separate from incomplete recurring costs');
  env.metrics.created=0;env.evaluate('renderExpenses()');assert.equal(env.metrics.created,0,'An unchanged Expenses snapshot preserves its DOM');
  assert.equal(env.evaluate('money(null)'),'Not available');assert.match(env.evaluate("money('0')"),/USD\s*0\.00/,'An explicit zero remains a known amount');
  assert.match(env.evaluate("money('8.99','EUR')"),/EUR\s*8\.99/,'Euro prices retain their currency');
  assert.match(env.evaluate("knownAmounts({native:{EUR:{monthly:'8.99'}}},'monthly')"),/EUR\s*8\.99/,'Unavailable conversion retains euro subtotals');
  assert.ok([...env.query('#expense-display-currency').children].some(option=>option.value==='EUR'),'Euro is selectable for display');
  assert.match(env.evaluate("fxCaption({usd_rub:'100',eur_rub:'110',date:'2026-09-06'})"),/1 EUR = RUB\s*110\.00/,'Euro reference rate is visible');
  env.evaluate("expenseState.data.totals.all.known.monthly=null;expenseState.data.fx.usd_rub=null;expenseState.data.fx.error='Network unavailable';renderExpenses()");assert.match(env.query('.expense-summary-card').textContent,/Known: USD\s*5\.00 \+ RUB\s*900\.00/,'Unavailable conversion falls back to explicit native subtotals');assert.match(env.query('.expense-rates').textContent,/Rate update failed: Network unavailable/);
}
{
  const pending=gate(),fixture=expenseFixture('USD');
  const env=environment((path,options)=>{assert.equal(path,'/api/expenses/settings');assert.equal(options.method,'PATCH');assert.equal(JSON.parse(options.body).currency,'RUB');return pending.promise;});
  env.evaluate('state.servers='+JSON.stringify(fixture.servers)+';acceptExpenses('+JSON.stringify(fixture)+");state.view='servers';renderServers()");
  const original=env.query('#server-rows').children[0],saving=env.evaluate("setExpenseCurrency('RUB')");assert.equal(env.query('#server-display-currency').disabled,true);pending.resolve(expenseFixture('RUB'));await saving;
  assert.equal(env.query('#server-display-currency').value,'RUB');assert.equal(env.query('#server-display-currency').disabled,false);assert.notEqual(env.query('#server-rows').children[0],original);assert.match(env.query('#server-rows').textContent,/RUB\s*450\.00/);
  env.evaluate("state.view='expenses';renderExpenses()");assert.equal(env.query('#expense-display-currency').value,'RUB');assert.equal(env.requests.length,1,'Changing shared display currency needs no rate request');
}
{
  const pending=gate(),fixture=expenseFixture('RUB');
  const env=environment(path=>{assert.equal(path,'/api/expenses');return pending.promise;});
  env.evaluate('state.servers='+JSON.stringify(fixture.servers)+';acceptExpenses('+JSON.stringify(fixture)+");state.view='servers';renderServers()");
  const loads=[env.evaluate("changeView('expenses')"),env.evaluate('loadExpenses()')];assert.equal(env.requests.length,1,'Concurrent Expenses opens share one load');
  await env.evaluate("changeView('servers')");assert.equal(env.query('#expenses-section').children.length,0,'Leaving Expenses releases its rendered table');
  fixture.servers[0].converted_amount='500.00';fixture.fx.usd_rub='100.00';pending.resolve(fixture);await Promise.all(loads);
  assert.match(env.query('#server-rows').textContent,/RUB\s*500\.00/,'A late FX reply refreshes the visible server costs');assert.equal(env.query('#expenses-section').children.length,0,'A late response cannot rebuild a closed Expenses view');assert.equal(env.timers.size,0,'Expenses adds no polling timers');
}
{
  let saved=expenseFixture(),failSave=true;
  const env=environment((path,options)=>{const values=options.body?JSON.parse(options.body):{};if(options.method==='POST'){if(failSave)throw new Error('Storage temporarily unavailable');assert.equal(values.amount,'120.000001');assert.equal(values.period,'yearly');saved.items=[{...values,id:'41',monthly:'10.00',yearly:'120.00',converted_amount:'120.00'}];}else if(options.method==='PATCH'){assert.equal(path,'/api/expenses/41');saved.items=[{...saved.items[0],...values}];}else{assert.equal(path,'/api/expenses/41');assert.equal(options.method,'DELETE');saved.items=[];}return saved;});
  const inputs=new Map(['name','amount','currency','period','notes','next_charge_date'].map(name=>[name,env.document.createElement('input')]));env.query('#expense-form').elements={namedItem:name=>inputs.get(name)};env.query('#expense-form').reset=()=>{for(const input of inputs.values())input.value='';};
  const close=env.document.createElement('button');close.dataset.close='expense-delete-modal';env.query('#expense-delete-modal').append(close);
  env.evaluate('acceptExpenses('+JSON.stringify(saved)+");state.view='expenses';initExpenses();openExpenseForm()");assert.equal(inputs.get('amount').value,'','New expenses never contain a preset amount');
  assert.equal(inputs.get('next_charge_date').value,'','A new expense has no invented payment date');
  for(const [name,value] of Object.entries({name:'Cloud storage',amount:'120.000001',currency:'USD',period:'yearly',notes:'Annual plan',next_charge_date:'2026-09-09'}))inputs.get(name).value=value;
  await env.evaluate('submitExpense({preventDefault(){}})');assert.equal(env.query('#expense-modal').open,true);assert.equal(inputs.get('amount').value,'120.000001','Failed saves retain precise input');assert.match(env.query('#expense-form-error').textContent,/temporarily unavailable/);
  failSave=false;await env.evaluate('submitExpense({preventDefault(){}})');assert.equal(env.query('#expense-modal').open,false);assert.equal(env.evaluate('expenseState.editing'),null);assert.equal(env.evaluate('expenseState.data.items[0].amount'),'120.000001','Decimal amounts travel as exact strings');
  assert.equal(env.evaluate('expenseState.data.items[0].next_charge_date'),'2026-09-09','Payment dates are saved explicitly');
  env.evaluate('openExpenseForm(expenseState.data.items[0])');assert.equal(inputs.get('next_charge_date').value,'2026-09-09');inputs.get('period').value='once';inputs.get('period').dispatchEvent({type:'change'});assert.equal(env.query('#expense-date-label').textContent,'Payment date');assert.equal(inputs.get('next_charge_date').value,'2026-09-09','Changing billing period never advances or replaces a date');
  inputs.get('next_charge_date').value='';await env.evaluate('submitExpense({preventDefault(){}})');assert.equal(env.evaluate('expenseState.data.items[0].period'),'once');assert.equal(env.evaluate('expenseState.data.items[0].next_charge_date'),null,'Clearing a date sends null');
  const writes=env.requests.length;env.evaluate('openExpenseDelete(expenseState.data.items[0])');assert.equal(env.requests.length,writes,'Opening delete confirmation does not delete');env.query('#expense-delete-modal').close();assert.equal(env.evaluate('expenseState.data.items.length'),1);
  env.evaluate('openExpenseDelete(expenseState.data.items[0])');await env.evaluate('submitExpenseDelete({preventDefault(){}})');assert.equal(env.evaluate('expenseState.data.items.length'),0);assert.equal(env.query('#expense-delete-modal').open,false);
}
{
  const env=environment(()=>{throw new Error('Unexpected request');});env.evaluate("state.rentals={today:'2026-09-06'}");
  for(const [value,level,caption] of [['2026-09-05','overdue','Saved date passed 1d ago'],['2026-09-06','urgent','Payment today'],['2026-09-09','urgent','In 3 days'],['2026-09-10','soon','In 4 days'],['2026-09-13','soon','In 7 days'],['2026-09-14','known','In 8 days']]){
    const rendered=env.evaluate('expensePaymentDate('+JSON.stringify({period:'monthly',next_charge_date:value})+')');assert.ok(rendered.classList.contains(level),value+' has '+level+' emphasis');assert.equal(rendered.querySelector('time').getAttribute('datetime'),value);assert.equal(rendered.querySelector('.rental-countdown').textContent,caption);
    if(level==='known')assert.ok(!rendered.classList.contains('urgent')&&!rendered.classList.contains('soon')&&!rendered.classList.contains('overdue'));
  }
  const missing=env.evaluate("expensePaymentDate({period:'monthly',next_charge_date:null})");assert.equal(missing.querySelector('time'),null);assert.match(missing.textContent,/Not set/);
  const fixture=expenseFixture();fixture.items=[{id:'71',name:'Cloud storage',amount:'5',currency:'USD',period:'monthly',next_charge_date:'2026-09-09',monthly:'5.00',yearly:'60.00',converted_amount:'5.00'}];env.evaluate('acceptExpenses('+JSON.stringify(fixture)+");state.view='expenses';renderExpenses()");
  assert.match(env.query('[data-expense-id="71"]').textContent,/In 3 days/);env.metrics.created=0;env.evaluate('renderExpenses()');assert.equal(env.metrics.created,0,'Stable payment dates keep the existing DOM');
  env.evaluate("state.rentals.today='2026-09-07';renderExpenses()");assert.match(env.query('[data-expense-id="71"]').textContent,/In 2 days/,'The existing rental day signal refreshes payment labels across midnight');
  assert.equal(env.requests.length,0);assert.equal(env.timers.size,0,'Payment dates create no requests or timers');
}
console.log('PASS: frontend runtime, bounded DOM, hidden timers, shared expense currency, totals, late rates, precise CRUD, payment dates, and explicit deletion');

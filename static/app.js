'use strict';
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const state = {servers: [], jobs: [], csrf: '', keys: [], view: 'pc', filter: 'all', search: '', selected: null, detail: null, tab: 'overview', jobId: null, formMode: 'add', formServer: null, pollTimer: null, notes: new Map(), rentals: null, rentalTimer: null, rentalSignature: '', quickNoteId: null, serverRows: new Map(), reloadPromise: null, rentalPromise: null, pollPromise: null, summaryJobCount: null, jobSignature: '', completedJobs: new Map(), packageTimer: null, deletion: null, deleteBusy: false};
const apiReads = new Map(), apiWrites = new Set();
let apiVersion = 0, busyJobsSource = null, busyJobs = new Map();
const dateFormatter = new Intl.DateTimeFormat('en-GB', {day:'2-digit',month:'2-digit',year:'numeric',hour:'2-digit',minute:'2-digit'});
const percentFormatter = new Intl.NumberFormat('en-GB', {maximumFractionDigits:1});
const countryNames = typeof Intl.DisplayNames === 'function' ? new Intl.DisplayNames(['en'], {type:'region'}) : null;

function countryFlag(host) {
  const code=state.countries?.[host];if(!/^[A-Z]{2}$/.test(code||''))return null;
  const flag=el('span','server-flag',String.fromCodePoint(...[...code].map(letter=>127397+letter.charCodeAt(0))));
  flag.title=(countryNames?.of(code)||code)+' · IP location';flag.setAttribute('role','img');flag.setAttribute('aria-label',flag.title);return flag;
}
function loadCountries() {
  const hosts=[...new Set(state.servers.map(s=>s.host))].sort().join('|');
  if(state.countryHosts===hosts)return state.countryPromise||Promise.resolve();
  if(state.countryPromise)return state.countryPromise.then(()=>loadCountries());
  state.countryHosts=hosts;if(!hosts)return Promise.resolve();
  state.countryPromise=api('/api/server-countries').then(result=>{state.countries=result.countries;renderServers();}).catch(()=>{}).finally(()=>{state.countryPromise=null;});
  return state.countryPromise;
}

function el(tag, cls, text) { const item = document.createElement(tag); if (cls) item.className = cls; if (text !== undefined && text !== null) item.textContent = String(text); return item; }
function append(parent, ...children) { for (const child of children.flat()) if (child != null) parent.append(child); return parent; }
function icon(name, extra = '') {
  const paths = {
    server: ['M5 3h14a2 2 0 0 1 2 2v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2Z', 'M5 13h14a2 2 0 0 1 2 2v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4a2 2 0 0 1 2-2Z', 'M7 7h.01M7 17h.01M15 7h2M15 17h2'],
    copy: ['M9 8h10a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H9a2 2 0 0 1-2-2V10a2 2 0 0 1 2-2Z', 'M16 8V4a2 2 0 0 0-2-2H4a2 2 0 0 0-2 2v10a2 2 0 0 0 2 2h3'],
    plug: ['M8 3v4M16 3v4M6 7h12v4a6 6 0 0 1-12 0V7ZM12 17v4'],
    bolt: ['m13 2-9 12h7l-1 8 10-12h-7l1-8Z'],
    chevronLeft: ['m15 5-7 7 7 7'], chevronRight: ['m9 5 7 7-7 7'],
    check: ['m5 12 4 4L19 6'], refresh: ['M20 7v5h-5', 'M4 17v-5h5', 'M6.1 6.1A8 8 0 0 1 20 12M4 12a8 8 0 0 0 13.9 5.9'],
    external: ['M14 3h7v7M10 14 21 3', 'M10 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-5'],
    more: ['M5 12h.01M12 12h.01M19 12h.01'], plus: ['M12 5v14M5 12h14'], close: ['m6 6 12 12M6 18 18 6'],
    search: ['M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0Zm-2 6 6 6'], calendar: ['M8 2v4M16 2v4M3 10h18', 'M5 4h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2Z', 'M8 14h2M14 14h2M8 18h2'],
    trash: ['M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7M14 10v7'], restore: ['M3 10a9 9 0 1 1 2 9M3 4v6h6', 'M12 7v5l3 2'], journal: ['M5 3h14v18H5zM9 7h6M9 11h6M9 15h4'],
    activity: ['M2 12h4l3-8 6 16 3-8h4'], alert: ['M12 3 2 21h20L12 3ZM12 9v5M12 18h.01'], terminal: ['m4 6 6 6-6 6M13 18h7'],
    wallet: ['M3 7V5a2 2 0 0 1 2-2h14v4M3 7h17v14H5a2 2 0 0 1-2-2V7ZM15 11h5v6h-5z'],
    key: ['M14 5a5 5 0 1 1-2 8L5 20H2v-3l7-7a5 5 0 0 1 5-5Z'], note: ['M13 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-8', 'm11 13 1-4 7-7 3 3-7 7-4 1Z']
  };
  const svg = document.createElementNS('http://www.w3.org/2000/svg','svg');
  for (const [key,value] of Object.entries({viewBox:'0 0 24 24',fill:'none',stroke:'currentColor','stroke-width':name === 'more' ? '3' : '1.7','stroke-linecap':'round','stroke-linejoin':'round','aria-hidden':'true',focusable:'false',class:'ui-icon ' + extra})) svg.setAttribute(key,value);
  for (const data of paths[name] || paths.server) {const path = document.createElementNS(svg.namespaceURI,'path');path.setAttribute('d',data);svg.append(path);} return svg;
}
function button(text, cls, action, title) { const item = el('button', cls, text); item.type = 'button'; if (action) item.addEventListener('click', action); if (title) {item.title = title; item.setAttribute('aria-label', title);} return item; }
function iconButton(name, title, action) {const item=button('', 'icon-button',action,title);item.append(icon(name));return item;}
function sshCommand(s) {return 'ssh ' + s.alias;}
async function copyText(text) {
  try {if (navigator.clipboard?.writeText) {await navigator.clipboard.writeText(text);return;}} catch (_) { /* Use the local-browser fallback below. */ }
  const previous=document.activeElement, area=el('textarea','clipboard-transfer');area.value=text;area.readOnly=true;area.tabIndex=-1;area.setAttribute('aria-hidden','true');document.body.append(area);
  let copied=false;try {area.select();copied=document.execCommand('copy');} catch (_) {} finally {area.remove();previous?.focus({preventScroll:true});}
  if (!copied) throw new Error('Clipboard unavailable. Copy manually: ' + text);
}
function copySSHButton(s, extra = '') {
  const command=sshCommand(s), label=el('span','','Copy SSH');
  const item=button('','button copy-ssh '+extra,null,'Copy command: '+command);item.dataset.command=command;item.append(icon('copy'),label);
  if (s.archived) {item.disabled=true;item.title='Restore the server from Trash to enable its SSH alias.';item.setAttribute('aria-label',item.title);return item;}
  item.addEventListener('click',safeAction(async () => {
    item.disabled=true;
    try {await copyText(command);item.replaceChildren(icon('check'),el('span','','Copied'));item.classList.add('is-copied');item.setAttribute('aria-label','Copied: '+command);toast('Copied: '+command);}
    finally {item.disabled=false;}
    setTimeout(()=>{item.replaceChildren(icon('copy'),label);item.classList.remove('is-copied');item.setAttribute('aria-label','Copy command: '+command);},1800);
  }));return item;
}
function link(text, url, cls) { const a = el('a', cls, text); a.href = url; a.target = '_blank'; a.rel = 'noopener noreferrer'; return a; }
function toast(text, error = false) { const root=$('#toasts');for(const old of [...root.children])if(old.textContent===text)old.remove();while(root.children.length>=3)root.firstElementChild.remove();const item = el('div', 'toast' + (error ? ' error' : ''), text); root.append(item); setTimeout(() => item.remove(), error ? 10000 : 4500); }
function safeAction(fn) { return async (...args) => {try {await fn(...args);} catch (error) {toast(error.message, true);}}; }
async function requestJSON(path, method, body) {
  const headers = {}; if (method !== 'GET') {headers['Content-Type'] = 'application/json'; headers['X-VPS-CSRF'] = state.csrf;}
  const response = await fetch(path, {method, headers, body: body === undefined ? undefined : JSON.stringify(body), credentials: 'same-origin'});
  const result = await response.json(); if (!response.ok) {const error=new Error(result.error || 'Could not reach the local panel.');error.status=response.status;error.details=result;throw error;} return result;
}
function api(path, method = 'GET', body) {
  if (method === 'GET') {
    if (apiReads.has(path)) return apiReads.get(path);
    const pending = (async () => {
      for (;;) {
        while (apiWrites.size) await Promise.allSettled([...apiWrites]);
        const version = apiVersion, result = await requestJSON(path, method, body);
        if (version === apiVersion) return result;
      }
    })().finally(() => apiReads.delete(path));
    apiReads.set(path, pending); return pending;
  }
  apiVersion++;
  const pending = requestJSON(path, method, body).finally(() => {apiVersion++; apiWrites.delete(pending);});
  apiWrites.add(pending); return pending;
}
function dateTime(value) { if (!value) return 'No data'; const d = new Date(value); return Number.isNaN(d.getTime()) ? 'No data' : dateFormatter.format(d); }
function age(value) { if (!value) return 'not checked'; const n = Math.max(0, (Date.now() - new Date(value)) / 1000); return n < 60 ? 'just now' : n < 3600 ? `${Math.floor(n / 60)}m ago` : n < 86400 ? `${Math.floor(n / 3600)}h ago` : `${Math.floor(n / 86400)}d ago`; }
function bytes(value) { if (value == null) return '—'; if (value < 1024 ** 2) return (value / 1024).toFixed(0) + ' KiB'; if (value < 1024 ** 3) return (value / 1024 ** 2).toFixed(0) + ' MiB'; return (value / 1024 ** 3).toFixed(1) + ' GiB'; }
function pct(value) { return value == null ? '—' : percentFormatter.format(Number(value)) + '%'; }
function duration(seconds) { if (seconds == null) return '—'; const d = Math.floor(seconds / 86400), h = Math.floor(seconds % 86400 / 3600), m = Math.floor(seconds % 3600 / 60); return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`; }
function leaseDays(s) { if (!s.lease_end || !state.rentals) return null; return Math.round((Date.parse(s.lease_end + 'T00:00:00Z') - Date.parse(state.rentals.today + 'T00:00:00Z')) / 86400000); }
function busy(s) {if(busyJobsSource!==state.jobs){busyJobsSource=state.jobs;busyJobs=new Map();for(const job of state.jobs)if(['queued','running'].includes(job.state)&&!busyJobs.has(job.server_id))busyJobs.set(job.server_id,job);}return busyJobs.get(s.id);}
function activeServers() { return state.servers.filter(s => !s.archived); }
function displayError(s) {
  if (!/[А-Яа-яЁё]/.test(s.last_error || '')) return s.last_error || '';
  const messages = {credentials_needed:'Login failed. Check the current user and credentials.',auth_failed:'Login failed. Check the current user and credentials.',host_key_changed:'Host key changed. Verify it before updating the saved fingerprint.',host_key_unknown:'Host key not confirmed. Use access setup.',unreachable:'SSH is unreachable. Check the VPS, port and hosting firewall.',timeout:'SSH connection timed out.',sudo_required:'Root or passwordless sudo is required.',python_required:'Python 3 is required on the server.'};
  return messages[s.error_kind || s.state] || 'The previous SSH check failed. Refresh to check the current status.';
}
function statusBadge(s) {
  const job = busy(s); let text = 'Not checked', cls = '';
  if (job) {text = job.kind === 'onboard' ? 'Setting up SSH' : job.kind === 'check' ? 'Checking SSH' : 'Refreshing'; cls = 'running';}
  else if (s.state === 'ready') {text = 'Available'; cls = 'ready';}
  else if (['credentials_needed','sudo_required','auth_failed'].includes(s.state)) {text = 'Access needed'; cls = 'error';}
  else if (s.state === 'host_key_changed') {text = 'Host key changed'; cls = 'error';}
  else if (!['pending','provisioning'].includes(s.state)) {text = 'Unavailable'; cls = 'error';}
  const badge = append(el('span', 'status ' + cls), el('i','dot'), text); badge.title = job ? job.message : displayError(s) || text; return badge;
}
function rental(s) {
  const days = leaseDays(s), out = el('div', 'rental');
  if (days !== null) {out.classList.add('known'); if (days < 0) out.classList.add('overdue'); else if (days <= 3) out.classList.add('urgent'); else if (days <= 7) out.classList.add('soon');const date=el('time','rental-date',s.lease_end);date.setAttribute('datetime',s.lease_end);append(out,date,el('small','rental-countdown',days<0?`Saved date passed ${-days}d ago`:days===0?'Expires today':`In ${days} days`));}
  else if (s.lease_hint) {const [m,d] = s.lease_hint.split('-'); append(out, `${d}.${m}`, el('small','','Year missing'));}
  else append(out, 'Not set', el('small','','Check hosting account'));
  return out;
}
function metric(value, caption) {const box = el('div', 'metric' + (value >= 85 ? ' hot' : '')); const progress = el('progress'); progress.max = 100; progress.value = Math.max(0, Math.min(100, value || 0)); progress.setAttribute('aria-label', caption || 'Usage'); return append(box, el('strong','',pct(value)), value == null ? null : progress, caption ? el('small','',caption) : null);}
function updateSummary() {
  renderCheckButton();
  const all = activeServers(), online = all.filter(s => s.state === 'ready');
  if ($('#nav-count').textContent !== String(all.length)) $('#nav-count').textContent = all.length;
  const trashed=state.servers.length-all.length,trashCount=$('#trash-count');
  if(trashCount.textContent!==String(trashed))trashCount.textContent=trashed;
  trashCount.classList.toggle('hidden',!trashed);const trashButton=$('#open-trash'),trashTitle=`Trash · ${trashed} servers`;
  if(trashButton.title!==trashTitle){trashButton.title=trashTitle;trashButton.setAttribute('aria-label',`Open Trash (${trashed} servers)`);}
  if(document.hidden || ['pc','burp','listeners','expenses','tasks','diary'].includes(state.view))return;
  $('#stat-total').textContent = all.length; $('#stat-online').textContent = online.length;
  $('#stat-attention').textContent = all.length - online.length;
  $('#stat-rent').textContent = all.filter(s => leaseDays(s) !== null && leaseDays(s) <= 7).length;
  const unknown = all.filter(s => !s.lease_end).length; $('#stat-rent-help').textContent = unknown ? `${unknown} dates need confirmation` : 'including past-due dates';
  const last = all.map(s => s.last_seen).filter(Boolean).sort().pop(); $('#snapshot-caption').textContent = last ? 'Last snapshot: ' + dateTime(last) : 'No snapshots yet';
  const count = state.jobs.filter(j => ['queued','running'].includes(j.state)).length;
  if(state.summaryJobCount!==count){state.summaryJobCount=count;$('#refresh-all').disabled=count>0;$('#refresh-all').replaceChildren(icon('refresh',count?'spin':''),el('span','',count?`Refreshing: ${count}`:'Refresh all'));}
}
function renderServers() {
  updateSummary(); renderRentalAlerts(); renderServerCosts();
  if(document.hidden || !['servers','trash'].includes(state.view))return;
  const archive = state.view === 'trash';
  let rows = state.servers.filter(s => s.archived === archive);
  $('#filter-count').textContent = rows.length;
  if (state.filter === 'ready') rows = rows.filter(s => s.state === 'ready');
  rows.sort((a,b) => Number(b.state === 'ready') - Number(a.state === 'ready') || (a.provider || '').localeCompare(b.provider || '', undefined, {sensitivity:'base'}));
  if (state.filter === 'attention') rows = rows.filter(s => s.state !== 'ready');
  if (state.search) rows = rows.filter(s => `${s.name} ${s.alias} ${s.host} ${s.provider} ${s.notes} ${s.short_note || ''}`.toLowerCase().includes(state.search));
  const body = $('#server-rows'), nodes = [];
  for (const s of rows) {
    const snap=s.snapshot, job=busy(s);
    const signature=JSON.stringify([archive,s.name,s.alias,s.host,state.countries?.[s.host],s.port,s.ssh_user,s.provider,s.provider_url,s.price,serverCostSignature(s),s.state,s.last_seen,s.ssh_latency_ms,s.ssh_checked_at,s.last_error,s.error_kind,s.notes,s.short_note,s.lease_end,s.lease_hint,leaseDays(s),snap&&[snap.cpu_pct,snap.cpu_count,snap.memory,snap.disk],job&&[job.id,job.kind,job.message]]);
    const previous=state.serverRows.get(s.id);
    if(previous?.signature===signature){const label=$('.age',previous.node),text=s.last_seen?'snapshot '+age(s.last_seen):'no snapshot';if(label.textContent!==text)label.textContent=text;nodes.push(previous.node);continue;}
    const tr = el('tr'); tr.dataset.serverId = s.id;tr.dataset.state=s.state==='ready'?'ready':'attention';
    const name = button(s.name, 'server-name', safeAction(() => openServer(s.id)));
    const subtitle = append(el('span','server-subtitle'),countryFlag(s.host),el('span','server-address',s.host)); subtitle.title = s.host;
    const provider=el('span','server-provider',s.provider || 'Hosting not set');provider.title=provider.textContent;
    const identity = append(el('div','server-identity'), append(el('div','server-icon'),icon('server')), append(el('div','server-label'), name, subtitle,provider));
    const status = append(el('td','status-cell'),statusBadge(s),el('small','age',s.last_seen ? 'snapshot ' + age(s.last_seen) : 'no snapshot'));
    if(s.ssh_checked_at&&(Number.isFinite(s.ssh_latency_ms)||s.state!=='ready')){const latency=el('small','ssh-latency',Number.isFinite(s.ssh_latency_ms)?'SSH '+Math.round(s.ssh_latency_ms)+' ms':'SSH check failed');latency.title='Full SSH connection and authentication time · '+dateTime(s.ssh_checked_at);status.append(latency);}
    if (s.last_seen) status.title = 'Last successful check: ' + dateTime(s.last_seen);
    const cpu = snap ? metric(snap.cpu_pct, snap.cpu_count + ' vCPU') : el('span','muted','—');
    const memory = snap ? metric(snap.memory.pct, bytes(snap.memory.used) + ' / ' + bytes(snap.memory.total)) : el('span','muted','—');
    const disk = snap ? metric(snap.disk.pct, bytes(snap.disk.used) + ' / ' + bytes(snap.disk.total)) : el('span','muted','—');
    const controls = el('div','actions');
    if (archive) {const restore=button('Restore','button secondary restore-server',safeAction(()=>restoreServer(s)));restore.disabled=!!job;controls.append(restore);}
    else {controls.append(copySSHButton(s));const refresh = iconButton('refresh','Refresh ' + s.name,safeAction(() => refreshServer(s.id))); refresh.disabled = !!busy(s);if(busy(s))refresh.firstChild.classList.add('spin'); controls.append(refresh);}
    if (s.provider_url) {const host = link('',s.provider_url,'icon-button hosting');host.append(icon('external')); host.title = 'Hosting account: ' + s.provider; host.setAttribute('aria-label',host.title); controls.append(host);}
    else controls.append(iconButton('external','Add hosting URL',() => editListedServer(s.id)));
    if(!archive)controls.append(iconButton('more','Edit ' + s.name,() => editListedServer(s.id)));
    const remove=iconButton('trash',archive?'Delete permanently: '+s.name:'Move to Trash: '+s.name,()=>openDelete(s,archive));remove.classList.add(archive?'delete-permanently':'move-trash');remove.disabled=!!job;controls.append(remove);
    const cpuCell=append(el('td','resource-cell cpu-cell'),cpu);cpuCell.dataset.label='CPU';
    const memCell=append(el('td','resource-cell memory-cell'),memory);memCell.dataset.label='Memory';
    const diskCell=append(el('td','resource-cell disk-cell'),disk);diskCell.dataset.label='Disk /';
    const leaseCell=append(el('td','lease-cell'),rental(s),serverCost(s));leaseCell.dataset.label='Rental expiry';
    const noteCell=append(el('td','note-cell'),listNoteButton(s));noteCell.dataset.label='Note';
    append(tr, append(el('td','name-cell'),identity), status, noteCell, cpuCell, memCell, diskCell, leaseCell, append(el('td','actions-cell'),controls));
    state.serverRows.set(s.id,{signature,node:tr});nodes.push(tr);
  }
  const visibleNodes=new Set(nodes);
  for(const node of [...body.children])if(!visibleNodes.has(node))node.remove();
  nodes.forEach((node,index)=>{const current=body.children[index];if(current===node)return;if(current?.dataset.serverId===node.dataset.serverId)body.replaceChild(node,current);else body.insertBefore(node,current||null);});
  const ids=new Set(state.servers.map(s=>s.id));for(const id of state.serverRows.keys())if(!ids.has(id))state.serverRows.delete(id);
  $('#empty-state').classList.toggle('hidden', rows.length > 0);
  $('#empty-state').textContent = archive ? (state.search?'No matching servers in Trash.':'Trash is empty.') : 'No matching servers.';
}
function editListedServer(id) {const server=state.servers.find(item=>item.id===id);if(server)openForm('edit',server);}
function reload() {
  if(state.reloadPromise)return state.reloadPromise;
  state.reloadPromise=(async()=>{const result=await api('/api/servers');state.servers=result.servers;state.jobs=result.jobs;state.rentals=result.rentals;acceptExpenses(result.expenses);renderServers();renderExpenses();renderJob();schedulePoll();if(state.countryHosts!==undefined)loadCountries();})().finally(()=>{state.reloadPromise=null;});
  return state.reloadPromise;
}
function listNoteButton(s) {
  const short=String(s.short_note || '').replace(/\s+/g,' ').trim();
  const long=String(s.notes || '').replace(/\s+/g,' ').trim();
  const preview=short || long, fullNote=!short && !!long;
  const item=button('','quick-note-button server-note-preview'+(preview?' has-note':''),safeAction(async()=>{
    if(!fullNote)return openQuickNote(s.id);
    await openServer(s.id);
    if(state.selected!==s.id || !state.detail)return;
    state.tab='notes';renderTabs();renderDrawerContent();$('#notes-editor')?.focus();
  }),`${preview?'Edit':'Add'} ${fullNote?'notes':'quick note'} for ${s.name}`);
  item.dataset.noteKind=fullNote?'notes':'short_note';
  const text=preview.length>260?preview.slice(0,259)+'…':preview;
  item.append(icon('note'),el('span','server-note-text',text || 'Add note'));
  if(preview)item.title=preview.length>600?preview.slice(0,599)+'…':preview;
  return item;
}
function quickNoteButton(s) {
  const b=button('', 'quick-note-button'+(s.short_note?' has-note':''),safeAction(()=>openQuickNote(s.id)),s.short_note?'Edit quick note: '+s.short_note:'Add quick note for '+s.name);
  b.append(icon('note'),el('span','',s.short_note || 'Add quick note'));return b;
}
async function openQuickNote(id) {
  const {server}=await api('/api/servers/'+id);state.quickNoteId=id;
  $('#quick-note-server').textContent=server.name;$('#quick-note-input').value=server.short_note || '';
  $('#quick-note-error').classList.add('hidden');updateQuickNoteCount();$('#quick-note-modal').showModal();$('#quick-note-input').focus();
}
function updateQuickNoteCount() {$('#quick-note-count').textContent=$('#quick-note-input').value.length+' / 160';}
async function saveQuickNote(event) {
  event.preventDefault();const id=state.quickNoteId;$('#quick-note-save').disabled=true;$('#quick-note-error').classList.add('hidden');
  try {
    const {server}=await api('/api/servers/'+id,'PATCH',{short_note:$('#quick-note-input').value});
    if(state.detail?.id===id){state.detail.short_note=server.short_note;if(state.tab!=='notes')renderDrawerContent();}
    $('#quick-note-modal').close();await reload();toast('Quick note saved.');
  } catch(error) {$('#quick-note-error').textContent=error.message;$('#quick-note-error').classList.remove('hidden');}
  finally {$('#quick-note-save').disabled=false;}
}
async function editRental(id) {const {server}=await api('/api/servers/'+id);openForm('edit',server);field('lease_end').focus();}
function renderRentalAlerts() {
  const data=state.rentals, panel=$('#rental-alerts'), visible=state.view==='servers';panel.classList.toggle('hidden',!visible);if(!data||!visible||document.hidden)return;
  const signature=JSON.stringify([data,state.view]);if(state.rentalSignature===signature)return;state.rentalSignature=signature;
  $('#rental-alert-count').textContent=data.alerts.length;$('#rental-alert-count').classList.toggle('has-alerts',data.alerts.length>0);
  const settings=data.notifications;$('#desktop-reminders').checked=settings.enabled;$('#desktop-reminders').disabled=!settings.available;$('#notification-preview').disabled=!settings.available;
  const status=$('#notification-status');status.classList.toggle('delivery-error',!!settings.error);
  status.textContent=settings.error || (!settings.available?'Desktop notifications unavailable: install notify-send.':settings.enabled?'Daily reminders from 7 days before expiry, while the local service is running.':'Desktop reminders off. In-panel alerts remain enabled.');
  status.title='Dates use '+data.timezone+'. Desktop notification settings, including Do Not Disturb, may hide notifications.';
  const list=$('#rental-alert-list');list.replaceChildren();
  for(const item of data.alerts) {
    const row=el('div','rental-alert-row '+item.level);row.dataset.serverId=item.id;
    const info=append(el('div','rental-alert-info'),button(item.name,'rental-server-name',safeAction(()=>openServer(item.id))),el('span','rental-alert-message',item.message),el('time','',item.lease_end));
    const actions=el('div','rental-alert-actions');if(item.provider_url)actions.append(link('Hosting ↗',item.provider_url,'text-button'));
    actions.append(button('Update date','text-button',safeAction(()=>editRental(item.id))));list.append(append(row,icon('calendar'),info,actions));
  }
  if(!data.alerts.length)list.append(el('p','no-rental-alerts',data.undated.length?'No confirmed dates due within 7 days.':'No renewals due within 7 days.'));
  const missing=$('#missing-dates');missing.classList.toggle('hidden',!data.undated.length);
  $('#missing-dates-summary').textContent=data.undated.length+' servers need a full expiry date';
  const undated=$('#missing-date-list');undated.replaceChildren();
  for(const item of data.undated) {
    const row=el('div','missing-date-row');row.dataset.serverId=item.id;
    append(row,el('strong','',item.name),el('span','',item.lease_hint?'Saved month/day: '+item.lease_hint+' · year missing':'No date saved'));
    if(item.provider_url)row.append(link('Hosting ↗',item.provider_url,'text-button'));
    row.append(button('Set date','text-button',safeAction(()=>editRental(item.id))));undated.append(row);
  }
}
function scheduleRentalPoll() {clearTimeout(state.rentalTimer);state.rentalTimer=document.hidden?null:setTimeout(pollRentals,60000);}
function pollRentals() {
  clearTimeout(state.rentalTimer);
  state.rentalTimer=null;if(document.hidden)return Promise.resolve();if(state.rentalPromise)return state.rentalPromise;
  state.rentalPromise=(async()=>{
    try {
      const [{rentals}]=await Promise.all([api('/api/rental-alerts'),state.view==='expenses'?loadExpenses({quiet:true}):Promise.resolve()]);
      if(JSON.stringify(rentals)!==JSON.stringify(state.rentals))await reload();else renderServers();
    }
    catch(error){$('#notification-status').textContent='Could not update rental alerts. Retrying…';state.rentalSignature='';}
    finally {state.rentalPromise=null;scheduleRentalPoll();}
  })();return state.rentalPromise;
}
async function setDesktopReminders() {
  const toggle=$('#desktop-reminders');toggle.disabled=true;
  try {const result=await api('/api/rental-settings','POST',{enabled:toggle.checked});state.rentals=result.rentals;}
  catch(error){toast(error.message,true);}
  finally {state.rentalSignature='';renderRentalAlerts();}
}
async function previewNotification() {
  const b=$('#notification-preview');b.disabled=true;
  try {const result=await api('/api/rental-notification-preview','POST',{});state.rentals=result.rentals;toast('Preview sent. If hidden, check desktop notification settings.');}
  catch(error){toast(error.message,true);}
  finally {b.disabled=false;state.rentalSignature='';renderRentalAlerts();}
}
function hasPendingJobs() {return state.completedJobs.size>0||state.jobs.some(j=>['queued','running'].includes(j.state));}
function schedulePoll() {if (!document.hidden && !state.pollTimer && !state.pollPromise && hasPendingJobs()) state.pollTimer = setTimeout(poll,1300);}
function poll() {
  clearTimeout(state.pollTimer);state.pollTimer=null;if(document.hidden)return Promise.resolve();if(state.pollPromise)return state.pollPromise;
  state.pollPromise=pollJobs().finally(()=>{state.pollPromise=null;schedulePoll();});return state.pollPromise;
}
async function pollJobs() {
  const oldJobs = new Map(state.jobs.map(j => [j.id,j.state]));
  try {
    const result=await api('/api/jobs');state.jobs=result.jobs;
    for(const job of state.jobs)if(['done','error'].includes(job.state)&&['queued','running'].includes(oldJobs.get(job.id)))state.completedJobs.set(job.id,job);
    const completed=[...state.completedJobs.values()];
    if(completed.length)await reload();else{renderServers();renderJob();}
    if (completed.some(j => j.server_id === state.selected)) {
      const result = await api('/api/servers/' + state.selected); if (result.server.id === state.selected) {state.detail = result.server; renderDrawerHeader(); if (state.tab !== 'notes') renderDrawerContent();}
    }
    if (state.view === 'events' && completed.length) await renderEvents();
    for(const job of completed)state.completedJobs.delete(job.id);
  } catch (error) {toast(error.message,true);clearTimeout(state.pollTimer);state.pollTimer=null;if(!document.hidden&&hasPendingJobs())state.pollTimer=setTimeout(poll,5000);}
}
async function refreshServer(id) {const result = await api('/api/servers/' + id + '/refresh','POST',{}); state.jobs=[...state.jobs,result.job]; await reload(); if (state.selected === id) renderDrawerHeader();}
async function refreshAll() {await api('/api/refresh-all','POST',{}); await reload(); toast('Refreshing SSH snapshots.');}
function renderCheckButton() {
  const b=$('#check-all');if(!b||document.hidden||!['servers','trash'].includes(state.view))return;
  if(!b.firstChild)b.append(icon('bolt'));
  const running=state.jobs.filter(j=>j.kind==='check'&&['queued','running'].includes(j.state)).length;
  const pending=!!state.checkSubmitting||running>0;
  b.disabled=pending||activeServers().length===0;b.classList.toggle('checking',pending);b.classList.toggle('hidden',state.view==='trash');
  b.setAttribute('aria-busy',String(pending));b.title=running?`Checking SSH: ${running} remaining`:'Check all servers: SSH connection time (ms)';b.setAttribute('aria-label',b.title);
}
async function checkAll() {
  if(state.checkSubmitting||state.jobs.some(j=>j.kind==='check'&&['queued','running'].includes(j.state)))return;
  state.checkSubmitting=true;renderCheckButton();
  try {
    const result=await api('/api/check-all','POST',{});
    const jobs=new Map(state.jobs.map(j=>[j.id,j]));for(const job of result.jobs)jobs.set(job.id,job);state.jobs=[...jobs.values()];
    if(result.jobs.some(j=>['done','error'].includes(j.state)))await reload();
    renderServers();schedulePoll();
    toast(`Checking SSH on ${result.jobs.length} servers.`+(result.skipped.length?` ${result.skipped.length} busy servers skipped.`:''));
  } finally {state.checkSubmitting=false;renderCheckButton();}
}
async function changeView(view) {
  if(state.view==='diary'&&view!=='diary')flushDiary();
  if(state.view!==view&&['pc','burp','listeners'].includes(state.view))releaseDesktopContent();
  if(state.view==='expenses'&&view!=='expenses')releaseExpenses();
  if((view==='trash')!==(state.view==='trash')){state.search='';$('#search').value='';}
  state.view = view; state.filter = 'all'; $$('.nav-button').forEach(b => b.classList.toggle('selected',b.dataset.view === (view==='trash'?'servers':view))); $$('[data-filter]').forEach(b => b.classList.toggle('selected',b.dataset.filter === 'all'));
  scheduleDesktopPoll();
  const titles = {servers:'Servers',trash:'Trash',expenses:'Expenses',tasks:'Tasks',diary:'Diary',events:'Activity',pc:'PC updates',burp:'Burp',listeners:'Listening ports'};
  $('#page-title').textContent = titles[view];
  $('#page-context').textContent = {pc:'This PC / Packages',listeners:'This PC / Network',burp:'This PC / Applications',servers:'VPS / Overview',trash:'VPS / Trash',expenses:'Local Desk / Costs',tasks:'Local Desk / Tasks',diary:'Local Desk / Personal notes',events:'Local Desk / Activity'}[view];
  $('#local-header-actions').replaceChildren();document.body.dataset.view=view;desktopState.signature='';
  const localView=['pc','burp','listeners'].includes(view);
  $('.key-card').classList.toggle('hidden',view!=='servers');
  $('#server-section').classList.toggle('hidden',!['servers','trash'].includes(view)); $('#events-section').classList.toggle('hidden',view !== 'events');$('#expenses-section').classList.toggle('hidden',view!=='expenses');
  $('#tasks-section').classList.toggle('hidden',view!=='tasks');
  $('#diary-section').classList.toggle('hidden',view!=='diary');
  $('#desktop-section').classList.toggle('hidden',!localView);$('.stats').classList.toggle('hidden',localView||['trash','expenses','tasks','diary'].includes(view));$('.header-actions').classList.toggle('hidden',!['servers','trash'].includes(view));
  $('#trash-caption').classList.toggle('hidden',view!=='trash');$('#back-servers').classList.toggle('hidden',view!=='trash');$('#refresh-all').classList.toggle('hidden',view==='trash');$('#add-server').classList.toggle('hidden',view==='trash');$('#open-trash').setAttribute('aria-pressed',String(view==='trash'));
  renderServers(); if (view === 'events') await renderEvents();
  if(view==='expenses')await loadExpenses();
  if(view==='tasks')await loadTasks();
  if(view==='diary')await loadDiary();
  if(view==='listeners')await loadListeners();else if(localView){renderDesktop();await loadDesktop(view);}else scheduleDesktopPoll();
}
async function renderEvents() {const result = await api('/api/events'); const root = $('#events-section'); root.replaceChildren(); if (!result.events.length) root.append(el('div','empty','No activity yet.')); for (const item of result.events) root.append(append(el('article','event-row'),el('i','dot ' + (item.kind === 'success' ? 'green' : item.kind === 'error' ? 'amber' : 'indigo')),append(el('div'),el('strong','',item.server_name || 'Local Desk'),el('p','',item.message),el('time','',dateTime(item.at)))));}
const tabs = [['overview','Overview'],['processes','Processes'],['services','Services'],['containers','Containers'],['packages','Software'],['notes','Notes']];
async function openServer(id) {
  if (state.selected && state.selected !== id) {flushNotes(state.selected);releaseNotes(state.selected);}
  cancelPackageSearch();$('#drawer-header').replaceChildren();$('#drawer-tabs').replaceChildren();
  state.selected = id; state.tab = 'overview'; state.detail = null;
  $('#drawer').classList.remove('hidden'); $('#drawer-backdrop').classList.remove('hidden'); $('#drawer-content').replaceChildren(el('div','empty','Loading…'));
  const result = await api('/api/servers/' + id); if (state.selected !== id) return; state.detail = result.server; renderDrawerHeader(); renderTabs(); renderDrawerContent(); $('#drawer').focus();
}
function cancelPackageSearch(){clearTimeout(state.packageTimer);state.packageTimer=null;}
function closeDrawer() {
  const id=state.selected;if(id)flushNotes(id);state.selected=null;state.detail=null;cancelPackageSearch();
  $('#drawer').classList.add('hidden');$('#drawer-backdrop').classList.add('hidden');
  for(const selector of ['#drawer-header','#drawer-tabs','#drawer-content'])$(selector).replaceChildren();
  if(id)releaseNotes(id);
}
function renderDrawerHeader() {
  const s = state.detail; if (!s) return;
  const top = append(el('div','drawer-top'),append(el('div'),el('div','eyebrow',s.provider || 'VPS'),el('h2','',s.name),el('p','drawer-subtitle',`${s.ssh_user}@${s.host}:${s.port} · ${s.alias}`),statusBadge(s)),iconButton('close','Close details',closeDrawer));
  const actions = el('div','drawer-buttons');
  const refresh = button('↻ Refresh','button primary',safeAction(() => refreshServer(s.id))); refresh.disabled = !!busy(s); if (!s.archived) actions.append(refresh);
  actions.append(copySSHButton(s),button('Edit','button secondary',() => openForm('edit',s)));
  if (s.provider_url) actions.append(link('Hosting ↗',s.provider_url,'button secondary'));
  else actions.append(button('Add hosting URL','button secondary',() => openForm('edit',s)));
  const commandLine=append(el('div','ssh-command-line'),icon('terminal'),el('code','',sshCommand(s)),el('span','',s.archived?'In Trash · SSH alias disabled':'managed key'));
  $('#drawer-header').replaceChildren(top,actions,commandLine);
}
function renderTabs() {const root = $('#drawer-tabs'); root.replaceChildren(); for (const [id,name] of tabs) root.append(button(name,state.tab === id ? 'selected' : '',() => {if (state.tab === 'notes') flushNotes(state.selected); state.tab = id; renderTabs(); renderDrawerContent();}));}
function section(title) {return append(el('section','detail-section'),el('h3','',title));}
function kv(items) {const result = el('dl','kv'); for (const [name,value] of items) append(result,el('dt','',name),value instanceof Node ? append(el('dd'),value) : el('dd','',value)); return result;}
function table(headers, rows) {const out = el('table','detail-table'), head = el('tr'); headers.forEach(x => head.append(el('th','',x))); out.append(append(el('thead'),head)); const body = el('tbody'); for (const row of rows) {const tr = el('tr'); row.forEach(v => tr.append(v instanceof Node ? append(el('td'),v) : el('td','',v ?? '—'))); body.append(tr);} out.append(body); return append(el('div','table-scroll'),out);}
function renderDrawerContent() {
  cancelPackageSearch();const s = state.detail, root = $('#drawer-content'); if (!s) return; root.replaceChildren();
  if (state.tab === 'notes') return renderNotes(root,s);
  if (state.tab === 'overview') {
    if (s.last_error) {
      const notice = append(el('div','notice'),el('p','',displayError(s)));
      if (!busy(s) && !s.archived) {
        const action = button('Set up access','button secondary',() => openForm('onboard',s)); notice.append(action);
        if (s.error_kind === 'host_key_changed') notice.append(button('Update host key','button secondary',safeAction(() => replaceHostKey(s))));
      } root.append(notice);
    }
    if (s.snapshot) renderOverview(root,s);
    else root.append(el('div','notice neutral','No snapshot yet. Refresh to connect with your SSH key, or set up access first.'));
    const meta = section('Details'); append(meta,kv([['Name',s.name],['SSH alias',s.alias],['Hosting',s.provider_url ? link(s.provider || 'Open hosting ↗',s.provider_url) : s.provider || 'Not set'],['Rental expiry',rental(s)],['Cost',serverCost(s)],['Quick note',quickNoteButton(s)]]));
    const notes = state.notes.get(s.id)?.value ?? s.notes;
    if (notes) {const preview = el('p','readonly-note',notes.length > 250 ? notes.slice(0,250) + '…' : notes); append(meta,preview);}
    meta.append(button('Notes','button secondary note-button',() => {state.tab='notes';renderTabs();renderDrawerContent();})); root.append(meta);
    const manage = section(s.archived?'Trash':'Connection');
    if(s.archived){append(manage,el('p','detail-caption','Restore this entry to enable SSH actions.'),button('Restore server','button secondary restore-server',safeAction(()=>restoreServer(s))),button('Delete permanently','button danger delete-permanently',()=>openDelete(s,true)));}
    else {append(manage,el('p','detail-caption','Refresh is read-only. Access setup changes SSH configuration.'),button('Set up root + SSH key','button secondary',() => openForm('onboard',s)),button('Move to Trash','button secondary move-trash',()=>openDelete(s)));}
    if(busy(s))$$('button',manage).forEach(item=>item.disabled=true);root.append(manage);
    return;
  }
  if (!s.snapshot) return root.append(el('div','empty','Refresh to collect a snapshot first.'));
  const snap = s.snapshot; root.append(el('p','detail-caption','Snapshot: ' + dateTime(snap.collected_at) + '. Manual refresh only.'));
  if (state.tab === 'processes') {
    root.append(el('p','detail-caption','Up to 60 processes. CPU 100% = one core; ~0.65s sample. Command arguments and environment variables are not collected.'));
    root.append(table(['Process / user','PID','CPU','RAM','Running'],snap.processes.map(p => [append(el('div'),el('span','',p.name),el('small','',p.user)),p.pid,pct(p.cpu_pct),bytes(p.memory_bytes),duration(p.age_seconds)])));
  } else if (state.tab === 'services') {
    root.append(el('p','detail-caption','Running systemd services. CPU 100% = one core. Memory covers the service where available, otherwise its main process.'));
    if (!snap.services.length) root.append(el('div','empty','No running systemd services found.'));
    else root.append(table(['Service','CPU','RAM','PID'],snap.services.map(svc => [append(el('div'),el('span','',svc.name),el('small','',svc.description)),pct(svc.cpu_pct),append(el('div'),bytes(svc.memory_bytes),el('small','',svc.memory_scope === 'service' ? 'whole service' : 'main process')),svc.pid])));
  } else if (state.tab === 'containers') {
    if (!snap.containers.length) root.append(el('div','empty','No Docker containers found.'));
    for (const c of snap.containers) {const card = el('article','container-card'); const badge = el('span','status ' + (c.state === 'running' ? 'ready' : ''),c.state === 'running' ? 'Running' : 'Stopped'); append(card,append(el('h3'),c.name,badge),el('p','',c.image),el('p','',c.status),el('p','',c.ports || 'No published ports'),append(el('div','container-metrics'),el('span','','CPU: ' + (c.cpu || '—')),el('span','','RAM: ' + (c.memory || '—')))); root.append(card);}
    root.append(el('p','readonly-note','Read-only. Containers are not started or restarted.'));
  } else if (state.tab === 'packages') {
    root.append(el('p','detail-caption',`${snap.packages.length} packages · ${snap.manual_tools.length} standalone tools. Inventory only; update availability is not checked.`));
    renderSoftware(root,s);
  }
}
function renderSoftware(root,s) {
  const input=el('input','list-search');input.type='search';input.placeholder='Find package or tool…';input.setAttribute('aria-label','Find package or tool');
  const results=el('div'),pager=el('div','table-footer software-pagination'),range=el('span');range.id='package-range';range.setAttribute('aria-live','polite');
  const entries=[...s.snapshot.manual_tools.map(item=>({item,manual:true,search:`${item.name} ${item.path}`.toLowerCase()})),...s.snapshot.packages.map(item=>({item,manual:false,search:`${item.name} ${item.version}`.toLowerCase()}))];
  let page=0,query=null,filtered=[];const size=100;
  const previous=button('Previous','text-button',()=>{page--;render();});previous.id='package-previous';
  const next=button('Next','text-button',()=>{page++;render();});next.id='package-next';
  pager.append(range,append(el('div','update-actions'),previous,next));root.append(input,results,pager);
  function render(){
    cancelPackageSearch();if(state.selected!==s.id||state.tab!=='packages'||!input.isConnected)return;
    const value=input.value.trim().toLowerCase();if(value!==query){query=value;page=0;filtered=entries.filter(entry=>entry.search.includes(query));}
    page=Math.max(0,Math.min(page,Math.ceil(filtered.length/size)-1));const start=page*size,visible=filtered.slice(start,start+size),manual=visible.filter(entry=>entry.manual),packages=visible.filter(entry=>!entry.manual);
    results.replaceChildren();
    if(manual.length)append(results,el('h3','','Standalone tools'),table(['Name','Path'],manual.map(({item})=>[item.name,item.path])));
    if(packages.length)append(results,el('h3','','System packages'),table(['Package','Version'],packages.map(({item})=>[item.name,item.version])));
    if(!filtered.length)results.append(el('div','empty','No matching packages or tools.'));
    pager.classList.toggle('hidden',filtered.length<=size);range.textContent=filtered.length?`${start+1}–${Math.min(start+size,filtered.length)} of ${filtered.length}`:'';previous.disabled=page===0;next.disabled=start+size>=filtered.length;
  }
  input.addEventListener('input',()=>{page=0;cancelPackageSearch();state.packageTimer=setTimeout(render,120);});render();
}
function renderOverview(root,s) {
  const snap=s.snapshot;
  if (s.state !== 'ready') root.append(el('p','detail-caption','Cached snapshot, not live status: ' + dateTime(snap.collected_at)));
  const metrics = el('div','detail-grid'); for (const [title,value,caption] of [['CPU',snap.cpu_pct,snap.cpu_count + ' vCPU'],['Memory',snap.memory.pct,bytes(snap.memory.used) + ' / ' + bytes(snap.memory.total)],['Disk /',snap.disk.pct,bytes(snap.disk.used) + ' / ' + bytes(snap.disk.total)]]) {const card = el('article','detail-metric metric'); const bar=el('progress');bar.max=100;bar.value=value;bar.setAttribute('aria-label',title);append(card,el('span','',title),el('strong','',pct(value)),el('small','',caption),bar);metrics.append(card);} root.append(metrics);
  if (s.history.length >= 2) {const history=section('CPU history');const ns='http://www.w3.org/2000/svg',svg=document.createElementNS(ns,'svg');svg.setAttribute('viewBox','0 0 600 70');svg.setAttribute('class','chart');svg.setAttribute('role','img');svg.setAttribute('aria-label','CPU usage history');const line=document.createElementNS(ns,'polyline');line.setAttribute('points',s.history.map((p,i)=>`${i*600/(s.history.length-1)},${65-Math.max(0,Math.min(100,p.cpu))*0.6}`).join(' '));line.setAttribute('fill','none');line.setAttribute('stroke','#8588e5');line.setAttribute('stroke-width','2');svg.append(line);append(history,svg,el('p','chart-caption',`${s.history.length} snapshots, ordered by check, not equal time intervals`));root.append(history);}
  const system=section('System and uptime');append(system,kv([['OS',snap.os],['Hostname',snap.hostname],['Kernel',snap.kernel],['Uptime',duration(snap.uptime_seconds)],['Last boot',dateTime(snap.boot_at)],['First recorded boot',dateTime(s.first_boot)],['Snapshot collected',dateTime(snap.collected_at)]]),el('p','readonly-note','First recorded boot is not the purchase date. Confirm creation and rental dates with your hosting provider.'));root.append(system);
  if (snap.failed_units.length) {const failed=section('Failed services');failed.append(el('pre','ports',snap.failed_units.join('\n')));root.append(failed);}
  const ports=section('Listening ports');ports.append(el('pre','ports',snap.ports.join('\n') || 'None found'));root.append(ports);
}
function noteState(s) {if (!state.notes.has(s.id)) state.notes.set(s.id,{value:s.notes,dirty:false,saving:false,timer:null,error:false});return state.notes.get(s.id);}
function releaseNotes(id){const draft=state.notes.get(id);if(draft&&!draft.dirty&&!draft.saving&&!draft.error){clearTimeout(draft.timer);state.notes.delete(id);}}
function renderNotes(root,s) {
  const draft=noteState(s),status=el('span','save-status');status.id='note-save-status';
  append(root,append(el('div','note-heading'),el('h3','','Notes'),status),el('p','detail-caption','Saved locally.'));
  const area=el('textarea','notes-editor');area.id='notes-editor';area.maxLength=16000;area.placeholder='Server notes';area.setAttribute('aria-label','Server notes');area.value=draft.value;
  area.addEventListener('input',()=>{draft.value=area.value;draft.dirty=true;draft.error=false;clearTimeout(draft.timer);draft.timer=setTimeout(()=>flushNotes(s.id),700);updateNoteStatus(s.id);});
  append(root,area,append(el('div','notes-footer'),el('span','','Do not store passwords or private keys here.'),button('Save','button secondary',()=>flushNotes(s.id))));updateNoteStatus(s.id);
}
function updateNoteStatus(id) {if (state.selected!==id || state.tab!=='notes')return;const node=$('#note-save-status'),draft=state.notes.get(id);if(node&&draft)node.textContent=draft.error?'Failed — retry saving':draft.saving?'Saving…':draft.dirty?'Unsaved changes':'Saved locally';}
async function flushNotes(id) {
  const draft=state.notes.get(id);if(!draft)return;clearTimeout(draft.timer);if(!draft.dirty||draft.saving)return;draft.saving=true;draft.error=false;const value=draft.value;updateNoteStatus(id);
  try {const result=await api('/api/servers/'+id,'PATCH',{notes:value});draft.dirty=draft.value!==value;const s=state.servers.find(s=>s.id===id);if(s)s.notes=value;if(state.detail?.id===id)state.detail.notes=value;renderServers();}
  catch(error){draft.error=true;toast('Note not saved: '+error.message,true);}
  finally {draft.saving=false;updateNoteStatus(id);if(draft.dirty&&!draft.error)draft.timer=setTimeout(()=>flushNotes(id),100);else if(state.selected!==id)releaseNotes(id);}
}
async function settleServerNotes(id) {
  const draft=state.notes.get(id);if(!draft)return;
  if(draft.saving)await Promise.allSettled([...apiWrites]);
  await flushNotes(id);
  if(draft.dirty||draft.saving||draft.error)throw new Error('Save your notes successfully before changing this entry.');
}
async function restoreServer(s) {
  await settleServerNotes(s.id);await api('/api/servers/'+s.id,'PATCH',{archived:false});
  if(state.selected===s.id)closeDrawer();await reload();toast('Server restored.');
}
function openDelete(server,permanent=false) {
  const s=state.servers.find(item=>item.id===server.id)||server;
  if(permanent&&(!s.archived||state.view!=='trash'))return toast('Permanent deletion is only available from Trash.',true);
  if(!permanent&&s.archived)return;
  if(busy(s))return toast('Wait for this server’s current task to finish.',true);
  state.deletion={id:s.id,alias:s.alias,name:s.name,permanent};state.deleteBusy=false;
  $('#delete-title').textContent=permanent?'Delete permanently?':'Move to Trash?';
  $('#delete-identity').textContent=s.name+' · '+s.alias;
  $('#delete-description').textContent=permanent?'Removes this entry, saved notes, dates and snapshots. The VPS, subscription and SSH keys are unchanged.':'You can restore this entry from Trash. Its SSH alias and rental reminders will be disabled; the VPS, subscription and keys are unchanged.';
  $('#delete-confirmation').classList.toggle('hidden',!permanent);$('#delete-expected-alias').textContent=s.alias;
  $('#delete-alias').value='';$('#delete-alias').required=permanent;$('#delete-alias').disabled=!permanent;
  $('#delete-error').classList.add('hidden');$('#delete-submit').textContent=permanent?'Delete permanently':'Move to Trash';
  syncDeleteConfirm();$('#delete-modal').showModal();(permanent?$('#delete-alias'):$('#delete-modal [data-close]')).focus();
}
function syncDeleteConfirm(){const target=state.deletion;$('#delete-submit').disabled=state.deleteBusy||!target||(target.permanent&&$('#delete-alias').value!==target.alias);}
async function submitDelete(event) {
  event.preventDefault();const target=state.deletion;
  if(!target||state.deleteBusy||(target.permanent&&$('#delete-alias').value!==target.alias))return;
  state.deleteBusy=true;syncDeleteConfirm();$$('[data-close="delete-modal"]').forEach(item=>item.disabled=true);$('#delete-error').classList.add('hidden');
  try {
    await settleServerNotes(target.id);
    if(target.permanent)await api('/api/servers/'+target.id,'DELETE',{confirm_alias:target.alias});
    else await api('/api/servers/'+target.id,'PATCH',{archived:true});
    if(state.selected===target.id)closeDrawer();
    if(target.permanent){const draft=state.notes.get(target.id);if(draft)clearTimeout(draft.timer);state.notes.delete(target.id);state.serverRows.delete(target.id);}
    $('#delete-modal').close();await reload();toast(target.permanent?'Saved server entry deleted. VPS unchanged.':'Moved to Trash. VPS unchanged.');
  } catch(error) {if($('#delete-modal').open){$('#delete-error').textContent=error.message;$('#delete-error').classList.remove('hidden');}else toast(error.message,true);}
  finally {state.deleteBusy=false;$$('[data-close="delete-modal"]').forEach(item=>item.disabled=false);syncDeleteConfirm();}
}
async function replaceHostKey(s) {if(!confirm(`The host key for ${s.host} changed. This may follow a reinstall or indicate an intercepted connection.\n\nOnly continue if you verified the address and expected this change. Replace the saved host key?`))return;const result=await api('/api/servers/'+s.id+'/host-key','POST',{});toast('Saved host key: '+result.fingerprint);await refreshServer(s.id);}
function field(name) {return $('#server-form').elements.namedItem(name);}
function syncCredentials() {
  const mode=field('mode').value, show=state.formMode!=='edit';$('#password-field').classList.toggle('hidden',mode!=='password');$('#key-field').classList.toggle('hidden',mode!=='key');$('#sudo-field').classList.toggle('hidden',field('user').value==='root');field('password').required=show&&mode==='password';field('key_id').required=show&&mode==='key';
}
function openForm(mode,s=null) {
  state.formMode=mode;state.formServer=s;const form=$('#server-form');form.reset();$('#form-error').classList.add('hidden');
  const title=mode==='edit'?'Edit server':mode==='onboard'?'Set up access':'Add server';$('#form-title').textContent=title;$('#form-submit').textContent=mode==='edit'?'Save changes':mode==='onboard'?'Set up root + SSH key':'Add and configure SSH';
  for(const key of ['name','alias','host','port','provider','provider_url','lease_end','short_note']){const input=field(key);input.value=s?.[key]??(key==='port'?22:'');input.disabled=mode==='onboard';}
  for(const key of ['cost_amount','cost_currency','cost_period']){const input=field(key);input.value=s?.[key]??({cost_amount:'',cost_currency:expenseState.data?.currency||'USD',cost_period:'monthly'}[key]);input.disabled=mode==='onboard';}
  $('#cost-form-hint').textContent=(s?.price?'Saved price: '+s.price+'. ':'')+(s?.cost_review?'Confirm the amount and billing period below.':'');$('#cost-form-hint').classList.toggle('hidden',!s?.price&&!s?.cost_review);
  field('cost_confirmed').checked=false;field('cost_confirmed').disabled=mode==='onboard';$('#cost-review-field').classList.toggle('hidden',!s?.cost_review);
  field('notes').value='';$('#new-notes-field').classList.toggle('hidden',mode!=='add');$('#credentials-section').classList.toggle('hidden',mode==='edit');
  $('#lease-form-hint').textContent=s?.lease_hint?'In imported records: '+s.lease_hint.split('-').reverse().join('.')+' (year missing). Confirm the full date.':'Full date from your hosting account';
  field('user').value=s?.ssh_user||'root';field('mode').value=mode==='onboard'&&s?.state==='ready'?'managed':'password';
  const keys=field('key_id');keys.replaceChildren();for(const k of state.keys){const option=el('option','',k.name+(k.archived?' (archived)':''));option.value=k.id;keys.append(option);}
  syncCredentials();$('#server-modal').showModal();
}
async function submitForm(event) {
  event.preventDefault();const mode=state.formMode,s=state.formServer;$('#form-error').classList.add('hidden');$('#form-submit').disabled=true;
  try {
    let result;
    if(mode==='edit') {const values={};for(const name of ['name','alias','host','provider','provider_url','short_note'])values[name]=field(name).value;values.port=Number(field('port').value);values.lease_end=field('lease_end').value||null;Object.assign(values,serverFormCost(s));result=await api('/api/servers/'+s.id,'PATCH',values);}
    else {
      const credentials={mode:field('mode').value,user:field('user').value,only_managed:field('only_managed').checked};
      if(credentials.mode==='password')credentials.password=field('password').value;if(credentials.mode==='key')credentials.key_id=field('key_id').value;if(field('sudo_password').value)credentials.sudo_password=field('sudo_password').value;
      try {if(mode==='onboard')result=await api('/api/servers/'+s.id+'/onboard','POST',{credentials});else {const values={};for(const name of ['name','alias','host','provider','provider_url','notes','short_note'])values[name]=field(name).value;values.port=Number(field('port').value);values.lease_end=field('lease_end').value||null;Object.assign(values,serverFormCost());result=await api('/api/servers','POST',{server:values,credentials});}}
      finally {delete credentials.password;delete credentials.sudo_password;field('password').value='';field('sudo_password').value='';}
    }
    $('#server-modal').close();await reload();
    if(result.job){state.jobId=result.job.id;renderJob(true);$('#job-modal').showModal();schedulePoll();}
    else {toast('Server saved.');if(state.selected===s.id){state.detail=result.server;renderDrawerHeader();if(state.tab!=='notes')renderDrawerContent();}}
  } catch(error){$('#form-error').textContent=error.message;$('#form-error').classList.remove('hidden');}
  finally {$('#form-submit').disabled=false;}
}
function serverFormCost(server=null){const amount=field('cost_amount').value;return {cost_amount:amount===''?null:amount,cost_currency:field('cost_currency').value,cost_period:field('cost_period').value,cost_review:amount!==''&&!!server?.cost_review&&!field('cost_confirmed').checked};}
function renderJob(force=false) {
  if(!state.jobId||document.hidden||(!force&&!$('#job-modal').open))return;const job=state.jobs.find(j=>j.id===state.jobId);if(!job)return;
  const signature=JSON.stringify(job);if(state.jobSignature===signature)return;state.jobSignature=signature;
  const root=$('#job-content');root.replaceChildren();const s=state.servers.find(s=>s.id===job.server_id);root.append(el('p','detail-caption',s?`${s.name} · ${s.host}`:''));
  const steps=['Connect to VPS','Back up and install SSH key','Verify separate root login','Disable SSH password login','Verify access after changes','Done · root + SSH key'];
  steps.forEach((text,index)=>{const done=job.state==='done'||job.step>index+1;const active=job.step===index+1&&job.state!=='done';root.append(append(el('div','job-step '+(done?'done':active?'active':'')),el('b','',done?'✓':index+1),text));});
  root.append(el('p',job.state==='error'?'form-error':'notice neutral',job.message));
  if(job.state==='error'&&s)root.append(button('Retry with current credentials','button secondary',()=>{$('#job-modal').close();openForm('onboard',s);}));
}
async function init() {
  desktopState.deferRender=true;
  const desktopReady=loadDesktop('pc');desktopReady.catch(()=>{});
  const result=await api('/api/bootstrap');state.csrf=result.csrf;state.servers=result.servers;state.jobs=result.jobs;state.keys=result.key_options;state.rentals=result.rentals;acceptExpenses(result.expenses);$('#key-fingerprint').textContent=result.key.fingerprint;$('#key-fingerprint').title=result.key.fingerprint;
  $('#refresh-all').addEventListener('click',safeAction(refreshAll));$('#add-server').addEventListener('click',()=>openForm('add'));
  $('#check-all')?.addEventListener('click',safeAction(checkAll));
  $('#open-trash span').replaceChildren(icon('trash'));$('#open-trash').addEventListener('click',safeAction(()=>changeView('trash')));$('#back-servers').addEventListener('click',safeAction(()=>changeView('servers')));
  $('#add-server').replaceChildren(icon('plus'),el('span','','Add server'));
  for(const [view,name] of [['servers','server'],['expenses','wallet'],['tasks','calendar'],['diary','note'],['events','journal'],['pc','terminal'],['burp','activity'],['listeners','plug']]){
    const nav=$(`.nav-button[data-view="${view}"]`),label={servers:'Servers',expenses:'Expenses',tasks:'Tasks',diary:'Diary',events:'Activity',pc:'PC updates',burp:'Burp',listeners:'Listening ports'}[view];
    nav.querySelector('span').replaceChildren(icon(name));nav.setAttribute('aria-label',label);nav.title=label;
  }
  $('.search>span').replaceChildren(icon('search'));$('.key-card strong').replaceChildren(icon('key'),el('span','','managed'));
  $$('.stats article').forEach((card,index)=>card.append(append(el('div','stat-icon'),icon(['server','activity','alert','calendar'][index]))));
  $$('.nav-button').forEach(b=>b.addEventListener('click',safeAction(()=>changeView(b.dataset.view))));
  $$('[data-filter]').forEach(b=>b.addEventListener('click',()=>{state.filter=b.dataset.filter;$$('[data-filter]').forEach(item=>item.classList.toggle('selected',item===b));renderServers();}));
  $('#search').addEventListener('input',event=>{state.search=event.target.value.trim().toLowerCase();renderServers();});
  $('#drawer-backdrop').addEventListener('click',closeDrawer);document.addEventListener('keydown',event=>{if(event.key==='Escape'&&!$('dialog[open]'))closeDrawer();});
  $$('[data-close]').forEach(b=>b.addEventListener('click',()=>$('#'+b.dataset.close).close()));
  initExpenses();
  initTasks();
  initDiary();
  $('#server-modal').addEventListener('close',()=>{field('password').value='';field('sudo_password').value='';if(!$('#server-modal').open)state.formServer=null;});
  $('#job-modal').addEventListener('close',()=>{state.jobSignature='';$('#job-content').replaceChildren();});
  $('#delete-form').addEventListener('submit',submitDelete);$('#delete-alias').addEventListener('input',syncDeleteConfirm);
  $('#delete-modal').addEventListener('cancel',event=>{if(state.deleteBusy)event.preventDefault();});
  $('#delete-modal').addEventListener('close',()=>{if($('#delete-modal').open)return;state.deletion=null;$('#delete-alias').value='';$('#delete-identity').textContent='';$('#delete-description').textContent='';$('#delete-expected-alias').textContent='';$('#delete-error').textContent='';});
  $('#server-form').addEventListener('submit',submitForm);field('mode').addEventListener('change',syncCredentials);field('user').addEventListener('input',syncCredentials);
  $('#quick-note-form').addEventListener('submit',saveQuickNote);$('#quick-note-input').addEventListener('input',updateQuickNoteCount);
  $('#desktop-reminders').addEventListener('change',setDesktopReminders);$('#notification-preview').addEventListener('click',previewNotification);
  document.addEventListener('visibilitychange',handleVisibility);
  field('provider').addEventListener('change',()=>{if(field('provider_url').value)return;const q=field('provider').value.toLowerCase().trim();const known=state.servers.find(s=>s.provider.toLowerCase()===q&&s.provider_url);if(known)field('provider_url').value=known.provider_url;});
  window.addEventListener('beforeunload',event=>{if(diaryDraftsPending()||[...state.notes.values()].some(n=>n.dirty||n.saving)){flushDiary();event.preventDefault();event.returnValue='';}});
  document.body.dataset.view='pc';renderServers();schedulePoll();scheduleRentalPoll();loadCountries();
  try {await desktopReady;} finally {desktopState.deferRender=false;renderDesktop();}
}
function handleVisibility() {
  if(document.hidden){clearTimeout(state.pollTimer);state.pollTimer=null;clearTimeout(state.rentalTimer);state.rentalTimer=null;clearTimeout(desktopState.timer);desktopState.timer=null;flushDiary();return;}
  pollRentals();if(hasPendingJobs())poll();else renderJob();
  if(state.view==='expenses')renderExpenses();
  if(state.view==='tasks')loadTasks();
  if(state.view==='diary')loadDiary();
  if(['pc','burp'].includes(state.view)||Object.values(desktopState.data?.jobs||{}).some(j=>j.state==='running'))pollDesktop().catch(()=>{});
}
init().catch(error=>{toast('Could not open panel: '+error.message,true);$('#snapshot-caption').textContent='Check local-desk.service and reload this page.';});

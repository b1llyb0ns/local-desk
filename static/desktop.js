'use strict';
const desktopState={data:null,timer:null,signature:'',search:'',manager:'apt',loads:new Map(),statusPromise:null,loadedRevisions:{},loadedAt:{},contentSignatures:{},revisions:{},sequence:0,statusSequence:0,controls:[],jobSlots:new Map(),deferRender:false};
const listenerState={data:null,loading:false,error:'',query:'',protocol:'all',scope:'all'};

async function loadListeners() {
  if(listenerState.loading){renderListeners();return;}
  listenerState.loading=true;listenerState.error='';renderListeners();
  try{listenerState.data=await api('/api/local-listeners');}
  catch(error){listenerState.error=error.message;}
  finally{listenerState.loading=false;renderListeners();}
}
function listenerEndpoint(row) {return (row.address.includes(':')?'['+row.address+']':row.address)+':'+row.port;}
function publicFacingTCP(row){return row.protocol==='tcp'&&['wildcard','public-address'].includes(row.exposure_class);}
function releaseDesktopContent(){desktopState.controls=[];desktopState.jobSlots.clear();desktopState.signature='';$('#desktop-section').replaceChildren();}
function renderListeners() {
  if(state.view!=='listeners')return;
  desktopState.signature='';
  const root=$('#desktop-section'),data=listenerState.data;
  const rows=[...(data?.listeners||[])].sort((a,b)=>Number(publicFacingTCP(b))-Number(publicFacingTCP(a))||a.port-b.port||a.protocol.localeCompare(b.protocol));
  const focused=document.activeElement?.id==='listener-search',selection=focused?[document.activeElement.selectionStart,document.activeElement.selectionEnd]:null;
  releaseDesktopContent();
  const refresh=button(listenerState.loading?'Refreshing…':'Refresh','button primary',()=>loadListeners());refresh.id='listener-refresh';refresh.prepend(icon('refresh',listenerState.loading?'spin':''));refresh.disabled=listenerState.loading;
  const copy=button('Copy command','button secondary',safeAction(async()=>{await copyText('ss -lntup');toast('Command copied.');}));copy.id='listener-copy-command';copy.prepend(icon('copy'));
  $('#local-header-actions').replaceChildren(append(el('div','update-actions'),copy,refresh));
  if(listenerState.error)root.append(el('p','form-error',listenerState.error+(data?' Showing the previous snapshot.':'')));
  if(!data){root.append(el('p','empty',listenerState.loading?'Reading local sockets…':'No socket snapshot. Try Refresh.'));return;}
  if(data.error)root.append(el('p','form-error',data.error));
  const complete=data.state==='ok';
  root.append(el('p','listener-summary update-caption',(data.stale?'Previous snapshot · ':'Snapshot · ')+dateTime(data.checked_at)+' · Refresh on demand'));
  const panel=el('section','update-panel listener-panel'),heading=el('div','update-panel-heading');
  const scopes=el('div','segmented listener-scopes');scopes.setAttribute('aria-label','Bind scope filter');
  for(const [value,label] of [['all','All sockets'],['external','External'],['local','Localhost']]){
    const filter=button(label,value===listenerState.scope?'selected':'',()=>{listenerState.scope=value;for(const item of scopes.children){const selected=item.dataset.scope===value;item.classList.toggle('selected',selected);item.setAttribute('aria-pressed',String(selected));}renderRows();});
    filter.dataset.scope=value;filter.setAttribute('aria-pressed',String(value===listenerState.scope));scopes.append(filter);
  }
  const filters=el('div','segmented listener-filters');filters.setAttribute('aria-label','Socket protocol');
  for(const [value,label] of [['all','All'],['tcp','TCP'],['udp','UDP']]){
    const filter=button(label,value===listenerState.protocol?'selected':'',()=>{
      listenerState.protocol=value;
      for(const item of filters.children){const selected=item.dataset.protocol===value;item.classList.toggle('selected',selected);item.setAttribute('aria-pressed',String(selected));}
      renderRows();
    });filter.dataset.protocol=value;filter.setAttribute('aria-pressed',String(value===listenerState.protocol));filters.append(filter);
  }
  const search=el('input','package-search');search.id='listener-search';search.type='search';search.placeholder='Find port or process';search.setAttribute('aria-label','Find port or process');search.value=listenerState.query;
  append(heading,scopes,filters,search);panel.append(heading);
  const count=el('p','update-caption');count.id='listener-count';count.setAttribute('aria-live','polite');panel.append(count);
  const listing=el('div');panel.append(listing);root.append(panel);
  function renderRows(){
    const query=listenerState.query.trim().toLowerCase();
    const filtered=rows.filter(row=>(listenerState.scope==='all'||(listenerState.scope==='local')===(row.exposure==='loopback'))&&(listenerState.protocol==='all'||row.protocol===listenerState.protocol)&&[row.protocol,row.address,row.port,...row.processes.flatMap(p=>[p.name,p.pid])].join(' ').toLowerCase().includes(query));
    count.textContent=filtered.length+' of '+rows.length+' sockets';listing.replaceChildren();
    if(!filtered.length){listing.append(el('p','empty',rows.length?'No matching sockets.':complete?'No listening TCP or bound UDP sockets found.':'No sockets could be read.'));return;}
    const result=table(['Process','PID','Protocol','Address','Port','Bind scope',''],filtered.map(row=>{
      const names=el('div','listener-process'),ids=el('div','process-pids');
      if(row.processes.length)for(const process of row.processes){names.append(el('div','',process.name));ids.append(el('div','',process.pid));}
      else{names.append(el('span','listener-empty','Not visible'));ids.textContent='—';}
      const labels={'local-only':'Local only','wildcard':'All interfaces','private-LAN':'Private network','public-address':'Public address','special-use':'Special-use address'};
      const fallback={loopback:'Local only',all_interfaces:'All interfaces',specific_interface:'Specific interface'};
      const highlighted=publicFacingTCP(row),scope=el('div');const badge=el('span','listener-scope '+row.exposure+' listener-badge '+(highlighted?'tcp-exposure '+(row.exposure_class==='public-address'?'public':'wildcard'):'neutral'),labels[row.exposure_class]||fallback[row.exposure]);scope.append(badge);
      if(row.exposure_reason)badge.title=row.exposure_reason;
      const address=listenerEndpoint(row);
      const copyAddress=button('','icon-button listener-copy-address',safeAction(async()=>{await copyText(address);toast('Address copied.');}),'Copy '+address);copyAddress.append(icon('copy'));
      return [names,ids,el('code','',row.protocol.toUpperCase()),el('code','listener-address',row.address),el('code','',row.port),scope,copyAddress];
    }));result.querySelector('table').classList.add('listener-table');[...result.querySelectorAll('tbody tr')].forEach((item,index)=>{const row=filtered[index];item.classList.toggle('tcp-public-row',publicFacingTCP(row)&&row.exposure_class==='public-address');item.classList.toggle('tcp-wildcard-row',publicFacingTCP(row)&&row.exposure_class==='wildcard');});listing.append(result);
  }
  search.addEventListener('input',()=>{listenerState.query=search.value;renderRows();});renderRows();
  root.append(el('p','update-caption','Process names and PIDs may be hidden by system permissions, even when another owner is shown. No root access is requested.'));
  root.append(append(el('p','listener-legend update-caption'),el('span','tcp-exposure-key','Highlighted TCP'),': public address or all interfaces. Internet reachability is not confirmed; firewall and NAT are not checked.'));
  if(focused){search.focus({preventScroll:true});search.setSelectionRange(...selection);}
}

function mergeDesktop(result,sequence) {
  const data=desktopState.data||(desktopState.data={jobs:{}});
  if(sequence>=desktopState.statusSequence){
    const previous=data.jobs||{};desktopState.statusSequence=sequence;
    if(result.jobs){data.jobs=result.jobs;for(const [kind,job] of Object.entries(result.jobs))if(previous[kind]?.state==='running'&&['done','error'].includes(job.state)&&state.view!==kind&&!document.hidden)toast(job.message,job.state==='error');}
    if('pc_error' in result)data.pc_error=result.pc_error;
    if('terminal_available' in result)data.terminal_available=result.terminal_available;
    Object.assign(desktopState.revisions,result.revisions||{});
  }
  for(const kind of ['pc','burp'])if(result[kind]){
    data[kind]=result[kind];desktopState.loadedRevisions[kind]=result.revisions?.[kind];desktopState.loadedAt[kind]=Date.now();
    desktopState.contentSignatures[kind]=kind==='pc'&&result.revisions?.pc!==undefined?result.revisions.pc:JSON.stringify(result[kind]);
    if(kind==='pc'){const count=result.pc.count||0;$('#pc-update-count').textContent=count;$('#pc-update-count').classList.toggle('hidden',!count);}
    else{$('#burp-update-count').textContent='1';$('#burp-update-count').classList.toggle('hidden',!result.burp.update_available);}
  }
}
function loadDesktop(view=state.view) {
  if(!['pc','burp'].includes(view)||document.hidden)return Promise.resolve();
  if(desktopState.loads.has(view))return desktopState.loads.get(view);
  const sequence=++desktopState.sequence;
  const pending=(async()=>{const result=await api('/api/desktop-updates?view='+view);mergeDesktop(result,sequence);renderDesktop();})().finally(()=>{desktopState.loads.delete(view);scheduleDesktopPoll();});
  desktopState.loads.set(view,pending);return pending;
}
function pollDesktop() {
  clearTimeout(desktopState.timer);desktopState.timer=null;
  if(document.hidden||!needsDesktopPoll())return Promise.resolve();if(desktopState.statusPromise)return desktopState.statusPromise;
  desktopState.statusPromise=(async()=>{
    const pending=[...desktopState.loads.values()];if(pending.length)await Promise.allSettled(pending);
    if(document.hidden||!needsDesktopPoll())return;
    const sequence=++desktopState.sequence,result=await api('/api/desktop-updates/status');mergeDesktop(result,sequence);
    if(document.hidden)return;
    const view=state.view;
    if(['pc','burp'].includes(view)&&(!desktopState.data?.[view]||desktopState.loadedRevisions[view]!==desktopState.revisions[view]||(view==='burp'&&Date.now()-desktopState.loadedAt.burp>=60000)))await loadDesktop(view);
    renderDesktop();
  })().finally(()=>{desktopState.statusPromise=null;scheduleDesktopPoll();});return desktopState.statusPromise;
}
function needsDesktopPoll(){return ['pc','burp'].includes(state.view)||Object.values(desktopState.data?.jobs||{}).some(j=>j.state==='running');}
function scheduleDesktopPoll() {
  clearTimeout(desktopState.timer);desktopState.timer=null;
  const active=Object.values(desktopState.data?.jobs||{}).some(j=>j.state==='running');
  if(document.hidden||(!active&&!['pc','burp'].includes(state.view)))return;
  desktopState.timer=setTimeout(()=>pollDesktop().catch(()=>{if(['pc','burp'].includes(state.view))toast('Could not refresh local update status.',true);}),active?1500:60000);
}
async function desktopAction(path,body={}) {
  const result=await api(path,'POST',body);mergeDesktop(result,++desktopState.sequence);
  renderDesktop();scheduleDesktopPoll();
}
function desktopButton(item,kind,disabled=false){item.desktopJobKind=kind;item.desktopDisabled=disabled;item.disabled=disabled||desktopState.data?.jobs?.[kind]?.state==='running';desktopState.controls.push(item);return item;}
function updateJob(root,kind) {
  const slot=el('div');desktopState.jobSlots.set(kind,slot);root.append(slot);renderDesktopJob(slot,kind);
}
function renderDesktopJob(slot,kind) {
  const job=desktopState.data.jobs[kind],signature=JSON.stringify(job||null);if(slot.dataset.signature===signature)return;slot.dataset.signature=signature;
  if(!job){slot.replaceChildren();return;}
  const box=el('div','update-job '+job.state);box.dataset.kind=kind;
  append(box,icon(job.state==='running'?'refresh':job.state==='error'?'alert':'check',job.state==='running'?'spin':''),el('span','',job.message));
  if(job.state==='running'&&job.percent!=null){const bar=el('progress');bar.max=100;bar.value=job.percent;bar.setAttribute('aria-label','Download progress');box.append(bar);}
  slot.replaceChildren(box);
}
function updateDesktopJobs(){
  for(const [kind,slot] of desktopState.jobSlots)renderDesktopJob(slot,kind);
  for(const item of desktopState.controls){const disabled=item.desktopDisabled||desktopState.data.jobs[item.desktopJobKind]?.state==='running';if(item.disabled!==!!disabled)item.disabled=!!disabled;}
}
function renderDesktop() {
  if(document.hidden||desktopState.deferRender||!['pc','burp'].includes(state.view))return;
  const root=$('#desktop-section'),data=desktopState.data;
  if(!data?.[state.view]){releaseDesktopContent();root.append(el('p','empty','Loading…'));return;}
  const signature=JSON.stringify([state.view,desktopState.contentSignatures[state.view],data.terminal_available,state.view==='pc'?data.pc_error:null]);
  if(desktopState.signature===signature){updateDesktopJobs();return;}
  const focused=document.activeElement,searchFocused=focused?.matches('.package-search'),selection=searchFocused?[focused.selectionStart,focused.selectionEnd]:null;
  releaseDesktopContent();desktopState.signature=signature;if(state.view==='pc')renderPC(root,data);else renderBurp(root,data);
  if(searchFocused){const search=$('.package-search',root);if(search){search.focus({preventScroll:true});search.setSelectionRange(...selection);}}
}
function summaryCard(title,value,caption='') {return append(el('article','update-stat'),el('span','',title),el('strong','',value),caption?el('small','',caption):null);}
const toolManagers=new Set(['go','pdtm','go-tools','pipx']);
function overviewItem(title,value,caption='',symbol='activity'){return append(el('div','overview-item'),append(el('div','overview-icon'),icon(symbol)),el('span','',title),el('strong','',value),el('small','',caption));}
function renderPC(root,data) {
  const pc=data.pc;
  const managers=pc.managers?.length?pc.managers:[{...pc,id:'apt',name:'APT',actions:['check','refresh-lists','upgrade']}];
  const apt=managers.find(m=>m.id==='apt')||{},selected=managers.find(m=>m.id===desktopState.manager)||managers[0];
  desktopState.manager=selected.id;
  const check=button('Refresh inventory','button secondary',safeAction(()=>desktopAction('/api/pc/check')));check.id='pc-inventory-refresh';check.prepend(icon('refresh'));desktopButton(check,'pc');
  $('#local-header-actions').replaceChildren(check);
  const toolCount=managers.filter(m=>toolManagers.has(m.id)).reduce((count,m)=>count+(m.installed_count||0),0);
  root.append(append(el('div','update-overview'),
    overviewItem('APT updates',apt.count??'—',apt.security_count!=null?apt.security_count+' from security repositories':'Local package cache','archive'),
    overviewItem('Installed tools',toolCount,managers.length+' package and tool sources','terminal'),
    overviewItem('Last apt update',apt.last_refresh_at?age(apt.last_refresh_at):'Unknown',apt.last_refresh_at?dateTime(apt.last_refresh_at):'No successful refresh time recorded','refresh'),
    overviewItem('Last APT upgrade',apt.last_upgrade_at?age(apt.last_upgrade_at):'Unknown',apt.last_upgrade_at?dateTime(apt.last_upgrade_at):'No completed upgrade recorded','calendar')));
  if(pc.reboot_requested)root.append(el('p','notice','The system has requested a reboot. Save your work first.'));
  if(data.pc_error)root.append(el('p','form-error',data.pc_error));updateJob(root,'pc');
  const workspace=el('div','pc-workspace'),side=el('aside','pc-side');side.setAttribute('aria-label','Package sources and actions');
  side.append(append(el('div','sources-heading'),el('h2','','Sources'),el('span','package-tag',managers.length)));
  const choose=(id,selector)=>{desktopState.manager=id;desktopState.search='';desktopState.signature='';renderDesktop();$(selector)?.focus({preventScroll:true});};
  const grid=el('div','manager-grid');
  for(const manager of managers){
    const isTool=toolManagers.has(manager.id),card=el('article','manager-card'+(selected.id===manager.id?' is-selected':''));card.dataset.manager=manager.id;
    const status=manager.state==='partial'?'Partial check':manager.error?'Check failed':manager.count==null?'Not checked':manager.count+' pending';
    const select=button(manager.name,'manager-name',()=>choose(manager.id,'.manager-card[data-manager="'+manager.id+'"] .manager-name'));select.title='Show '+manager.name;select.setAttribute('aria-pressed',String(selected.id===manager.id));select.setAttribute('aria-controls','package-panel');
    card.append(append(el('div','manager-heading'),select,el('span','package-tag',status)));
    const meta=el('div','manager-meta');
    const line=(label,value)=>meta.append(append(el('div'),el('span','',label),el('strong','',value)));
    line(manager.id==='apt'?'Cache read':'Versions checked',manager.updates_checked_at?dateTime(manager.updates_checked_at):manager.id==='apt'&&manager.checked_at?dateTime(manager.checked_at):'Not checked');
    if(manager.id==='apt'){
      line('apt update',manager.last_refresh_at?dateTime(manager.last_refresh_at):'Not recorded');
      if(manager.last_refresh_source)meta.title=manager.last_refresh_source;
    }else{
      if(manager.installed_count!=null)line('Installed',String(manager.installed_count));
      if(manager.id==='snap'){
        meta.append(el('small','','snapd auto-refresh · last '+dateTime(manager.last_auto_refresh_at)+' · next '+dateTime(manager.next_auto_refresh_at)));
      }
    }
    if(manager.error)meta.append(el('small','tool-status update',manager.error));card.append(meta);
    const actions=el('div','manager-actions');
    const path=isTool?'/api/tool-updates/'+manager.id+'/':'/api/desktop-updates/manager/'+manager.id+'/';
    for(const operation of manager.actions||[]){
      const label=operation==='check'?(manager.id==='apt'?'Read cache':'Check updates'):operation==='refresh-lists'?'apt update':manager.id==='apt'?'Update APT packages':manager.id==='snap'?'Update snaps':'Update Flatpaks';
      const action=button(label,'button '+(operation==='upgrade'?'primary':'secondary'),safeAction(async()=>{
        if(operation!=='check'){
          const message=manager.id==='apt'?(operation==='refresh-lists'?'Refresh APT package lists on this PC?':'Upgrade APT packages on this PC? Services may restart; a reboot may be required.'):'Update '+manager.name+' apps on this PC? Running apps may be affected.';
          if(!confirm(message+'\n\nA terminal will open for review and confirmation. No VPS will be changed.'))return;
        }
        await desktopAction(path+operation);
      }));action.dataset.action=operation;desktopButton(action,'pc',operation!=='check'&&!data.terminal_available);actions.append(action);
    }
    card.append(actions);grid.append(card);
  }
  side.append(grid,el('p','update-caption','Inventory read '+dateTime(pc.checked_at)));
  const panel=el('section','update-panel package-panel');panel.id='package-panel';const heading=el('div','update-panel-heading'),tabs=el('div','segmented manager-tabs');
  for(const manager of managers){
    const tab=button(manager.name,selected.id===manager.id?'selected':'',()=>choose(manager.id,'.manager-tabs [data-manager="'+manager.id+'"]'));tab.dataset.manager=manager.id;tab.setAttribute('aria-pressed',String(selected.id===manager.id));tabs.append(tab);
  }
  const search=el('input','package-search');search.type='search';search.placeholder='Find package';search.setAttribute('aria-label','Find package');search.value=desktopState.search;
  append(heading,el('h2','','Package updates'),search);panel.append(heading,tabs);
  const listing=el('div','package-listing');panel.append(listing);workspace.append(panel,side);root.append(workspace);
  const render=()=>{
    const rows=(selected.packages||[]).filter(p=>p.name.toLowerCase().includes(desktopState.search));listing.replaceChildren();desktopState.controls=desktopState.controls.filter(item=>item.isConnected);
    if(!rows.length){
      listing.append(el('div','empty',desktopState.search?'No matching packages.':toolManagers.has(selected.id)?'No installed tools found.':selected.count==null?'Updates have not been checked. Use Check updates above.':'No pending updates in the last check.'));return;
    }
    if(toolManagers.has(selected.id)){
      const result=table(['Tool / source','Installed','Available','Status',''],rows.map(p=>{
        const info=append(el('div'),el('strong','',p.name),el('span','tool-path',p.path||''),el('small','',p.source||''));
        const ready=p.updatable&&!!p.candidate,status=el('div','tool-status'+(ready?' update':''));
        status.append(el('span','',p.check_error?'Check failed':!p.updatable?'Manual':ready?p.version_unknown?'Confirm stable install':'Update available':p.latest||p.updates_checked_at?'Current':'Not checked'));
        if(p.reason)status.append(el('small','',p.reason));if(p.check_error)status.append(el('small','',p.check_error));
        if(p.last_updated_at)status.append(el('small','','Updated '+dateTime(p.last_updated_at)));
        const actions=el('div','tool-actions');
        const install=button(p.version_unknown?'Install stable':'Update','button secondary tool-update',safeAction(async()=>{
          const warning=p.version_unknown?'The installed version is unknown. Installing stable may replace a newer or custom build.\n\n':'';
          if(!confirm(warning+'Install '+p.name+' '+p.candidate+' on this PC?\n\n'+(p.path||'')+'\nA terminal will open for confirmation. The previous installation will be backed up.'))return;
          await desktopAction('/api/tool-updates/'+selected.id+'/install',{item:p.id,version:p.candidate,allow_unknown_version:!!p.version_unknown});
        }));install.dataset.toolId=p.id;desktopButton(install,'pc',!ready||!data.terminal_available);install.title=ready?'Install this version only':p.reason||'Check versions first';actions.append(install);
        return[info,el('code','',p.installed||'Unknown'),el('code','candidate-version',p.candidate||p.latest||'—'),status,actions];
      }));result.querySelector('table').classList.add('tool-table');listing.append(result);return;
    }
    listing.append(table(['Package','Installed','Available','Type'],rows.map(p=>[el('code','',p.name),el('code','',p.installed||'—'),el('code','candidate-version',p.candidate||'—'),el('span','package-tag'+(p.security?' security':''),p.held?'Held':p.trusted===false?'Untrusted source':p.security?'Security':'Update')])));
  };search.addEventListener('input',()=>{desktopState.search=search.value.trim().toLowerCase();render();});render();
  root.append(el('p','update-caption','Inventory is local. Version checks contact official repositories. Tools update individually; custom and pinned installs stay manual.'));
}
function renderBurp(root,data) {
  const burp=data.burp,latest=burp.latest;
  const toolbar=el('div','update-toolbar');
  const check=button('Check version','button secondary',safeAction(()=>desktopAction('/api/burp/check')));check.id='burp-check-version';check.prepend(icon('refresh'));desktopButton(check,'burp');
  const install=button(burp.update_available?'Download and update':'Install official release','button primary',safeAction(async()=>{
    if(!confirm('Update Burp JAR to '+latest.version+'?\n\nThe JAR will be downloaded from PortSwigger and SHA-256 verified. The existing desktop launch command and the burp shell alias will be preserved; only their JAR path will change. The old JAR and settings will be kept. Save projects and close Burp first.'))return;
    await desktopAction('/api/burp/install',{version:latest.version,preserve_launch:true});
  }));install.prepend(icon('plus'));desktopButton(install,'burp',!latest.version||burp.running.length>0||burp.installed_newer);
  append(toolbar,el('p','section-label','Burp Suite · Stable'),append(el('div','update-actions'),check,install));root.append(toolbar);
  root.append(append(el('div','update-stats'),summaryCard('Installed',burp.version||'Not detected'),summaryCard('Latest stable',latest.version||'Not checked',burp.update_available?'Update available':latest.version?'Current stable release':''),summaryCard('Last checked',latest.checked_at?age(latest.checked_at):'Never',latest.checked_at?dateTime(latest.checked_at):'')));
  updateJob(root,'burp');if(burp.error)root.append(el('p','form-error',burp.error));
  if(burp.custom_launcher)root.append(el('p','notice neutral','The current launch command has custom Java options. An update preserves them and changes only the JAR path.'));
  if(burp.running.length)root.append(el('p','notice','Burp is running. Save your projects and close it before installing.'));
  if(burp.installed_newer)root.append(el('p','notice neutral','The installed version is newer than stable. Automatic downgrades are disabled.'));
  const settings=el('section','update-panel');settings.append(el('h2','','Installation details'));
  settings.append(el('p','update-caption','Release checks run only when you click Check version. Downloads and installation require confirmation.'));
  settings.append(kv([['Launch shortcut',el('code','path-value',burp.launcher)],['Installed JAR',el('code','path-value',burp.jar||'Not detected')],['Source',link('PortSwigger downloads ↗','https://portswigger.net/burp/downloads')]]));
  if(latest.release_url)settings.append(link('Release notes ↗',latest.release_url,'button secondary'));
  if(latest.sha256){const details=el('details','checksum-details');details.append(el('summary','','SHA-256'),el('code','path-value',latest.sha256));settings.append(details);}
  root.append(settings);
}

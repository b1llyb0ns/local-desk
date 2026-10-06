// Headless Chromium with an isolated temporary profile; no user browser data.
import {spawn} from 'node:child_process';
import {mkdtemp,readFile,writeFile,rm,mkdir} from 'node:fs/promises';
import {join,dirname,resolve} from 'node:path';
import {fileURLToPath} from 'node:url';
import assert from 'node:assert/strict';
import {runInNewContext} from 'node:vm';

// Exercise scheduled callbacks with a fake clock: no browser, network or 60-second wait.
async function checkListenerSource(){
  const root=resolve(dirname(fileURLToPath(import.meta.url)),'../static');
  const desktop=await readFile(join(root,'desktop.js'),'utf8'),app=await readFile(join(root,'app.js'),'utf8'),index=await readFile(join(root,'index.html'),'utf8');
  assert.doesNotMatch(index,/data-view=["']rent["']/,'Rentals has no separate navigation entry');
  assert.match(index,/id=["']rental-alerts["']/,'Rental alerts remain available in the server workspace');
  assert.match(index,/name=["']lease_end["']/,'Server editing retains the rental-expiry field');
  assert.doesNotMatch(desktop+'\n'+app,/\/api\/local-port-watch|\/api\/local-listeners\/cached|pollPortCache|updatePortBadge|port-alert|port-watch-enabled|Mark reviewed|Monitor external listeners/i,'Port alerts and cached/background port requests are removed');
  const requests=[],timers=new Map();let timerId=0,clockMs=Date.UTC(2026,8,5,12);
  class FakeDate extends Date {constructor(...args){super(...(args.length?args:[clockMs]));}static now(){return clockMs;}}
  const revisions={pc:'fixture:1',burp:'fixture:1'},jobs={};
  const context={state:{view:'pc'},document:{hidden:false},Date:FakeDate,$:()=>({textContent:'',classList:{toggle(){}}}),toast(){},
    api:async path=>{
      requests.push(path);
      const common={jobs:structuredClone(jobs),revisions:{...revisions},pc_error:''};
      if(path==='/api/desktop-updates/status')return common;
      if(path==='/api/desktop-updates?view=pc')return {...common,pc:{count:0},terminal_available:true};
      if(path==='/api/desktop-updates?view=burp')return {...common,burp:{update_available:false},terminal_available:true};
      assert.fail('Unexpected background request: '+path);
    },
    setTimeout:callback=>{const id=++timerId;timers.set(id,callback);return id;},clearTimeout:id=>timers.delete(id)};
  runInNewContext(desktop+'\nrenderDesktop=()=>{};renderListeners=()=>{};updateDesktopJobs=()=>{};',context);
  const tick=async()=>{
    const scheduled=[...timers.entries()];
    for(const [id,callback] of scheduled){timers.delete(id);await callback();}
  };
  await context.loadDesktop('pc');
  assert.deepEqual(requests,['/api/desktop-updates?view=pc'],'PC startup requests only its own inventory');
  requests.length=0;await tick();
  assert.deepEqual(requests,['/api/desktop-updates/status'],'Unchanged inventory uses only lightweight status');
  requests.length=0;jobs.pc={state:'running',message:'Checking package inventory',percent:10};
  await context.pollDesktop();
  assert.deepEqual(requests,['/api/desktop-updates/status'],'Job progress does not reload inventory');
  requests.length=0;revisions.pc='fixture:2';await context.pollDesktop();
  assert.deepEqual(requests,['/api/desktop-updates/status','/api/desktop-updates?view=pc'],'Changed visible revision reloads only PC');
  requests.length=0;revisions.burp='fixture:2';await context.pollDesktop();
  assert.deepEqual(requests,['/api/desktop-updates/status'],'A hidden Burp revision does not load Burp');
  requests.length=0;context.state.view='burp';await context.loadDesktop('burp');
  assert.deepEqual(requests,['/api/desktop-updates?view=burp'],'Burp loads separately on demand');
  requests.length=0;await tick();
  assert.deepEqual(requests,['/api/desktop-updates/status'],'Burp polling also uses lightweight status');
  requests.length=0;revisions.pc='fixture:3';await context.pollDesktop();
  assert.deepEqual(requests,['/api/desktop-updates/status'],'Even a previously loaded hidden inventory stays deferred');
  requests.length=0;clockMs+=60000;await context.pollDesktop();
  assert.deepEqual(requests,['/api/desktop-updates/status','/api/desktop-updates?view=burp'],'Visible Burp periodically refreshes external process/launcher state');
  context.state.view='pc';await context.loadDesktop('pc');requests.length=0;clockMs+=60000;await context.pollDesktop();
  assert.deepEqual(requests,['/api/desktop-updates/status'],'PC idle polling never adopts Burp process-refresh work');
  requests.length=0;context.document.hidden=true;await tick();await context.pollDesktop();
  assert.deepEqual(requests,[],'Hidden tab performs no desktop GET, even during a running job');
  context.document.hidden=false;delete jobs.pc;await context.pollDesktop();
  for(const view of ['servers','listeners']){
    requests.length=0;context.state.view=view;context.scheduleDesktopPoll();await tick();
    assert.deepEqual(requests,[],'Inactive desktop view does not poll: '+view);
  }
}
await checkListenerSource();
if(process.argv.includes('--source-only')){console.log('PASS: port-alert removal, lazy desktop snapshots, revision-aware status and hidden-tab fake-clock checks');process.exit(0);}

const outputDirectory=resolve(dirname(fileURLToPath(import.meta.url)),'../tmp/browser');
await mkdir(outputDirectory,{recursive:true,mode:0o700});
const directory=await mkdtemp(join(outputDirectory,'profile-'));
const chrome=spawn('chromium',['--headless=new','--disable-gpu','--no-first-run','--no-default-browser-check','--disable-background-networking','--remote-debugging-pipe','--user-data-dir='+directory,'about:blank'],{stdio:['ignore','ignore','pipe','pipe','pipe']});
let sequence=0,pending=new Map(),buffer=Buffer.alloc(0),session,errors=[];
chrome.stderr.on('data',()=>{});
chrome.stdio[4].on('data',chunk=>{buffer=Buffer.concat([buffer,chunk]);let index;while((index=buffer.indexOf(0))>=0){const text=buffer.subarray(0,index).toString();buffer=buffer.subarray(index+1);if(!text)continue;const m=JSON.parse(text);if(m.id&&pending.has(m.id)){const {resolve,reject,timer}=pending.get(m.id);pending.delete(m.id);clearTimeout(timer);m.error?reject(new Error(JSON.stringify(m.error))):resolve(m.result);}else if(m.method==='Runtime.exceptionThrown')errors.push(m.params.exceptionDetails.text+': '+(m.params.exceptionDetails.exception?.description||''));else if(m.method==='Log.entryAdded'&&m.params.entry.level==='error')errors.push(m.params.entry.text);}});
function call(method,params={},sessionId=session){return new Promise((resolve,reject)=>{const id=++sequence;const timer=setTimeout(()=>{pending.delete(id);reject(new Error('CDP timeout: '+method));},15000);pending.set(id,{resolve,reject,timer});chrome.stdio[3].write(JSON.stringify({id,method,params,...(sessionId?{sessionId}:{})})+'\0');});}
async function evaluate(expression){const r=await call('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true,userGesture:true});if(r.exceptionDetails)throw new Error(r.exceptionDetails.exception?.description||r.exceptionDetails.text);return r.result.value;}
async function wait(expression){for(let n=0;n<60;n++){try{if(await evaluate(expression))return;}catch(e){if(!/navigated|context|Cannot find/.test(e.message))throw e;}await new Promise(r=>setTimeout(r,100));}throw new Error('UI wait timed out: '+expression);}
async function click(selector){assert.ok(await evaluate(`!!document.querySelector(${JSON.stringify(selector)})`),selector);await evaluate(`document.querySelector(${JSON.stringify(selector)}).click()`);}
async function fill(selector,value){await evaluate(`(()=>{const e=document.querySelector(${JSON.stringify(selector)});e.value=${JSON.stringify(value)};e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));})()`);}
async function screenshot(name){await new Promise(resolve=>setTimeout(resolve,150));const r=await call('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});const path=join(outputDirectory,name);await writeFile(path,Buffer.from(r.data,'base64'));console.log('Screenshot:',path);}
async function toolScreenshot(name){const scroll=await evaluate('window.scrollY');await wait('document.querySelector("#toasts").children.length === 0');await evaluate('document.querySelector(".tool-table").closest(".update-panel").scrollIntoView({block:"start"})');await screenshot(name);await evaluate(`window.scrollTo(0,${scroll})`);}
async function selectManager(manager){await click(`.manager-card[data-manager="${manager}"] .manager-name`);await wait(`document.querySelector('.manager-tabs [data-manager="${manager}"]')?.getAttribute('aria-pressed') === 'true'`);}
async function managerAction(manager,operation){await selectManager(manager);const selector=`.manager-card[data-manager="${manager}"] [data-action="${operation}"]`;assert.ok(await evaluate(`document.querySelector(${JSON.stringify(selector)}).getClientRects().length > 0`),'Selected manager action is visible: '+manager+'/'+operation);await click(selector);}
async function desktopPCLayout(){
  await wait('!!document.querySelector(".pc-workspace .package-panel") && !!document.querySelector(".pc-workspace .pc-side .manager-grid")');
  assert.equal(await evaluate('document.querySelectorAll(".update-overview .overview-item").length'),4);
  assert.ok(await evaluate('[...document.querySelectorAll(".update-overview .overview-item")].every(e=>{const style=getComputedStyle(e),height=e.getBoundingClientRect().height;return height>=(innerWidth<=760?92:96)&&height<=145&&parseFloat(style.borderRadius)===10&&style.backgroundColor==="rgb(21, 21, 21)"&&[style.borderTopWidth,style.borderRightWidth,style.borderBottomWidth,style.borderLeftWidth].every(width=>parseFloat(width)===0);})'),'Summary uses compact graphite cards with 10px corners and no decorative borders');
  assert.ok(await evaluate('[...document.querySelectorAll(".update-overview .overview-item strong")].every(e=>{const style=getComputedStyle(e);return !/mono/i.test(style.fontFamily)&&parseFloat(style.fontSize)>=24&&parseFloat(style.fontSize)<=44;})'),'Summary values use large sans-serif numerals');
  assert.equal(await evaluate('parseFloat(getComputedStyle(document.querySelector("#page-title")).fontSize)'),28,'Dashboard page-title size');
  assert.ok(await evaluate('(()=>{const tab=document.querySelector(".manager-tabs button.selected"),style=getComputedStyle(tab);return tab.getAttribute("aria-pressed")==="true"&&style.backgroundColor==="rgb(41, 41, 41)"&&style.color==="rgb(215, 243, 43)"&&style.borderTopColor==="rgba(0, 0, 0, 0)"&&parseFloat(style.borderRadius)===6;})()'),'Selected package tab uses a neutral pill and lime label');
  const positions=await evaluate('(()=>{const left=document.querySelector(".pc-workspace .package-panel").getBoundingClientRect(),right=document.querySelector(".pc-workspace .pc-side").getBoundingClientRect();return {viewport:innerWidth,left:left.left,leftRight:left.right,leftWidth:left.width,leftTop:left.top,leftBottom:left.bottom,rightLeft:right.left,rightWidth:right.width,rightTop:right.top,rightBottom:right.bottom};})()');
  if(positions.viewport>1100){
    assert.ok(positions.leftRight<=positions.rightLeft&&positions.leftWidth>positions.rightWidth,'Packages occupy the larger left column: '+JSON.stringify(positions));
    assert.ok(Math.abs(positions.leftTop-positions.rightTop)<=4,'PC columns align at the top');
    assert.ok(Math.abs(positions.rightWidth-300)<=1,'Sources occupy a 300px desktop column: '+JSON.stringify(positions));
  }else assert.ok(Math.abs(positions.left-positions.rightLeft)<=4&&positions.rightTop>=positions.leftBottom-4,'Packages precede source controls at 1100px and below: '+JSON.stringify(positions));
}
async function desktopNavigation(){
  const layout=await evaluate(`(()=>{
    const shell=document.querySelector('.app-shell'),header=shell.querySelector(':scope > .sidebar'),main=shell.querySelector(':scope > main'),nav=header.querySelector('nav'),brand=header.querySelector('.brand'),status=header.querySelector('.sidebar-bottom'),buttons=[...nav.querySelectorAll('.nav-button')];
    const rect=e=>{const r=e.getBoundingClientRect();return{left:r.left,right:r.right,top:r.top,bottom:r.bottom,width:r.width,height:r.height};};
    const shellStyle=getComputedStyle(shell),navStyle=getComputedStyle(nav),headerStyle=getComputedStyle(header),before=nav.scrollLeft;
    nav.scrollLeft=nav.scrollWidth;const scrolls=nav.scrollLeft>0;nav.scrollLeft=before;
    const bodyStyle=getComputedStyle(document.body);
    return{width:innerWidth,height:innerHeight,shell:rect(shell),header:rect(header),main:rect(main),nav:rect(nav),brand:rect(brand),status:rect(status),radius:parseFloat(shellStyle.borderRadius),background:shellStyle.backgroundColor,minHeight:parseFloat(shellStyle.minHeight),borders:[shellStyle.borderTopWidth,shellStyle.borderRightWidth,shellStyle.borderBottomWidth,shellStyle.borderLeftWidth].map(parseFloat),
      bodyBackground:bodyStyle.backgroundColor,bodyPadding:[bodyStyle.paddingTop,bodyStyle.paddingRight,bodyStyle.paddingBottom,bodyStyle.paddingLeft].map(parseFloat),headerPosition:headerStyle.position,navOverflow:navStyle.overflowX,navScrollWidth:nav.scrollWidth,navClientWidth:nav.clientWidth,scrolls,
      filters:[document.body,shell,header].map(e=>({filter:getComputedStyle(e).filter,backdrop:getComputedStyle(e).backdropFilter})),
      buttons:buttons.map(button=>({...rect(button),view:button.dataset.view,fontSize:parseFloat(getComputedStyle(button).fontSize),title:button.title,label:[...button.childNodes].filter(node=>node.nodeType===Node.TEXT_NODE).map(node=>node.textContent).join('').trim()}))};
  })()`);
  assert.equal(layout.bodyBackground,'rgb(5, 5, 5)','Page and dashboard share the same near-black background');
  assert.deepEqual(layout.bodyPadding,[0,0,0,0],'The page has no outer frame or padding');
  assert.equal(layout.background,'rgb(5, 5, 5)','Application shell stays near black');
  assert.equal(layout.radius,0,'The application shell has no rounded outer frame');
  assert.deepEqual(layout.borders,[0,0,0,0],'The application shell has no border');
  assert.ok(layout.minHeight>=layout.height-1,'Application background covers the full viewport height');
  assert.ok(layout.shell.width<=1601&&layout.shell.left>=0&&layout.shell.right<=layout.width+1,'Application shell fits the viewport and 1600px cap');
  if(layout.width>1600)assert.ok(Math.abs(layout.shell.width-1600)<=1,'Wide screens use the 1600px application shell');
  assert.ok(Math.abs(layout.header.width-layout.shell.width)<=2&&layout.header.bottom<=layout.main.top+1,'Horizontal navigation sits above the full-width main workspace');
  assert.ok(!['fixed','sticky'].includes(layout.headerPosition),'Top navigation remains in normal page flow');
  assert.equal(layout.buttons.length,8,'Eight main application sections remain in navigation');
  assert.deepEqual(layout.buttons.map(button=>button.view),['pc','listeners','servers','expenses','tasks','diary','events','burp'],'Expenses precedes Activity and Burp in the existing navigation');
  assert.ok(layout.buttons.every(button=>button.fontSize>=11&&button.label.length>0&&button.title.trim().length>0&&button.width>0&&button.height>=40),'Navigation retains visible text labels and usable buttons');
  assert.ok(layout.buttons.every(button=>Math.abs(button.top-layout.buttons[0].top)<=2),'Navigation buttons form one horizontal row');
  assert.ok(layout.brand.height>0&&layout.status.height>0,'Brand and local status remain visible');
  if(layout.width<=1200){
    assert.ok(layout.nav.top>=Math.max(layout.brand.bottom,layout.status.bottom)-1,'Compact topbar places navigation below the brand and status row');
    assert.ok(layout.brand.top<layout.status.bottom&&layout.status.top<layout.brand.bottom,'Brand and local status share the first row');
  }else assert.ok(layout.nav.top<layout.brand.bottom&&layout.brand.top<layout.nav.bottom,'Wide topbar aligns navigation beside the brand');
  if(layout.navScrollWidth>layout.navClientWidth+1)assert.ok(['auto','scroll'].includes(layout.navOverflow)&&layout.scrolls,'Overflowing navigation scrolls within its own strip');
  assert.ok(layout.filters.every(style=>style.filter==='none'&&style.backdrop==='none'),'Dashboard surfaces use no visual filters or backdrop blur');
  assert.ok(await evaluate('document.documentElement.scrollWidth<=innerWidth'),'Navigation creates no page-level horizontal overflow at '+layout.width+'px');
}
async function noPortAlerts(){
  assert.equal(await evaluate('document.querySelectorAll(".port-alerts,.port-alert-count,.port-alert-row,#port-watch-enabled,#port-alert-acknowledge").length'),0,'Port alerts, badges and controls are absent');
  assert.doesNotMatch(await evaluate('document.querySelector("#desktop-section").textContent'),/Mark reviewed|Monitor external listeners|New external listeners|New alerts|Background checks paused|Desktop popups|initial baseline|First seen/i,'No obsolete port-alert text');
  if(await evaluate('!!document.querySelector("#listener-search")'))assert.ok(await evaluate('(()=>{const key=document.querySelector(".tcp-exposure-key");if(!key)return false;const rect=key.getBoundingClientRect();return rect.width>60&&rect.height>0&&rect.height<25&&getComputedStyle(key).whiteSpace==="nowrap";})()'),'TCP legend label is readable, not constrained to the 7px indicator');
}
async function portRequestCount(){return evaluate('performance.getEntriesByType("resource").filter(e=>/^\\/api\\/local-(?:listeners|port-watch)(?:\\/|$)/.test(new URL(e.name).pathname)).length');}
async function checkDesktopOptimizations(){
  assert.equal(await evaluate('document.querySelectorAll("#server-rows tr").length'),0,'Hidden server rows are not built on PC startup');
  assert.equal(await evaluate('performance.getEntriesByType("resource").filter(e=>{const u=new URL(e.name);return u.pathname==="/api/desktop-updates"&&u.searchParams.get("view")==="burp";}).length'),0,'PC startup does not load Burp');
  const rapid=await evaluate(`(async()=>{
    const originalFetch=window.fetch,requests=[];
    let release,started;
    const gate=new Promise(resolve=>{release=resolve;}),firstBurp=new Promise(resolve=>{started=resolve;});
    window.fetch=(input,options)=>{
      const url=new URL(typeof input==='string'?input:input.url,location.href);
      if((options?.method||'GET')==='GET'&&url.pathname==='/api/desktop-updates'){
        const view=url.searchParams.get('view');requests.push(view);
        if(view==='burp'){started();return gate.then(()=>originalFetch.call(window,input,options));}
      }
      return originalFetch.call(window,input,options);
    };
    try{
      const transitions=[changeView('burp'),changeView('pc'),changeView('burp'),changeView('pc'),loadDesktop('burp'),loadDesktop('burp')];
      await firstBurp;release();await Promise.all(transitions);
      return {requests,view:state.view,pc:!!document.querySelector('.package-search'),burp:!!document.querySelector('#burp-check-version'),servers:document.querySelectorAll('#server-rows tr').length};
    }finally{release();window.fetch=originalFetch;}
  })()`);
  assert.equal(rapid.requests.filter(view=>view==='burp').length,1,'Rapid Burp entry shares one in-flight snapshot');
  assert.equal(rapid.requests.filter(view=>view==='pc').length,1,'Rapid PC entry shares one in-flight snapshot');
  assert.deepEqual({...rapid,requests:undefined},{requests:undefined,view:'pc',pc:true,burp:false,servers:0},'A late Burp response cannot replace the visible PC or build hidden servers');
  await fill('.package-search','openssl');
  const progress=await evaluate(`(async()=>{
    const baseline=await api('/api/desktop-updates/status'),originalFetch=window.fetch,requests=[],states=[];
    const input=document.querySelector('.package-search'),row=document.querySelector('.package-listing tbody tr');
    input.focus();input.setSelectionRange(1,4);
    let response;
    window.fetch=(input,options)=>{
      const url=new URL(typeof input==='string'?input:input.url,location.href);
      if((options?.method||'GET')==='GET'&&url.pathname.startsWith('/api/desktop-updates')){
        requests.push(url.pathname+url.search);
        if(url.pathname==='/api/desktop-updates/status')return Promise.resolve(new Response(JSON.stringify(response),{status:200,headers:{'Content-Type':'application/json'}}));
      }
      return originalFetch.call(window,input,options);
    };
    try{
      for(const percent of [10,55,null]){
        response={...baseline,jobs:{...baseline.jobs,pc:{state:percent===null?'done':'running',operation:'check',manager:'apt',message:percent===null?'Inventory check complete':'Checking package inventory',percent}}};
        await pollDesktop();
        states.push({inputSame:document.querySelector('.package-search')===input,rowSame:document.querySelector('.package-listing tbody tr')===row,
          focused:document.activeElement===input,selection:[input.selectionStart,input.selectionEnd],query:input.value,
          rowCount:document.querySelectorAll('.package-listing tbody tr').length,
          percent:document.querySelector('.update-job[data-kind="pc"] progress')?.value??null,
          message:document.querySelector('.update-job[data-kind="pc"]')?.textContent,
          disabled:document.querySelector('#pc-inventory-refresh').disabled});
      }
      return {requests,states};
    }finally{window.fetch=originalFetch;await pollDesktop();}
  })()`);
  assert.deepEqual(progress.requests,Array(3).fill('/api/desktop-updates/status'),'Job progress reads no full inventory');
  for(const [index,entry] of progress.states.entries()){
    assert.equal(entry.inputSame,true,'Progress preserves the package-search DOM node');
    assert.equal(entry.rowSame,true,'Progress preserves the filtered package row DOM node');
    assert.equal(entry.focused,true,'Progress preserves package-search focus');
    assert.deepEqual(entry.selection,[1,4],'Progress preserves the search caret/selection');
    assert.equal(entry.query,'openssl');assert.equal(entry.rowCount,1);
    assert.equal(entry.percent,[10,55,null][index],'Progress bar updates without rebuilding the table');
    assert.equal(entry.disabled,index<2,'Busy controls update in place');
    assert.ok(entry.message.includes(index<2?'Checking package inventory':'Inventory check complete'));
  }
  await fill('.package-search','');
  const hidden=await evaluate(`(async()=>{
    const originalFetch=window.fetch,descriptor=Object.getOwnPropertyDescriptor(document,'hidden'),requests=[];
    window.fetch=(input,options)=>{if((options?.method||'GET')==='GET')requests.push(String(input));return originalFetch.call(window,input,options);};
    Object.defineProperty(document,'hidden',{configurable:true,value:true});
    try{handleVisibility();await Promise.all([pollDesktop(),pollRentals(),poll()]);return {requests,timers:[state.pollTimer,state.rentalTimer,desktopState.timer]};}
    finally{if(descriptor)Object.defineProperty(document,'hidden',descriptor);else delete document.hidden;window.fetch=originalFetch;handleVisibility();await Promise.all([pollDesktop(),pollRentals()]);}
  })()`);
  assert.deepEqual(hidden.requests,[],'Hidden tabs issue no desktop, rental or VPS polling GET');
  assert.ok(hidden.timers.every(value=>value===null),'Hidden tabs clear all three polling timers');
  assert.equal(await evaluate('document.querySelectorAll("#server-rows tr").length'),0,'Background status and rentals do not build hidden servers');
}
async function checkSoftwarePagination(){
  // Only this open page gets a large inventory; fixture storage and SSH stay untouched.
  const id=await evaluate(`(()=>{
    const id=state.detail.id;
    state.detail={...state.detail,snapshot:{...state.detail.snapshot,
      packages:Array.from({length:1000},(_,index)=>({name:'package-'+String(index).padStart(4,'0'),version:'1.0.'+index})),
      manual_tools:Array.from({length:7},(_,index)=>({name:'tool-'+String(index).padStart(4,'0'),path:'/opt/tools/tool-'+index}))}};
    state.tab='packages';renderTabs();renderDrawerContent();return id;
  })()`);
  try{
    assert.equal(await evaluate('document.querySelectorAll("#drawer-content tbody tr").length'),100,'System packages and standalone tools share one 100-row page limit');
    assert.equal(await evaluate('document.querySelector("#package-range").textContent'),'1–100 of 1007');
    assert.equal(await evaluate('document.querySelector("#package-previous").disabled'),true);
    assert.equal(await evaluate('document.querySelector("#package-next").disabled'),false);
    await click('#package-next');
    assert.equal(await evaluate('document.querySelectorAll("#drawer-content tbody tr").length'),100);
    assert.equal(await evaluate('document.querySelector("#package-range").textContent'),'101–200 of 1007');
    assert.ok(await evaluate('document.querySelector("#drawer-content tbody tr").textContent.includes("package-0093")'),'Next page continues after the shared tools/package boundary');
    await click('#package-previous');assert.equal(await evaluate('document.querySelector("#package-range").textContent'),'1–100 of 1007');
    for(let page=1;page<11;page++)await click('#package-next');
    assert.equal(await evaluate('document.querySelectorAll("#drawer-content tbody tr").length'),7);
    assert.equal(await evaluate('document.querySelector("#package-range").textContent'),'1001–1007 of 1007');
    assert.equal(await evaluate('document.querySelector("#package-next").disabled'),true);
    await fill('.list-search','package-0999');
    await wait('document.querySelectorAll("#drawer-content tbody tr").length === 1 && document.querySelector("#drawer-content tbody tr").textContent.includes("package-0999")');
    assert.equal(await evaluate('document.querySelector("#package-range").textContent'),'1–1 of 1','Search resets paging and covers all 1,000 packages');
    assert.equal(await evaluate('document.querySelector(".software-pagination").classList.contains("hidden")'),true);
    await fill('.list-search','/opt/tools/tool-6');
    await wait('document.querySelectorAll("#drawer-content tbody tr").length === 1 && document.querySelector("#drawer-content tbody tr").textContent.includes("tool-0006")');
    await fill('.list-search','1.0.999');
    await wait('document.querySelectorAll("#drawer-content tbody tr").length === 1 && document.querySelector("#drawer-content tbody tr").textContent.includes("package-0999")');
    await fill('.list-search','');
    await wait('document.querySelectorAll("#drawer-content tbody tr").length === 100 && document.querySelector("#package-range").textContent === "1–100 of 1007"');
    await fill('.list-search','package');await click('#drawer-header .icon-button');
    assert.equal(await evaluate('state.packageTimer'),null,'Closing Software cancels its pending search debounce');
    assert.equal(await evaluate('state.detail'),null,'Closing the drawer releases its detail snapshot');
    assert.equal(await evaluate('document.querySelectorAll("#drawer-header *,#drawer-tabs *,#drawer-content *").length'),0,'Closing the drawer releases hidden header, tabs and package DOM');
  }finally{
    await evaluate(`(async()=>{closeDrawer();await openServer(${JSON.stringify(id)});state.tab='packages';renderTabs();renderDrawerContent();})()`);
  }
  assert.equal(await evaluate('state.detail.snapshot.packages.length < 1000'),true,'Reopening restores the real fixture snapshot');
}
async function showServers(){await click('[data-view=servers]');await wait('state.view==="servers" && document.querySelectorAll("#server-rows tr").length === state.servers.filter(server=>!server.archived).length');}
async function checkExpenses(readOnly=false){
  assert.equal(await evaluate('performance.getEntriesByType("resource").filter(entry=>new URL(entry.name).pathname==="/api/expenses").length'),0,'PC and Servers startup use cached costs without requesting live FX');
  assert.ok(await evaluate('document.querySelector("#server-cost-summary").getClientRects().length>0'),'Servers has a compact cost summary');
  await click('[data-view=expenses]');await wait('!!document.querySelector("#add-expense") && document.querySelectorAll(".expense-summary-card").length===3 && !expenseState.loading');
  assert.ok(await evaluate('document.querySelector("#server-section").classList.contains("hidden") && document.querySelector(".stats").classList.contains("hidden")'),'Expenses has its own workspace');
  assert.equal(await evaluate('document.querySelectorAll("[data-expense-server-id]").length'),await evaluate('state.servers.filter(server=>!server.archived).length'),'Only active VPS contribute expense rows');
  assert.ok(await evaluate('document.querySelector(".expense-once").textContent.includes("Separate from recurring")'),'One-time spending is separate');
  assert.ok(await evaluate('document.querySelector(".expense-rates").textContent.includes("Last checked:")'),'Rates show their last attempt time');
  assert.ok(await evaluate('(()=>{const total=expenseState.data.totals.all,card=document.querySelector(".expense-summary-card");return total.monthly!==null||total.known.monthly===null||card.querySelector("strong").textContent===money(total.known.monthly);})()'),'Incomplete budgets prominently show the known converted subtotal');
  const originalCurrency=await evaluate('expenseState.data.currency');
  if(!readOnly){
    await fill('#expense-display-currency','RUB');await wait('expenseState.data.currency==="RUB" && !expenseState.currencyBusy');
    await click('#add-expense');await wait('document.querySelector("#expense-modal").open');assert.equal(await evaluate('document.querySelector("#expense-form [name=amount]").value'),'','New expense starts without a preset price');
    const paymentDate=await evaluate('new Date(Date.parse(state.rentals.today+"T00:00:00Z")+3*86400000).toISOString().slice(0,10)');
    assert.equal(await evaluate('document.querySelector("#expense-form [name=next_charge_date]").value'),'','New expense starts without an invented payment date');
    await fill('#expense-form [name=name]','Cloud storage');await fill('#expense-form [name=amount]','120');await fill('#expense-form [name=currency]','USD');await fill('#expense-form [name=period]','yearly');await fill('#expense-form [name=notes]','Annual storage plan');await fill('#expense-form [name=next_charge_date]',paymentDate);await click('#expense-save');
    await wait('!document.querySelector("#expense-modal").open && expenseState.data.items.some(item=>item.name==="Cloud storage")');
    const id=await evaluate('expenseState.data.items.find(item=>item.name==="Cloud storage").id'),row=`[data-expense-id="${id}"]`;
    assert.equal(await evaluate(`expenseState.data.items.find(item=>item.id===${JSON.stringify(id)}).monthly`),'900.00','An annual USD charge normalizes to a monthly RUB estimate');
    assert.equal(await evaluate(`document.querySelector(${JSON.stringify(row+' .expense-payment time')}).getAttribute('datetime')`),paymentDate);
    assert.ok(await evaluate(`document.querySelector(${JSON.stringify(row+' .expense-payment')}).classList.contains('urgent')`),'Payment within three days is emphasized');
    assert.ok(await evaluate(`document.querySelector(${JSON.stringify(row)}).textContent.includes('Annual storage plan')`));await screenshot('expenses.png');
    await click(row+' .button');await wait('document.querySelector("#expense-modal").open');assert.equal(await evaluate('document.querySelector("#expense-form [name=next_charge_date]").value'),paymentDate);await fill('#expense-form [name=amount]','9.99');await fill('#expense-form [name=currency]','RUB');await fill('#expense-form [name=period]','once');assert.equal(await evaluate('document.querySelector("#expense-form [name=next_charge_date]").value'),paymentDate,'Changing recurrence preserves the chosen date');await fill('#expense-form [name=next_charge_date]','');await click('#expense-save');await wait('!document.querySelector("#expense-modal").open');
    assert.equal(await evaluate(`expenseState.data.items.find(item=>item.id===${JSON.stringify(id)}).next_charge_date`),null,'An expense date can be cleared');
    assert.ok(await evaluate(`document.querySelector(${JSON.stringify(row)}).textContent.includes('One-time')`));assert.equal(await evaluate('expenseState.data.totals.items.once'),'9.99');assert.equal(await evaluate('expenseState.data.totals.items.monthly'),'0.00');
    await click(row+' .icon-button');await wait('document.querySelector("#expense-delete-modal").open');await click('#expense-delete-modal [data-close]');assert.equal(await evaluate('expenseState.data.items.length'),1,'Cancel preserves the saved expense');
    await click(row+' .icon-button');await click('#expense-delete-submit');await wait('!document.querySelector("#expense-delete-modal").open && expenseState.data.items.length===0');
    const serverId=await evaluate('expenseState.data.servers[0].id');await click(`[data-expense-server-id="${serverId}"] .button`);await wait('document.querySelector("#server-modal").open');
    assert.equal(await evaluate('document.querySelector("#server-form [name=cost_amount]").value'),'1400');assert.ok(await evaluate('document.querySelector("#cost-form-hint").textContent.includes("Saved price:")'));await click('#server-form [name=cost_confirmed]');await click('#form-submit');await wait('!document.querySelector("#server-modal").open');
    assert.equal(await evaluate(`expenseState.data.servers.find(server=>server.id===${JSON.stringify(serverId)}).cost_review`),false,'Imported billing periods can be explicitly confirmed');
    await click('#refresh-expense-rate');await wait('!expenseState.rateBusy');
    await showServers();assert.equal(await evaluate('document.querySelector("#server-display-currency").value'),'RUB','Display currency is shared with Servers');
    await fill('#server-display-currency',originalCurrency);await wait(`expenseState.data.currency===${JSON.stringify(originalCurrency)} && !expenseState.currencyBusy`);
  }else{await screenshot('production-expenses.png');await showServers();}
}
async function reloadPage(){const origin=await evaluate('performance.timeOrigin');await call('Page.reload');await wait(`performance.timeOrigin !== ${origin} && document.querySelector(".nav-button.selected")?.dataset.view === "pc" && !!document.querySelector(".package-search") && !!state.csrf`);assert.equal(await evaluate('document.querySelectorAll("#server-rows tr").length'),0,'Reload also leaves hidden server rows unbuilt');await showServers();}
async function checkDiary(){
  assert.equal(new URL(process.argv[2]||'http://127.0.0.1:8788/').port,'8788','Diary edits use the isolated fixture');
  assert.ok(!process.argv.includes('--read-only'));
  await click('[data-view=diary]');await wait('diaryState.loadedWeek===diaryState.week && !document.querySelector("#diary-editor").disabled');
  await fill('#diary-date','2026-09-07');await wait('diaryState.day==="2026-09-07" && diaryState.loadedWeek==="2026-09-07"');
  assert.equal(await evaluate('document.querySelectorAll(".diary-day").length'),7);
  const note='Очистил четыре сервера и проверил SSH.\nОбновил описания в панели.\n\nНа завтра: настроить резервные копии.';
  await fill('#diary-editor',note);await wait('!diaryDraftsPending() && document.querySelector("#diary-status").textContent==="Saved"');
  await click('.diary-day[data-day="2026-09-08"]');await fill('#diary-editor','Настроил резервные копии. Проверил восстановление файлов.');
  await click('#diary-save');await wait('!diaryDraftsPending()');
  await click('.diary-day[data-day="2026-09-07"]');assert.equal(await evaluate('document.querySelector("#diary-editor").value'),note);
  await call('Page.reload');await wait('!!state.csrf && !!document.querySelector(".package-search")');
  await click('[data-view=diary]');await wait('diaryState.loadedWeek===diaryState.week');await fill('#diary-date','2026-09-07');await wait('diaryState.day==="2026-09-07" && !diaryState.loading');
  assert.equal(await evaluate('document.querySelector("#diary-editor").value'),note,'Diary text persists after a full page reload');
  await click('#diary-next');await wait('diaryState.week==="2026-09-14" && !diaryState.loading');assert.equal(await evaluate('document.querySelector("#diary-editor").value'),'');
  await click('#diary-prev');await wait('diaryState.week==="2026-09-07" && !diaryState.loading');assert.equal(await evaluate('document.querySelector("#diary-editor").value'),note);
  await evaluate('(async()=>{const original=copyText;try{copyText=async value=>{globalThis.copiedWeek=value;};await copyDiaryWeek();}finally{copyText=original;}})()');
  assert.match(await evaluate('copiedWeek'),/Проверил восстановление файлов/);
  await screenshot('diary.png');await desktopNavigation();
  await call('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
  assert.ok(await evaluate('document.documentElement.scrollWidth<=innerWidth'),'Diary fits mobile');await screenshot('diary-mobile.png');
  await call('Emulation.setDeviceMetricsOverride',{width:320,height:844,deviceScaleFactor:1,mobile:true});
  assert.ok(await evaluate('document.documentElement.scrollWidth<=innerWidth'),'Diary fits narrow screens');
  assert.deepEqual(errors.filter(e=>!e.includes('favicon.ico')),[],'Diary has no browser errors');
  console.log('PASS: weekly diary, autosave, date switching, persisted text, week copying and mobile layout');
}

async function checkTasks(){
  assert.equal(new URL(process.argv[2]||'http://127.0.0.1:8788/').port,'8788','Task editing runs against the isolated fixture');
  await click('[data-view=tasks]');await wait('taskState.loaded');
  assert.equal(await evaluate('document.querySelectorAll(".task-row").length'),0);
  await click('#task-add');await fill('#task-form [name=title]','Продлить сервер');await fill('#task-form [name=due_date]','2000-01-01');await fill('#task-form [name=notes]','Тариф Standard');
  await click('#task-save');await wait('!document.querySelector("#task-modal").open && document.querySelectorAll(".task-row").length===1');
  assert.equal(await evaluate('document.querySelector(".task-overdue").textContent'),'Overdue');
  await call('Page.reload');await wait('!!state.csrf && !!document.querySelector(".package-search")');
  await click('[data-view=tasks]');await wait('taskState.loaded && document.querySelectorAll(".task-row").length===1');
  assert.equal(await evaluate('document.querySelector(".task-title").textContent'),'Продлить сервер');
  await click('.task-actions [aria-label="Edit task"]');await fill('#task-form [name=due_date]','2026-11-23');await click('#task-save');await wait('!document.querySelector("#task-modal").open && document.querySelector(".task-date").dateTime==="2026-11-23"');
  await click('.task-check');await wait('!!document.querySelector(".task-row.completed") && !document.querySelector(".task-check").disabled');
  await click('.task-check');await wait('!document.querySelector(".task-row.completed") && !document.querySelector(".task-check").disabled');
  await screenshot('tasks.png');await desktopNavigation();
  await call('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
  assert.ok(await evaluate('document.documentElement.scrollWidth<=innerWidth'),'Tasks fit the mobile viewport');await screenshot('tasks-mobile.png');
  await call('Emulation.setDeviceMetricsOverride',{width:320,height:844,deviceScaleFactor:1,mobile:true});
  assert.ok(await evaluate('document.documentElement.scrollWidth<=innerWidth'),'Tasks fit a narrow viewport');
  assert.deepEqual(errors.filter(e=>!e.includes('favicon.ico')),[],'Task browser errors');
  console.log('PASS: dated task creation, edit, completion, reopening, reload persistence and responsive layout');
}

try{
  const target=await call('Target.createTarget',{url:'about:blank'},null);const attached=await call('Target.attachToTarget',{targetId:target.targetId,flatten:true},null);session=attached.sessionId;
  await call('Page.enable');await call('Runtime.enable');await call('Log.enable');await call('Emulation.setDeviceMetricsOverride',{width:1440,height:1000,deviceScaleFactor:1,mobile:false});
  await call('Network.setUserAgentOverride',{userAgent:'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36'});
  const url=process.argv[2]||'http://127.0.0.1:8788/';await call('Page.navigate',{url});await wait('document.querySelector(".nav-button.selected")?.dataset.view === "pc" && !!document.querySelector(".package-search") && !!state.csrf');
  if(process.argv.includes('--diary-only')){
    await checkDiary();
  }else if(process.argv.includes('--tasks-only')){
    await checkTasks();
  }else{
  await screenshot('initial-layout.png');
  assert.equal(await evaluate('document.querySelectorAll("#server-rows tr").length'),0,'PC startup leaves the hidden server table empty');
  assert.equal(await evaluate('document.title'),'Local Desk');
  assert.equal(await evaluate('document.querySelector(".brand").textContent.trim()'),'Local Desk');
  assert.equal(await evaluate('document.querySelector("#page-title").textContent'),'PC updates');
  assert.equal(await evaluate('document.querySelector("#page-context").textContent'),'This PC / Packages');
  assert.ok(await evaluate('!!document.querySelector("#local-header-actions #pc-inventory-refresh")'));
  await click('[data-view=pc]');await wait('!!document.querySelector("#local-header-actions #pc-inventory-refresh")');
  assert.equal(await evaluate('[...document.querySelectorAll(".nav-button")].at(-1).dataset.view'),'burp');
  await desktopNavigation();
  assert.equal(await evaluate('getComputedStyle(document.querySelector(".manager-card[data-manager=apt] .primary")).backgroundColor'),'rgb(215, 243, 43)','Primary actions use the lime accent');
  assert.equal(await evaluate('getComputedStyle(document.querySelector(".manager-card[data-manager=apt] .primary")).color'),'rgb(16, 18, 10)','Primary actions keep dark readable text');
  await noPortAlerts();assert.equal(await portRequestCount(),0,'PC startup does not read local ports');
  assert.deepEqual(await evaluate("['wildcard','public-address','private-LAN','local-only','special-use'].flatMap(exposure_class=>['tcp','udp'].map(protocol=>publicFacingTCP({protocol,exposure_class})))"),[true,false,true,false,false,false,false,false,false,false],'Only wildcard/public-address TCP receives exposure highlighting');
  await desktopPCLayout();
  await screenshot('home-pc.png');
  if(!process.argv.includes('--read-only'))await checkDesktopOptimizations();
  await showServers();
  assert.equal(await evaluate('document.querySelectorAll(".nav-button[data-view=rent]").length'),0,'There is no separate Rentals page');
  assert.ok(await evaluate('document.querySelector(".nav-button.selected").dataset.view==="servers" && document.querySelector("#rental-alerts").getClientRects().length>0'),'Servers displays the rental-alert panel');
  const initialServerCount=await evaluate('state.servers.filter(server=>!server.archived).length');
  assert.equal(await evaluate('document.querySelectorAll("#server-rows .lease-cell").length'),initialServerCount,'Every server retains its rental-expiry cell');
  assert.equal(await evaluate('document.querySelector("#stat-total").textContent'),String(initialServerCount));
  assert.equal(await evaluate('document.querySelectorAll("#server-rows .copy-ssh").length'),initialServerCount);
  assert.equal(await evaluate('document.documentElement.lang'),'en');
  await screenshot('desktop.png');
  await checkExpenses(process.argv.includes('--read-only'));
  if(!process.argv.includes('--read-only')){
    assert.ok(await evaluate('document.querySelector("#server-rows tr:nth-child(2) .note-cell").textContent.includes("Example server notes")'),'Existing long notes visible in overview');
    await click('#server-rows tr:nth-child(2) .server-note-preview');await wait('!!document.querySelector("#notes-editor")');
    assert.ok(await evaluate('document.querySelector("#notes-editor").value.includes("Review the renewal")'));
    await fill('#notes-editor','Example server notes. Edited from the overview.');await wait('document.querySelector("#note-save-status").textContent.includes("Saved")');
    await click('#drawer-header .icon-button');
    assert.ok(await evaluate('document.querySelector("#server-rows tr:nth-child(2) .note-cell").textContent.includes("Edited from the overview")'),'Long-note preview updates after saving');
    assert.equal(await evaluate('/[А-Яа-яЁё]/.test(document.body.innerText)'),false,'English interface');
    assert.equal(await evaluate('document.querySelectorAll("#server-rows .quick-note-button").length'),8);
    await click('#server-rows .quick-note-button');await wait('document.querySelector("#quick-note-modal").open');
    const shortNote='VPN only · <b>plain text</b>';await fill('#quick-note-input',shortNote);await click('#quick-note-save');
    await wait('!document.querySelector("#quick-note-modal").open && document.querySelector("#server-rows .quick-note-button").textContent.includes("VPN only")');
    assert.equal(await evaluate('document.querySelector("#server-rows .quick-note-button span").textContent'),shortNote);
    assert.equal(await evaluate('document.querySelectorAll("#server-rows .quick-note-button b").length'),0);
    await fill('#search','VPN only');assert.equal(await evaluate('document.querySelectorAll("#server-rows tr").length'),1);await fill('#search','');
    await reloadPage();
    assert.equal(await evaluate('document.querySelector("#server-rows .quick-note-button span").textContent'),shortNote);
    await click('#desktop-reminders');await wait('!document.querySelector("#desktop-reminders").disabled && !document.querySelector("#desktop-reminders").checked');
    await reloadPage();
    assert.equal(await evaluate('document.querySelector("#desktop-reminders").checked'),false);
    await click('#desktop-reminders');await wait('!document.querySelector("#desktop-reminders").disabled && document.querySelector("#desktop-reminders").checked');
    await click('#notification-preview');await wait('document.querySelector("#toasts").textContent.includes("Preview sent")');
    const rentalFixtures=await evaluate('(async()=>{const b=await (await fetch("/api/bootstrap")).json();const base=Date.parse(b.rentals.today+"T00:00:00Z");return b.servers.slice(0,5).map((s,i)=>({id:s.id,date:new Date(base+[7,3,0,-1,8][i]*86400000).toISOString().slice(0,10)}));})()');
    for(const item of rentalFixtures){await evaluate(`(async()=>{const b=await (await fetch('/api/bootstrap')).json();await fetch('/api/servers/${item.id}',{method:'PATCH',headers:{'Content-Type':'application/json','X-VPS-CSRF':b.csrf},body:JSON.stringify({lease_end:'${item.date}'})});})()`);}
    await evaluate('document.dispatchEvent(new Event("visibilitychange"))');
    await wait('document.querySelectorAll(".rental-alert-row").length === 4');
    assert.deepEqual(await evaluate('[...document.querySelectorAll(".rental-alert-row")].map(e=>e.className.split(" ").pop())'),['overdue','today','urgent','soon']);
    assert.equal(await evaluate('document.querySelector("#stat-rent").textContent'),'4');
    assert.ok(await evaluate('state.view==="servers" && [...document.querySelectorAll(".rental-alert-row .rental-alert-actions button")].every(button=>button.textContent==="Update date" && button.getClientRects().length>0)'),'Rental date actions remain visible in Servers');
    const alertDate=await evaluate('document.querySelector(".rental-alert-row time").textContent');
    await click('.rental-alert-row .rental-alert-actions button');await wait('document.querySelector("#server-modal").open && document.activeElement?.name==="lease_end"');
    assert.equal(await evaluate('document.querySelector("[name=lease_end]").value'),alertDate,'The rental action opens the existing server expiry date');
    await click('[data-close="server-modal"]');await wait('!document.querySelector("#server-modal").open');
    await screenshot('rental-alerts.png');
    await call('Browser.grantPermissions',{origin:new URL(url).origin,permissions:['clipboardReadWrite','clipboardSanitizedWrite']},null);
    await click('#server-rows .copy-ssh');
    await wait('document.querySelector("#server-rows .copy-ssh").textContent.includes("Copied")');
    assert.equal(await evaluate('navigator.clipboard.readText()'),'ssh server-a');
    await screenshot('clipboard.png');
    await click('#server-rows tr:nth-child(2) .copy-ssh');
    await wait('(async()=>await navigator.clipboard.readText()==="ssh server-b")()');
    await click('#server-rows .server-name');await wait('document.querySelectorAll("#drawer-tabs button").length === 6');
    await click('#drawer-header .copy-ssh');await wait('document.querySelector("#drawer-header .copy-ssh").textContent.includes("Copied")');assert.equal(await evaluate('navigator.clipboard.readText()'),'ssh server-a');
    assert.equal(await evaluate('document.querySelector(".ssh-command-line code").textContent'),'ssh server-a');
    await evaluate('navigator.clipboard.writeText("")');
    await evaluate('window.savedClipboard=navigator.clipboard.writeText.bind(navigator.clipboard);navigator.clipboard.writeText=()=>Promise.reject(new Error("Clipboard unavailable"))');
    await click('#drawer-header .copy-ssh');await wait('(async()=>await navigator.clipboard.readText()==="ssh server-a")()');
    assert.equal(await evaluate('navigator.clipboard.readText()'),'ssh server-a');await evaluate('navigator.clipboard.writeText=window.savedClipboard');
    await evaluate('document.querySelectorAll("#drawer-tabs button")[5].click()');await wait('!!document.querySelector("#notes-editor")');
    const note='UI smoke test: заметка <img src=x onerror=alert(1)>\nВторая строка';await fill('#notes-editor',note);await wait('document.querySelector("#note-save-status").textContent.includes("Saved")');
    assert.equal(await evaluate('document.querySelectorAll("#drawer-content img").length'),0);
    await reloadPage();await click('#server-rows .server-name');await wait('document.querySelectorAll("#drawer-tabs button").length === 6');await evaluate('document.querySelectorAll("#drawer-tabs button")[5].click()');
    assert.equal(await evaluate('document.querySelector("#notes-editor").value'),note);
    await screenshot('notes.png');
    await evaluate('document.querySelectorAll("#drawer-tabs button")[1].click()');assert.ok(await evaluate('document.querySelector("#drawer-content").textContent.includes("systemd")'));
    await evaluate('document.querySelectorAll("#drawer-tabs button")[2].click()');assert.ok(await evaluate('document.querySelector("#drawer-content").textContent.includes("ssh.service")'));
    await evaluate('document.querySelectorAll("#drawer-tabs button")[3].click()');assert.ok(await evaluate('document.querySelector("#drawer-content").textContent.includes("example-web")'));
    await evaluate('document.querySelectorAll("#drawer-tabs button")[4].click()');await fill('.list-search','openssh');await wait('document.querySelectorAll("#drawer-content tbody tr").length === 1 && document.querySelector("#drawer-content").textContent.includes("openssh-server")');
    const host=await evaluate('(()=>{let a=document.querySelector("#drawer-header a[target=_blank]");return {href:a.href,rel:a.rel};})()');assert.equal(host.href,'https://example.com/hosting');assert.ok(host.rel.includes('noopener'));
    await checkSoftwarePagination();
    await click('#drawer-header .icon-button');await click('#server-rows .actions button[aria-label^="Edit "]');await wait('document.querySelector("#server-modal").open');
    await fill('[name=lease_end]','2026-12-31');await fill('[name=provider_url]','https://example.com/new-cabinet');await click('#form-submit');await wait('!document.querySelector("#server-modal").open');await wait('document.querySelector("#server-rows").textContent.includes("2026-12-31")');
    await wait('document.querySelectorAll(".rental-alert-row").length === 3');
    await click('#add-server');await fill('[name=name]','New test VPS');await fill('[name=alias]','new-test');await fill('[name=host]','192.0.2.99');await fill('[name=mode]','managed');await fill('[name=notes]','Added notes');await click('#form-submit');await wait('document.querySelectorAll("#server-rows tr").length === 9');await wait('document.querySelector("#job-content").textContent.includes("SSH configured")');await click('[data-close="job-modal"]');
    await fill('#search','new-test');assert.equal(await evaluate('document.querySelectorAll("#server-rows tr").length'),1);await fill('#search','');
    const trashId=await evaluate('state.servers.find(server=>server.alias==="new-test").id');
    const trashRow=`#server-rows tr[data-server-id="${trashId}"]`;
    assert.equal(await evaluate('document.querySelectorAll(".nav-button[data-view=archive],.nav-button[data-view=trash]").length'),0,'Trash is an icon, not a full navigation section');
    assert.ok(await evaluate('document.querySelector("#open-trash").getBoundingClientRect().width<=44'),'Trash uses a small icon button');
    await click(trashRow+' .move-trash');await wait('document.querySelector("#delete-modal").open');
    assert.ok(await evaluate('document.querySelector("#delete-confirmation").classList.contains("hidden")'),'Moving to Trash does not require permanent-deletion confirmation');
    await click('#delete-modal [data-close]');assert.equal(await evaluate('state.servers.filter(server=>!server.archived).length'),9,'Cancel keeps the active entry');
    await click(trashRow+' .move-trash');await click('#delete-submit');await wait('!document.querySelector("#delete-modal").open && document.querySelectorAll("#server-rows tr").length===8');
    assert.equal(await evaluate('document.querySelector("#trash-count").textContent'),'1');
    await reloadPage();await click('#open-trash');await wait('state.view==="trash" && document.querySelectorAll("#server-rows tr").length===1');
    assert.equal(await evaluate('document.querySelector("#page-title").textContent'),'Trash');
    assert.ok(await evaluate('document.querySelector("#rental-alerts").classList.contains("hidden")'),'Trashed entries have no rental reminders');
    assert.equal(await evaluate('document.querySelectorAll("#server-rows .move-trash,#server-rows .copy-ssh").length'),0,'Trash offers no SSH action or nested trash action');
    await screenshot('trash.png');await click(trashRow+' .restore-server');await wait('document.querySelectorAll("#server-rows tr").length===0');
    await click('#back-servers');await wait('document.querySelectorAll("#server-rows tr").length===9');
    assert.equal(await evaluate('state.servers.find(server=>server.alias==="new-test").notes'),'Added notes','Restoring retains saved notes');
    await click(trashRow+' .move-trash');await click('#delete-submit');await wait('!document.querySelector("#delete-modal").open && document.querySelectorAll("#server-rows tr").length===8');
    await click('#open-trash');await click(trashRow+' .delete-permanently');await wait('document.querySelector("#delete-modal").open');
    assert.ok(await evaluate('document.querySelector("#delete-submit").disabled'));
    await fill('#delete-alias','new-tes');assert.ok(await evaluate('document.querySelector("#delete-submit").disabled'),'A wrong alias cannot confirm deletion');
    await click('#delete-modal [data-close]');assert.ok(await evaluate('state.servers.some(server=>server.alias==="new-test")'),'Cancel keeps the trashed entry');
    await click(trashRow+' .delete-permanently');await fill('#delete-alias','new-test');await screenshot('delete-confirmation.png');
    assert.equal(await evaluate('document.querySelector("#delete-expected-alias").textContent'),'new-test','A queued close event does not clear a reopened deletion dialog');
    assert.equal(await evaluate('document.querySelector("#delete-alias").value'),'new-test');await click('#delete-submit');
    await wait('!document.querySelector("#delete-modal").open && document.querySelectorAll("#server-rows tr").length===0');
    assert.ok(await evaluate('document.querySelector("#empty-state").textContent.includes("Trash is empty")'));
    await reloadPage();assert.ok(await evaluate('!state.servers.some(server=>server.alias==="new-test")'),'Permanent deletion survives reload');
    await click('#refresh-all');await wait('!document.querySelector("#refresh-all").disabled');
    await click('[data-view=events]');await wait('document.querySelectorAll(".event-row").length > 0');await click('[data-view=servers]');
    await click('[data-view=pc]');await wait('!!document.querySelector(".package-search")');
    assert.ok(await evaluate('document.querySelector("#desktop-section").textContent.includes("warp-terminal")'));
    assert.ok(await evaluate('document.querySelector(".stats").classList.contains("hidden")'));
    await screenshot('pc-updates.png');await fill('.package-search','openssl');assert.equal(await evaluate('document.querySelectorAll("#desktop-section tbody tr").length'),1);
    await evaluate('window.confirm=()=>true');
    assert.equal(await evaluate('document.querySelectorAll(".manager-card").length'),6);
    assert.ok(await evaluate('document.querySelector(".manager-card[data-manager=apt]").textContent.includes("04/09/2026")'));
    await managerAction('apt','upgrade');
    await wait('document.querySelector("#desktop-section").textContent.includes("Test package action: upgrade")');
    await managerAction('snap','check');await wait('document.querySelector("#desktop-section").textContent.includes("Snap check complete")');
    await click('.manager-tabs [data-manager=snap]');assert.ok(await evaluate('document.querySelector("#desktop-section").textContent.includes("firefox")'));
    await managerAction('snap','upgrade');await wait('document.querySelector("#desktop-section").textContent.includes("Snap update complete")');
    await evaluate(`window.sentToolRequests=[];window.confirmations=[];window.acceptUpdates=false;window.confirm=message=>{window.confirmations.push(message);return window.acceptUpdates;};window.previousFetch=window.fetch.bind(window);window.fetch=(input,options)=>{if(String(input).startsWith('/api/tool-updates/'))window.sentToolRequests.push({path:String(input),body:options?.body?JSON.parse(options.body):{}});return window.previousFetch(input,options);};`);
    assert.deepEqual(await evaluate('[...document.querySelectorAll(".manager-card")].map(e=>e.dataset.manager)'),['apt','snap','go','pdtm','go-tools','pipx']);
    assert.equal(await evaluate('document.querySelectorAll(".manager-card[data-manager=go] [data-action=upgrade],.manager-card[data-manager=pdtm] [data-action=upgrade],.manager-card[data-manager=go-tools] [data-action=upgrade],.manager-card[data-manager=pipx] [data-action=upgrade]").length'),0,'Tools have no bulk-upgrade action');
    await click('.manager-tabs [data-manager=go]');
    assert.equal(await evaluate('document.querySelectorAll(".tool-table tbody tr").length'),1);
    assert.ok(await evaluate('document.querySelector(".tool-table").textContent.includes("/usr/local/go")'));
    await managerAction('go','check');await wait('document.querySelector("#desktop-section").textContent.includes("Go toolchain check complete")');
    const beforeCancellation=await evaluate('window.sentToolRequests.length');await click('.tool-update[data-tool-id=go]');
    assert.equal(await evaluate('window.sentToolRequests.length'),beforeCancellation,'Cancelled confirmation sends no update request');
    assert.ok(await evaluate('window.confirmations.at(-1).includes("go1.27.1")'));
    await evaluate('window.acceptUpdates=true');await click('.tool-update[data-tool-id=go]');
    await wait('document.querySelector("#desktop-section").textContent.includes("Tool action: go/go go1.27.1")');
    assert.deepEqual(await evaluate('window.sentToolRequests.at(-1)'),{path:'/api/tool-updates/go/install',body:{item:'go',version:'go1.27.1',allow_unknown_version:false}});
    await toolScreenshot('go-toolchain.png');
    await click('.manager-tabs [data-manager=pdtm]');await managerAction('pdtm','check');
    await wait('document.querySelector("#desktop-section").textContent.includes("ProjectDiscovery check complete")');
    assert.equal(await evaluate('document.querySelectorAll(".tool-table tbody tr").length'),3);
    assert.equal(await evaluate('document.querySelector(".tool-update[data-tool-id=nuclei]").textContent'),'Install stable');
    await fill('.package-search','nuclei');assert.equal(await evaluate('document.querySelectorAll(".tool-table tbody tr").length'),1);await fill('.package-search','');
    await click('.tool-update[data-tool-id=httpx]');await wait('document.querySelector("#desktop-section").textContent.includes("Tool action: pdtm/httpx v1.10.1")');
    assert.deepEqual(await evaluate('window.sentToolRequests.at(-1)'),{path:'/api/tool-updates/pdtm/install',body:{item:'httpx',version:'v1.10.1',allow_unknown_version:false}});
    await click('.tool-update[data-tool-id=nuclei]');await wait('document.querySelector("#desktop-section").textContent.includes("Tool action: pdtm/nuclei v3.5.0")');
    assert.ok(await evaluate('window.confirmations.at(-1).includes("installed version is unknown") && window.confirmations.at(-1).includes("newer or custom build")'));
    assert.deepEqual(await evaluate('window.sentToolRequests.at(-1)'),{path:'/api/tool-updates/pdtm/install',body:{item:'nuclei',version:'v3.5.0',allow_unknown_version:true}});
    await toolScreenshot('projectdiscovery-tools.png');
    await click('.manager-tabs [data-manager=go-tools]');await managerAction('go-tools','check');
    await wait('document.querySelector("#desktop-section").textContent.includes("Go tools check complete")');
    assert.equal(await evaluate('document.querySelector(".tool-update[data-tool-id=ffuf]").disabled'),true);
    assert.ok(await evaluate('document.querySelector(".tool-update[data-tool-id=ffuf]").closest("tr").textContent.includes("Manual")'));
    const beforeManual=await evaluate('window.sentToolRequests.length');await click('.tool-update[data-tool-id=ffuf]');
    assert.equal(await evaluate('window.sentToolRequests.length'),beforeManual,'Custom build remains untouched');
    await click('.tool-update[data-tool-id=katana]');await wait('document.querySelector("#desktop-section").textContent.includes("Tool action: go-tools/katana v1.7.1")');
    assert.deepEqual(await evaluate('window.sentToolRequests.at(-1)'),{path:'/api/tool-updates/go-tools/install',body:{item:'katana',version:'v1.7.1',allow_unknown_version:false}});
    await toolScreenshot('go-tools.png');
    await click('.manager-tabs [data-manager=pipx]');await managerAction('pipx','check');
    await wait('document.querySelector("#desktop-section").textContent.includes("pipx check complete")');
    assert.equal(await evaluate('document.querySelectorAll(".tool-update:disabled").length'),2);
    assert.ok(await evaluate('document.querySelector(".tool-update[data-tool-id=ai-ffuf]").closest("tr").textContent.includes("editable")'));
    assert.ok(await evaluate('document.querySelector(".tool-update[data-tool-id=penelope-shell-handler]").closest("tr").textContent.includes("Version-pinned")'));
    await click('.tool-update[data-tool-id=sqlmap]');await wait('document.querySelector("#desktop-section").textContent.includes("Tool action: pipx/sqlmap 1.10.9")');
    assert.deepEqual(await evaluate('window.sentToolRequests.at(-1)'),{path:'/api/tool-updates/pipx/install',body:{item:'sqlmap',version:'1.10.9',allow_unknown_version:false}});
    await toolScreenshot('pipx-tools.png');
    await click('.manager-tabs [data-manager=apt]');
    await click('[data-view=burp]');await wait('!!document.querySelector("#burp-check-version")');
    assert.ok(await evaluate('document.querySelector("#desktop-section").textContent.includes("2026.8")'));
    assert.equal(await evaluate('document.querySelectorAll("#burp-weekly").length'),0,'Burp has no weekly scheduler control');
    assert.doesNotMatch(await evaluate('document.querySelector("#desktop-section").textContent'),/Next check|weekly checks/i,'Burp shows no automatic-check promise');
    await screenshot('burp-updates.png');
    await evaluate('[...document.querySelectorAll("#desktop-section button")].find(b=>b.textContent.includes("Download and update")).click()');
    await wait('document.querySelector("#desktop-section").textContent.includes("Test official install")');
    assert.equal(await portRequestCount(),0,'Server, PC and Burp workflows do not read local ports');
    await click('[data-view=listeners]');await wait('document.querySelectorAll(".listener-table tbody tr").length === 4 && !document.querySelector("#listener-refresh").disabled');
    assert.equal(await evaluate('document.querySelector(".listener-scopes [data-scope=all]").getAttribute("aria-pressed")'),'true','All sockets are shown by default');
    await noPortAlerts();
    assert.equal(await evaluate('document.querySelectorAll(".listener-table tr.tcp-public-row,.listener-table tr.tcp-wildcard-row").length'),1,'Only the external TCP fixture is highlighted');
    assert.equal(await evaluate('document.querySelectorAll(".listener-table tr.tcp-wildcard-row").length'),1);
    assert.ok(await evaluate('document.querySelector(".listener-table tr.tcp-wildcard-row").textContent.includes("0.0.0.0")'));
    assert.ok(await evaluate('[...document.querySelectorAll(".listener-table tbody tr")].filter(row=>row.textContent.includes("avahi")||row.textContent.includes("python3")||row.textContent.includes("postgres")).every(row=>!row.matches(".tcp-public-row,.tcp-wildcard-row")&&!row.querySelector(".tcp-exposure"))'),'UDP and localhost rows have no TCP exposure highlight');
    assert.ok(await evaluate('document.querySelector(".listener-legend")?.textContent.includes("Internet reachability is not confirmed")'),'Bind exposure is not presented as proven Internet reachability');
    assert.ok(await evaluate('!!document.querySelector("#local-header-actions #listener-copy-command") && !!document.querySelector("#local-header-actions #listener-refresh")'),'Listener actions are in the page header');
    await click('.listener-scopes [data-scope=external]');assert.equal(await evaluate('document.querySelectorAll(".listener-table tbody tr").length'),2,'External scope includes nonloopback TCP and UDP');
    await click('.listener-scopes [data-scope=all]');
    assert.equal(await evaluate('document.querySelectorAll(".listener-table tbody tr").length'),4);
    assert.equal(await evaluate('document.querySelectorAll(".listener-scope.loopback").length'),2);
    assert.equal(await evaluate('document.querySelectorAll(".listener-scope.all_interfaces").length'),2);
    assert.ok(await evaluate('document.querySelector(".listener-table").textContent.includes("Not visible")'));
    await click('.listener-filters [data-protocol=udp]');assert.equal(await evaluate('document.querySelectorAll(".listener-table tbody tr").length'),1);
    assert.ok(await evaluate('document.querySelector(".listener-table").textContent.includes("avahi")'));
    assert.equal(await evaluate('document.querySelectorAll(".listener-table .tcp-public-row,.listener-table .tcp-wildcard-row,.listener-table .tcp-exposure").length'),0,'UDP is displayed without TCP highlighting');
    await click('.listener-filters [data-protocol=tcp]');assert.equal(await evaluate('document.querySelectorAll(".listener-table tbody tr").length'),3);
    await fill('#listener-search','2345');assert.equal(await evaluate('document.querySelectorAll(".listener-table tbody tr").length'),1);
    assert.ok(await evaluate('document.querySelector(".listener-table").textContent.includes("postgres")'));
    await fill('#listener-search','::1');assert.equal(await evaluate('document.querySelectorAll(".listener-table tbody tr").length'),1);
    await click('.listener-copy-address');await wait('(async()=>await navigator.clipboard.readText()==="[::1]:5432")()');
    await fill('#listener-search','8787');assert.equal(await evaluate('document.querySelectorAll(".listener-table tbody tr").length'),1);
    await click('.listener-copy-address');await wait('(async()=>await navigator.clipboard.readText()==="127.0.0.1:8787")()');
    await fill('#listener-search','avahi');assert.equal(await evaluate('document.querySelectorAll(".listener-table tbody tr").length'),0);
    await click('.listener-filters [data-protocol=all]');assert.equal(await evaluate('document.querySelectorAll(".listener-table tbody tr").length'),1);
    await click('.listener-copy-address');await wait('(async()=>await navigator.clipboard.readText()==="*:5353")()');
    await fill('#listener-search','');assert.equal(await evaluate('document.querySelectorAll(".listener-table tbody tr").length'),4);
    await click('#listener-copy-command');await wait('(async()=>await navigator.clipboard.readText()==="ss -lntup")()');
    const listenerReads=await evaluate('performance.getEntriesByType("resource").filter(e=>new URL(e.name).pathname==="/api/local-listeners").length');
    await click('#listener-refresh');await wait(`performance.getEntriesByType("resource").filter(e=>new URL(e.name).pathname==="/api/local-listeners").length > ${listenerReads} && !document.querySelector("#listener-refresh").disabled`);
    assert.equal(await evaluate('document.querySelectorAll(".listener-table tbody tr").length'),4);await wait('document.querySelector("#toasts").children.length === 0');await screenshot('listeners.png');
    const beforeReturningToPC=await portRequestCount();
    await click('[data-view=pc]');await wait('!!document.querySelector(".package-search") && !document.querySelector("#listener-search")');
    assert.ok(await evaluate('document.querySelector("#desktop-section").textContent.includes("Package updates")'),'PC renders after shared container was used for ports');
    await evaluate('document.dispatchEvent(new Event("visibilitychange"));loadDesktop()');
    assert.equal(await portRequestCount(),beforeReturningToPC,'Returning to PC, visibility changes and status refresh do not read local ports');await noPortAlerts();
    await click('[data-view=listeners]');await wait('document.querySelectorAll(".listener-table tbody tr").length === 4 && !document.querySelector("#listener-refresh").disabled');
    await click('[data-view=servers]');
  } else {
    await click('[data-view=pc]');await wait('!!document.querySelector(".package-search")');await screenshot('production-pc-updates.png');
    if(await evaluate('!!document.querySelector(".manager-tabs [data-manager=pdtm]")')){await click('.manager-tabs [data-manager=pdtm]');await wait('!!document.querySelector(".tool-table")');await toolScreenshot('production-projectdiscovery-tools.png');}
    await click('[data-view=burp]');await wait('!!document.querySelector("#burp-check-version")');await screenshot('production-burp-updates.png');
    await click('[data-view=listeners]');await wait('!!document.querySelector("#listener-count") && !document.querySelector("#listener-refresh").disabled');await noPortAlerts();assert.equal(await evaluate('document.querySelector(".listener-scopes [data-scope=all]").getAttribute("aria-pressed")'),'true');await screenshot('production-listeners.png');
    await click('[data-view=servers]');
  }
  await call('Emulation.setDeviceMetricsOverride',{width:1280,height:900,deviceScaleFactor:1,mobile:false});await screenshot('laptop.png');
  assert.ok(await evaluate('document.documentElement.scrollWidth <= 1280'),'laptop horizontal overflow');
  await click('[data-view=pc]');await wait('!!document.querySelector(".pc-workspace") && !document.querySelector("#listener-search")');assert.ok(await evaluate('document.documentElement.scrollWidth <= 1280'),'laptop PC workspace horizontal overflow');await desktopNavigation();await desktopPCLayout();await screenshot('laptop-pc.png');
  for(const width of [1920,1600,1440,1280,1201,1200,1101,1100,1001,1000,760,390]){await call('Emulation.setDeviceMetricsOverride',{width,height:900,deviceScaleFactor:1,mobile:false});await desktopNavigation();await desktopPCLayout();assert.ok(await evaluate('document.documentElement.scrollWidth <= innerWidth'),'PC workspace horizontal overflow at '+width+'px');}
  await click('[data-view=servers]');
  await call('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});await screenshot('mobile.png');
  if(await evaluate('document.documentElement.scrollWidth > 390'))console.log('Overflow:',await evaluate('JSON.stringify([...document.querySelectorAll("body *")].filter(e=>e.getBoundingClientRect().right>395 && getComputedStyle(e).display!=="none").slice(0,30).map(e=>[e.tagName,e.id,e.className,e.getBoundingClientRect().right]))'));
  assert.ok(await evaluate('document.documentElement.scrollWidth <= 390'), 'mobile horizontal overflow');
  assert.equal(await evaluate('getComputedStyle(document.querySelector("#server-rows tr")).display'),'grid');
  await evaluate('document.querySelector("#server-rows .copy-ssh").scrollIntoView({block:"center"})');await screenshot('mobile-card.png');
  await call('Emulation.setDeviceMetricsOverride',{width:320,height:844,deviceScaleFactor:1,mobile:true});await screenshot('narrow-card.png');
  assert.ok(await evaluate('[...document.querySelectorAll("#server-rows .actions")].every(actions=>{const right=actions.getBoundingClientRect().right;return [...actions.children].every(button=>button.getBoundingClientRect().right<=right+1&&button.scrollWidth<=button.clientWidth+1);})'),'All server actions and Copy SSH text fit a 320px viewport');
  await call('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
  await click('[data-view=expenses]');await wait('document.querySelectorAll(".expense-summary-card").length===3 && !expenseState.loading');await evaluate('window.scrollTo(0,0)');assert.ok(await evaluate('document.documentElement.scrollWidth<=innerWidth'),'Mobile Expenses fits the viewport');await screenshot('mobile-expenses.png');
  await click('[data-view=listeners]');await wait('!!document.querySelector("#listener-count") && !document.querySelector("#listener-refresh").disabled');
  await noPortAlerts();
  await evaluate('window.scrollTo(0,0)');
  assert.ok(await evaluate('document.documentElement.scrollWidth <= 390'),'mobile listening-ports horizontal overflow');await screenshot('mobile-listeners.png');
  await click('[data-view=pc]');await wait('!!document.querySelector(".manager-tabs") && !document.querySelector("#listener-search")');
  assert.equal(await evaluate('document.querySelectorAll(".update-overview .overview-item").length'),4);
  assert.ok(await evaluate('document.documentElement.scrollWidth <= 390'),'mobile PC overview horizontal overflow');
  await desktopNavigation();await desktopPCLayout();
  assert.ok(await evaluate('(()=>{const left=document.querySelector(".pc-workspace .package-panel").getBoundingClientRect(),side=document.querySelector(".pc-workspace .pc-side").getBoundingClientRect();return Math.abs(left.left-side.left)<=4&&(side.top>=left.bottom-4||left.top>=side.bottom-4);})()'),'Mobile PC columns stack without overlap');
  await evaluate('window.scrollTo(0,0)');await screenshot('mobile-pc.png');
  if(await evaluate('!!document.querySelector(".manager-tabs [data-manager=pipx]")')){await click('.manager-tabs [data-manager=pipx]');await wait('!!document.querySelector(".tool-table")');assert.ok(await evaluate('document.documentElement.scrollWidth <= 390'),'mobile tool-manager horizontal overflow');await toolScreenshot('mobile-pipx-tools.png');}
  const filtered=errors.filter(e=>!e.includes('favicon.ico'));assert.deepEqual(filtered,[],'browser JS/CSP errors');
  console.log('PASS: browser rendering, responsive layout, local ports'+(process.argv.includes('--read-only')?' (read-only)':', individual Go/ProjectDiscovery/pipx updates, cancelled and unknown-version confirmations, manual install exclusions, clipboard and fallback, notes persistence, plaintext safety, tabs, hosting links, lease edits, onboarding UI, search and refresh'));
  }
}catch(error){
  console.error('Browser check failed:',error.message);
  try{await screenshot('failure.png');}catch{}
  throw error;
}finally{
  try{await call('Browser.close',{},null);}catch{}
  chrome.kill();for(const item of pending.values())clearTimeout(item.timer);
  await new Promise(resolve=>chrome.exitCode===null?chrome.once('exit',resolve):resolve());
  await rm(directory,{recursive:true,force:true,maxRetries:5,retryDelay:100});
}

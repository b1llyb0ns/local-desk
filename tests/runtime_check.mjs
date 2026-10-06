// Read-only runtime census with an isolated Chromium profile and no real jobs.
import {spawn} from 'node:child_process';
import {mkdtemp,mkdir,writeFile,rm} from 'node:fs/promises';
import {dirname,join,resolve} from 'node:path';
import {fileURLToPath} from 'node:url';
import assert from 'node:assert/strict';

const root=resolve(dirname(fileURLToPath(import.meta.url)),'..');
const targetURL=process.argv[2]||'http://127.0.0.1:8788/';
assert.ok(['http://127.0.0.1:8788/','http://127.0.0.1:8787/'].includes(targetURL),'Only the local panel or isolated fixture is allowed');
const output=resolve(root,process.argv[3]||'tmp/runtime-before.json');
assert.equal(dirname(output),join(root,'tmp'),'Aggregate output must stay in project tmp');
await mkdir(join(root,'tmp'),{recursive:true,mode:0o700});
const profile=await mkdtemp(join(root,'tmp/runtime-profile-'));
const chrome=spawn('chromium',['--headless=new','--disable-gpu','--no-first-run','--no-default-browser-check','--disable-background-networking','--remote-debugging-pipe','--user-data-dir='+profile,'about:blank'],{stdio:['ignore','ignore','pipe','pipe','pipe']});
let nextId=0,session,buffer=Buffer.alloc(0);const pending=new Map(),errors=[];
chrome.stderr.on('data',()=>{});
chrome.stdio[4].on('data',chunk=>{
  buffer=Buffer.concat([buffer,chunk]);let end;
  while((end=buffer.indexOf(0))>=0){
    const raw=buffer.subarray(0,end).toString();buffer=buffer.subarray(end+1);if(!raw)continue;
    const message=JSON.parse(raw),request=pending.get(message.id);
    if(request){pending.delete(message.id);clearTimeout(request.timer);message.error?request.reject(new Error(JSON.stringify(message.error))):request.resolve(message.result);}
    else if(message.method==='Runtime.exceptionThrown')errors.push(message.params.exceptionDetails.text);
  }
});
function call(method,params={},sessionId=session){return new Promise((resolve,reject)=>{const id=++nextId,timer=setTimeout(()=>{pending.delete(id);reject(new Error('CDP timeout: '+method));},45000);pending.set(id,{resolve,reject,timer});chrome.stdio[3].write(JSON.stringify({id,method,params,...(sessionId?{sessionId}:{})})+'\0');});}
async function evaluate(expression){const result=await call('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true,userGesture:true});if(result.exceptionDetails)throw new Error(result.exceptionDetails.exception?.description||result.exceptionDetails.text);return result.result.value;}
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
async function wait(expression){for(let n=0;n<100;n++){if(await evaluate(expression))return;await pause(100);}throw new Error('Runtime UI did not become ready');}

// These metrics live only in JS: collecting them creates no DOM mutations.
const instrumentation=`(()=>{
  const nativeFetch=window.fetch.bind(window),nativeTimeout=window.setTimeout.bind(window),nativeClear=window.clearTimeout.bind(window),nativeInterval=window.setInterval.bind(window),nativeClearInterval=window.clearInterval.bind(window);
  const counts={requests:{},network:{},mocked:{},timerScheduled:0,timerCallbacks:0,intervalScheduled:0,intervalCallbacks:0,mutations:{records:0,childList:0,attributes:0,characterData:0,added:0,removed:0,packages:0,jobs:0,servers:0,other:0},longTasks:0,longTaskMs:0,blockedWrites:0};
  const timeouts=new Map(),intervals=new Map();
  const bump=(map,key)=>{map[key]=(map[key]||0)+1;};
  const profiler=window.__runtimeProfiler={counts,timeouts,intervals,activeJob:false,status:null,jobTick:0};
  window.fetch=(input,options)=>{
    const url=new URL(typeof input==='string'?input:input.url,location.href),method=(options?.method||input?.method||'GET').toUpperCase();
    if(method!=='GET'){counts.blockedWrites++;throw new Error('Runtime profiling blocks state-changing requests');}
    if(url.origin!==location.origin)throw new Error('Runtime profiling blocks external requests');
    const path=url.pathname.replace(/\\/servers\\/[^/]+/,'/servers/:id')+(url.pathname==='/api/desktop-updates'?url.search:'');
    bump(counts.requests,path);
    if(profiler.activeJob&&url.pathname==='/api/desktop-updates/status'){
      bump(counts.mocked,path);profiler.jobTick++;
      return Promise.resolve(new Response(JSON.stringify({...profiler.status,jobs:{...profiler.status.jobs,pc:{state:'running',operation:'check',manager:'apt',message:'Checking package inventory',percent:(profiler.jobTick*7)%100}}}),{status:200,headers:{'Content-Type':'application/json'}}));
    }
    bump(counts.network,path);return nativeFetch(input,options);
  };
  window.setTimeout=(callback,delay,...args)=>{counts.timerScheduled++;let id;id=nativeTimeout(()=>{timeouts.delete(id);counts.timerCallbacks++;typeof callback==='function'?callback(...args):(0,eval)(callback);},delay);timeouts.set(id,{delay:Number(delay)||0,name:typeof callback==='function'?(callback.name||'anonymous'):'string'});return id;};
  window.clearTimeout=id=>{timeouts.delete(id);return nativeClear(id);};
  window.setInterval=(callback,delay,...args)=>{counts.intervalScheduled++;const id=nativeInterval(()=>{counts.intervalCallbacks++;typeof callback==='function'?callback(...args):(0,eval)(callback);},delay);intervals.set(id,{delay:Number(delay)||0,name:typeof callback==='function'?(callback.name||'anonymous'):'string'});return id;};
  window.clearInterval=id=>{intervals.delete(id);return nativeClearInterval(id);};
  new MutationObserver(records=>{for(const record of records){const c=counts.mutations;c.records++;c[record.type]++;c.added+=record.addedNodes.length;c.removed+=record.removedNodes.length;const target=record.target.nodeType===1?record.target:record.target.parentElement;if(target?.closest('.package-listing'))c.packages++;else if(target?.closest('.update-job')||target?.querySelector?.('.update-job'))c.jobs++;else if(target?.closest('#server-rows'))c.servers++;else c.other++;}}).observe(document,{subtree:true,childList:true,attributes:true,characterData:true});
  try{new PerformanceObserver(list=>{for(const entry of list.getEntries()){counts.longTasks++;counts.longTaskMs+=entry.duration;}}).observe({type:'longtask',buffered:true});}catch{}
})();`;

async function snapshot(gc=false){
  if(gc){await call('HeapProfiler.collectGarbage');await call('HeapProfiler.collectGarbage');}
  const performance=Object.fromEntries((await call('Performance.getMetrics')).metrics.map(entry=>[entry.name,entry.value]));
  const dom=await call('Memory.getDOMCounters');
  const browser=await evaluate(`(()=>{const p=window.__runtimeProfiler;return {counts:p.counts,activeTimers:[...p.timeouts.values()],activeIntervals:[...p.intervals.values()],connectedElements:document.querySelectorAll('*').length,serverRows:document.querySelectorAll('#server-rows tr').length,drawerNodes:document.querySelectorAll('#drawer-header *,#drawer-tabs *,#drawer-content *').length,drawerHidden:document.querySelector('#drawer').classList.contains('hidden'),retainedDetachedControls:desktopState.controls?.filter(item=>!item.isConnected).length||0,retainedDetachedJobSlots:[...(desktopState.jobSlots?.values()||[])].filter(item=>!item.isConnected).length};})()`);
  return {at:Date.now(),performance,dom,browser};
}
function subtract(after,before){const result={};for(const key of new Set([...Object.keys(before||{}),...Object.keys(after||{})])){const a=after?.[key]??0,b=before?.[key]??0;result[key]=typeof a==='object'?subtract(a,b):a-b;}return result;}
function summarize(before,after){
  const metrics=['TaskDuration','ScriptDuration','LayoutDuration','RecalcStyleDuration','LayoutCount','RecalcStyleCount'];
  return {wallMs:after.at-before.at,counts:subtract(after.browser.counts,before.browser.counts),work:Object.fromEntries(metrics.map(name=>[name,(after.performance[name]||0)-(before.performance[name]||0)])),heap:{before:before.performance.JSHeapUsedSize,after:after.performance.JSHeapUsedSize,delta:after.performance.JSHeapUsedSize-before.performance.JSHeapUsedSize},dom:{before:before.dom,after:after.dom,delta:subtract(after.dom,before.dom)},page:{before:{...before.browser,counts:undefined},after:{...after.browser,counts:undefined}}};
}
const report={target:new URL(targetURL).port==='8788'?'isolated fixture':'production read-only',createdAt:new Date().toISOString(),notes:['All state-changing fetches blocked; active jobs mocked inside isolated page only.','Real idle windows use wall-clock timers; simulated census is separately labelled.','GC checkpoints are bounded retention comparisons, not proof of an unbounded memory leak.'],startup:null,phases:[],errors};
async function phase(name,milliseconds,work=null,gc=false){
  console.log('Runtime phase:',name);const before=await snapshot(gc);
  if(work)await work();else await pause(milliseconds);
  const after=await snapshot(gc),entry={name,...summarize(before,after)};report.phases.push(entry);
  console.log(JSON.stringify({phase:name,wallMs:entry.wallMs,requests:entry.counts.requests,mutations:entry.counts.mutations.records,heapDelta:entry.heap.delta,domNodesDelta:entry.dom.delta.nodes}));
  await writeFile(output,JSON.stringify(report,null,2)+'\n');
}
try{
  const target=await call('Target.createTarget',{url:'about:blank'},null);session=(await call('Target.attachToTarget',{targetId:target.targetId,flatten:true},null)).sessionId;
  await call('Page.enable');await call('Runtime.enable');await call('Performance.enable');await call('HeapProfiler.enable');
  await call('Emulation.setDeviceMetricsOverride',{width:1440,height:1000,deviceScaleFactor:1,mobile:false});
  await call('Network.setUserAgentOverride',{userAgent:'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36'});
  await call('Page.addScriptToEvaluateOnNewDocument',{source:instrumentation});await call('Page.navigate',{url:targetURL});
  await wait('!!document.querySelector(".package-search") && !!state.csrf');
  report.startup=await snapshot(true);console.log('BASELINE PAGE LOADED — subsequent source edits do not affect this open page.');
  await phase('pc_idle_real_20s',20000);
  await evaluate('(async()=>{const p=window.__runtimeProfiler;p.status=await api("/api/desktop-updates/status");p.activeJob=true;await pollDesktop();})()');
  await phase('pc_mock_active_job_real_15s',15000);
  await evaluate('(async()=>{window.__runtimeProfiler.activeJob=false;await pollDesktop();await changeView("burp");})()');
  await phase('burp_idle_real_8s',8000);
  await evaluate('changeView("listeners")');
  await phase('listeners_idle_real_8s',8000);
  await phase('listener_entry_retention_after_gc',0,async()=>{},true);
  await evaluate('changeView("servers")');
  await phase('drawer_open_software_close_10_after_gc',0,async()=>{await evaluate('(async()=>{const id=state.servers[0].id;for(let i=0;i<10;i++){await openServer(id);state.tab="packages";renderTabs();renderDrawerContent();closeDrawer();}})()');},true);
  await evaluate('changeView("pc")');
  await phase('30_cycles_pc_servers_burp_after_gc',0,async()=>{await evaluate('(async()=>{for(let i=0;i<30;i++){await changeView("servers");await changeView("burp");await changeView("pc");}})()');},true);
  await phase('simulated_10_minute_pc_idle_poll_census',0,async()=>{await evaluate('(async()=>{const originalNow=Date.now;let clock=originalNow();Date.now=()=>clock;try{for(let i=0;i<10;i++){clock+=60000;await pollDesktop();await pollRentals();}}finally{Date.now=originalNow;}})()');});
  report.phases.at(-1).simulation='Manual calls of the two scheduled visible-PC polling paths at 10 synthetic one-minute timestamps; not a wall-clock CPU measurement.';
  report.final=await snapshot(true);assert.equal(report.final.browser.counts.blockedWrites,0,'No mutation was attempted');assert.deepEqual(errors,[],'No browser runtime errors');
  await writeFile(output,JSON.stringify(report,null,2)+'\n');console.log('Runtime aggregate:',output);
}catch(error){report.failure=error.message;await writeFile(output,JSON.stringify(report,null,2)+'\n');throw error;}
finally{
  try{await call('Browser.close',{},null);}catch{}
  chrome.kill();for(const request of pending.values())clearTimeout(request.timer);
  await new Promise(resolve=>chrome.exitCode===null?chrome.once('exit',resolve):resolve());
  await rm(profile,{recursive:true,force:true});
}

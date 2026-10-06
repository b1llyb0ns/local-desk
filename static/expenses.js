'use strict';
const expenseState={data:null,servers:new Map(),loadPromise:null,loading:false,error:'',signature:'',summarySignature:'',currencyBusy:false,rateBusy:false,editing:null,saving:false,deleting:null,deleteBusy:false};
const moneyFormatters=new Map(['USD','EUR','RUB'].map(currency=>[currency,new Intl.NumberFormat('en-GB',{style:'currency',currency,currencyDisplay:'code',minimumFractionDigits:2,maximumFractionDigits:2})]));
const costPeriods={monthly:'month',quarterly:'quarter',yearly:'year',once:'one-time'};
function money(value,currency=expenseState.data?.currency||'USD') {
  if(value===null||value===undefined||value===''||!Number.isFinite(Number(value)))return 'Not available';
  return (moneyFormatters.get(currency)||moneyFormatters.get('USD')).format(Number(value));
}
function acceptExpenses(data) {
  if(!data)return;
  expenseState.data=data;expenseState.servers=new Map((data.servers||[]).map(server=>[String(server.id),server]));
}
function costPeriod(period){return period==='once'?'one-time':'/ '+(costPeriods[period]||'month');}
function serverCostSignature(server) {
  const cost=expenseState.servers.get(String(server.id));
  return [server.cost_amount,server.cost_currency,server.cost_period,server.cost_review,cost?.converted_amount,expenseState.data?.currency,expenseState.data?.fx?.stale];
}
function serverCost(server) {
  const cost=expenseState.servers.get(String(server.id))||server,amount=cost.cost_amount,currency=cost.cost_currency,period=cost.cost_period||'monthly';
  const out=el('div','server-cost');
  if(amount===null||amount===undefined||!currency){
    out.append(el('span','muted','Cost not set'));
    if(server.price)out.append(el('small','cost-warning','Saved: '+server.price+' · not in totals'));
    return out;
  }
  const display=expenseState.data?.currency||currency,converted=cost.converted_amount;
  const original=money(amount,currency)+' '+costPeriod(period);
  if(currency===display||server.archived)out.append(el('span','',original));
  else if(converted!==null&&converted!==undefined){out.append(el('span','','≈ '+money(converted,display)+' '+costPeriod(period)));out.title=original;}
  else {out.append(el('span','',original),el('small','cost-warning','Conversion unavailable'));}
  if(cost.cost_review)out.append(el('small','cost-warning','Confirm billing period'));
  else if(currency!==display&&expenseState.data?.fx?.stale&&converted!=null)out.append(el('small','cost-warning','Cached rate'));
  return out;
}
function currencyControl(id) {
  const select=el('select','currency-select');select.id=id;select.setAttribute('aria-label','Display currency');
  for(const currency of ['USD','EUR','RUB']){const option=el('option','',currency);option.value=currency;select.append(option);}
  select.value=expenseState.data?.currency||'USD';select.disabled=expenseState.currencyBusy;
  select.addEventListener('change',safeAction(()=>setExpenseCurrency(select.value)));
  return append(el('label','display-currency'),el('span','','Display'),select);
}
function syncCurrencyControls() {for(const select of $$('.currency-select')){select.value=expenseState.data?.currency||'USD';select.disabled=expenseState.currencyBusy;}}
function expenseIssue(summary) {
  if(!summary)return 'Costs not available';
  const issues=[];
  if(summary.unknown_count)issues.push(summary.unknown_count+' cost'+(summary.unknown_count===1?'':'s')+' not set');
  if(summary.review_count)issues.push(summary.review_count+' billing period'+(summary.review_count===1?'':'s')+' to confirm');
  if([summary.monthly,summary.yearly,summary.once].some(value=>value===null)&&!summary.unknown_count)issues.push('Conversion unavailable');
  return issues.join(' · ');
}
function knownAmounts(summary,period) {
  if(!summary?.native)return '';
  return ['USD','EUR','RUB'].filter(currency=>Number(summary.native[currency]?.[period])!==0&&summary.native[currency]?.[period]!=null).map(currency=>money(summary.native[currency][period],currency)).join(' + ');
}
function knownCost(summary,period){return summary?.known?.[period]!=null?money(summary.known[period]):knownAmounts(summary,period);}
function summaryValue(value){return value===null||value===undefined?'Incomplete':money(value);}
function summaryAmount(summary,period){return summaryValue(summary?.[period]??summary?.known?.[period]);}
function renderServerCosts() {
  const root=$('#server-cost-summary');if(!root)return;
  root.classList.toggle('hidden',state.view!=='servers');if(state.view!=='servers'||document.hidden)return;
  const data=expenseState.data,summary=data?.totals?.servers,signature=JSON.stringify([summary,data?.currency,data?.fx?.date,data?.fx?.stale]);
  if(expenseState.summarySignature===signature)return;expenseState.summarySignature=signature;
  const text=append(el('div','server-cost-totals'),el('span','cost-summary-label','VPS cost'));
  if(summary){text.append(el('strong','',(summary.monthly==null?'Known: ':'')+summaryAmount(summary,'monthly')+' / month'),el('span','',summaryAmount(summary,'yearly')+' / year'));
    if(summary.once===null||Number(summary.once)!==0)text.append(el('span','',summaryValue(summary.once)+' one-time'));
    const issue=expenseIssue(summary),known=summary.monthly==null&&summary.known?.monthly==null?knownCost(summary,'monthly'):'';
    if(issue)text.append(el('small','cost-warning',issue+(known?' · Known: '+known+' / month':'')));
  }else text.append(el('span','muted','Costs not available'));
  root.replaceChildren(text,append(el('div','server-cost-actions'),currencyControl('server-display-currency'),button('Expenses →','text-button',safeAction(()=>changeView('expenses')))));
  syncCurrencyControls();
}
async function setExpenseCurrency(currency) {
  if(expenseState.currencyBusy)return;
  expenseState.currencyBusy=true;syncCurrencyControls();
  try {acceptExpenses(await api('/api/expenses/settings','PATCH',{currency}));expenseState.error='';renderServers();renderExpenses();}
  finally {expenseState.currencyBusy=false;syncCurrencyControls();}
}
function fxCaption(fx) {
  if(!fx?.usd_rub)return 'Conversion unavailable · Open Expenses to check the daily currency rates.';
  return 'Estimated conversion · 1 USD = '+money(fx.usd_rub,'RUB')+(fx.eur_rub?' · 1 EUR = '+money(fx.eur_rub,'RUB'):' · EUR rate unavailable')+' · CBR '+(fx.date||'date unavailable')+(fx.stale?' · Cached rate':'');
}
function renderExpenseHeader() {
  if(state.view!=='expenses')return;
  const root=$('#local-header-actions');
  if(!$('#add-expense'))root.replaceChildren(append(el('div','update-actions'),currencyControl('expense-display-currency'),append(button('','button primary',()=>openExpenseForm()),icon('plus'),el('span','','Add expense'))));
  const add=$('#local-header-actions .primary');if(add)add.id='add-expense';syncCurrencyControls();
}
function expenseSummary(title,summary,description) {
  const card=append(el('article','expense-summary-card'),el('span','',title),el('strong','',summaryAmount(summary,'monthly')),el('span','expense-normalized',summary?.monthly==null?'known costs per month':'per month'),el('small','',summaryAmount(summary,'yearly')+' per year'),el('p','',description));
  const issue=expenseIssue(summary);if(issue)card.append(el('small','cost-warning',issue));
  if(summary?.monthly==null&&summary?.known?.monthly==null){const known=knownCost(summary,'monthly');if(known)card.append(el('small','expense-known','Known: '+known+' / month'));}
  return card;
}
function expenseAmount(item,server=false) {
  const amount=server?item.cost_amount:item.amount,currency=server?item.cost_currency:item.currency,period=server?item.cost_period:item.period;
  const cell=el('div','expense-amount');
  if(amount===null||amount===undefined||!currency){cell.append(el('span','muted','Cost not set'));if(item.price)cell.append(el('small','', 'Saved: '+item.price));return cell;}
  cell.append(el('span','',money(amount,currency)+' '+costPeriod(period)));
  if(currency!==expenseState.data.currency)cell.append(el('small',item.converted_amount==null?'cost-warning':'',item.converted_amount==null?'Conversion unavailable':'≈ '+money(item.converted_amount)+' '+costPeriod(period)));
  if(item.cost_review)cell.append(el('small','cost-warning','Confirm billing period'));
  return cell;
}
function expensePaymentDate(item) {
  const value=item.next_charge_date,label=item.period==='once'?'Payment date':'Next payment';
  const out=el('div','expense-payment'),heading=append(el('div','expense-payment-heading'),el('span','expense-payment-label',label));out.append(heading);
  if(!value){heading.append(el('span','expense-payment-unset','Not set'));return out;}
  const date=el('time','rental-date',value);date.setAttribute('datetime',value);heading.append(date);
  const days=leaseDays({lease_end:value});if(days===null||!Number.isFinite(days))return out;
  out.classList.add('known');if(days<0)out.classList.add('overdue');else if(days<=3)out.classList.add('urgent');else if(days<=7)out.classList.add('soon');
  out.append(el('small','rental-countdown',days<0?`Saved date passed ${-days}d ago`:days===0?'Payment today':`In ${days} days`));return out;
}
function expenseTable(items,server=false) {
  const rows=items.map(item=>{
    const name=append(el('div','expense-name'),el('strong','',item.name));
    if(server)name.append(el('small','',item.alias));else {name.append(expensePaymentDate(item));if(item.notes)name.append(el('small','expense-notes',item.notes));}
    const period=server?item.cost_period:item.period;
    const normalized=period==='once'?el('span','muted','One-time'):append(el('div','expense-normalized'),el('span','',item.monthly==null?'Incomplete':money(item.monthly)+' / month'),el('small','',item.yearly==null?'Incomplete':money(item.yearly)+' / year'));
    const actions=el('div','expense-actions');
    if(server)actions.append(button('Edit cost','button secondary',safeAction(async()=>{const {server:detail}=await api('/api/servers/'+item.id);openForm('edit',detail);field('cost_amount').focus();})));
    else actions.append(button('Edit','button secondary',()=>openExpenseForm(item)),iconButton('trash','Delete expense: '+item.name,()=>openExpenseDelete(item)));
    const row=[name,expenseAmount(item,server),normalized,actions];return row;
  });
  const listing=table(['Name','Billing amount','In '+expenseState.data.currency,'Actions'],rows);listing.classList.add('expense-table-wrap');
  $$('tbody tr',listing).forEach((row,index)=>{row.dataset[server?'expenseServerId':'expenseId']=items[index].id;});
  return listing;
}
function renderExpenses() {
  if(state.view!=='expenses'||document.hidden)return;
  renderExpenseHeader();
  const data=expenseState.data,signature=JSON.stringify([data,state.rentals?.today,expenseState.error,expenseState.loading,expenseState.rateBusy]);
  if(expenseState.signature===signature)return;expenseState.signature=signature;
  const root=$('#expenses-section');root.replaceChildren();
  if(!data){root.append(el('div','empty',expenseState.error||'Loading expenses…'));if(expenseState.error)root.append(button('Try again','button secondary',safeAction(loadExpenses)));return;}
  if(expenseState.error)root.append(el('p','form-error',expenseState.error));
  root.append(append(el('div','expense-overview'),expenseSummary('All recurring',data.totals.all,'Active VPS + your expenses'),expenseSummary('VPS',data.totals.servers,data.servers.length+' active server'+(data.servers.length===1?'':'s')),expenseSummary('Other expenses',data.totals.items,data.items.length+' saved item'+(data.items.length===1?'':'s'))));
  const once=append(el('div','expense-once'),el('span','','One-time expenses'),el('strong','',summaryValue(data.totals.all.once)),el('small','','Separate from recurring totals'));
  if(data.totals.all.once==null){const known=knownCost(data.totals.all,'once');if(known)once.append(el('small','cost-warning','Known: '+known));}root.append(once);
  const rates=el('div','expense-rates');
  const caption=append(el('div'),el('p','',fxCaption(data.fx)));
  if(data.fx?.usd_rub)caption.append(el('small','','Daily reference rate; your provider may charge a different rate. '),link('CBR source ↗','https://www.cbr.ru/scripts/XML_daily.asp','text-button'));
  if(data.fx?.checked_at)caption.append(el('small','expense-rate-checked','Last checked: '+dateTime(data.fx.checked_at)));
  if(data.fx?.error)caption.append(el('p','cost-warning','Rate update failed: '+data.fx.error));
  if(expenseState.loading)caption.append(el('small','','Checking today’s rate…'));
  const refresh=button(expenseState.rateBusy?'Updating…':'Refresh rate','button secondary',safeAction(refreshExpenseRates));refresh.id='refresh-expense-rate';refresh.disabled=expenseState.rateBusy||expenseState.loading;rates.append(caption,refresh);root.append(rates);
  const servers=append(el('section','expense-panel'),append(el('div','update-panel-heading'),el('h2','','VPS costs'),el('span','muted','Active servers only')));
  servers.append(data.servers.length?expenseTable(data.servers,true):el('p','expense-empty','No active servers.'));root.append(servers);
  const items=append(el('section','expense-panel'),append(el('div','update-panel-heading'),el('h2','','Subscriptions & other expenses')));
  items.append(data.items.length?expenseTable(data.items):el('p','expense-empty','Add your subscriptions and other expenses with the amount you actually pay.'));root.append(items);
  root.append(el('p','expense-caption','Monthly, quarterly and yearly charges are normalized to monthly and yearly totals. One-time expenses stay separate.'));
}
function releaseExpenses(){expenseState.signature='';$('#expenses-section')?.replaceChildren();}
function loadExpenses({quiet=false}={}) {
  if(expenseState.loadPromise)return expenseState.loadPromise;
  expenseState.loading=!quiet;expenseState.error='';renderExpenses();
  expenseState.loadPromise=(async()=>{try{acceptExpenses(await api('/api/expenses'));renderServers();}catch(error){expenseState.error=error.message;}finally{expenseState.loading=false;expenseState.loadPromise=null;renderExpenses();}})();
  return expenseState.loadPromise;
}
async function refreshExpenseRates() {
  if(expenseState.rateBusy||expenseState.loading)return;
  expenseState.rateBusy=true;expenseState.error='';renderExpenses();
  try{acceptExpenses(await api('/api/expenses/rates/refresh','POST',{}));renderServers();}
  catch(error){expenseState.error=error.message;}
  finally{expenseState.rateBusy=false;renderExpenses();}
}
function expenseField(name){return $('#expense-form').elements.namedItem(name);}
function syncExpenseDateLabel() {$('#expense-date-label').textContent=expenseField('period').value==='once'?'Payment date':'Next payment';}
function openExpenseForm(item=null) {
  if(expenseState.saving)return;
  expenseState.editing=item?.id||null;$('#expense-form').reset();
  $('#expense-form-title').textContent=item?'Edit expense':'Add expense';$('#expense-save').textContent=item?'Save changes':'Add expense';
  for(const name of ['name','amount','notes','next_charge_date'])expenseField(name).value=item?.[name]??'';
  expenseField('currency').value=item?.currency||expenseState.data?.currency||'USD';expenseField('period').value=item?.period||'monthly';
  syncExpenseDateLabel();$('#expense-form-error').classList.add('hidden');$('#expense-modal').showModal();expenseField('name').focus();
}
function expenseFormBusy(busy) {expenseState.saving=busy;$('#expense-save').disabled=busy;$$('[data-close="expense-modal"]').forEach(item=>item.disabled=busy);for(const name of ['name','amount','currency','period','notes','next_charge_date'])expenseField(name).disabled=busy;}
async function submitExpense(event) {
  event.preventDefault();if(expenseState.saving)return;
  const id=expenseState.editing,values={};for(const name of ['name','amount','currency','period','notes'])values[name]=expenseField(name).value;
  values.next_charge_date=expenseField('next_charge_date').value||null;
  expenseFormBusy(true);$('#expense-form-error').classList.add('hidden');
  try {acceptExpenses(await api('/api/expenses'+(id?'/'+id:''),id?'PATCH':'POST',values));$('#expense-modal').close();renderExpenses();renderServerCosts();toast(id?'Expense saved.':'Expense added.');}
  catch(error){$('#expense-form-error').textContent=error.message;$('#expense-form-error').classList.remove('hidden');}
  finally{expenseFormBusy(false);}
}
function openExpenseDelete(item) {
  if(expenseState.deleteBusy)return;
  expenseState.deleting={id:item.id,name:item.name};$('#expense-delete-name').textContent=item.name;$('#expense-delete-error').classList.add('hidden');$('#expense-delete-modal').showModal();$('#expense-delete-modal [data-close]').focus();
}
async function submitExpenseDelete(event) {
  event.preventDefault();if(expenseState.deleteBusy||!expenseState.deleting)return;
  const id=expenseState.deleting.id;expenseState.deleteBusy=true;$('#expense-delete-submit').disabled=true;$$('[data-close="expense-delete-modal"]').forEach(item=>item.disabled=true);$('#expense-delete-error').classList.add('hidden');
  try{acceptExpenses(await api('/api/expenses/'+id,'DELETE',{}));$('#expense-delete-modal').close();renderExpenses();toast('Expense deleted.');}
  catch(error){$('#expense-delete-error').textContent=error.message;$('#expense-delete-error').classList.remove('hidden');}
  finally{expenseState.deleteBusy=false;$('#expense-delete-submit').disabled=false;$$('[data-close="expense-delete-modal"]').forEach(item=>item.disabled=false);}
}
function initExpenses() {
  $('#expense-form').addEventListener('submit',submitExpense);$('#expense-delete-form').addEventListener('submit',submitExpenseDelete);
  expenseField('period').addEventListener('change',syncExpenseDateLabel);
  $('#expense-modal').addEventListener('cancel',event=>{if(expenseState.saving)event.preventDefault();});
  $('#expense-modal').addEventListener('close',()=>{if($('#expense-modal').open)return;expenseState.editing=null;$('#expense-form').reset();$('#expense-form-error').textContent='';});
  $('#expense-delete-modal').addEventListener('cancel',event=>{if(expenseState.deleteBusy)event.preventDefault();});
  $('#expense-delete-modal').addEventListener('close',()=>{if($('#expense-delete-modal').open)return;expenseState.deleting=null;$('#expense-delete-name').textContent='';$('#expense-delete-error').textContent='';});
}

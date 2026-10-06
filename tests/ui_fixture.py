"""Isolated UI fixture. Never imports real hosts and never executes SSH."""
import json
import datetime as dt
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server
from server import Store, Application, make_server
from test_panel import FakeEngine, SNAPSHOT, LOCAL_LISTENERS

listener_inventory = json.loads(json.dumps(LOCAL_LISTENERS))
server.local_listener_snapshot = lambda: json.loads(json.dumps(listener_inventory))

fixture_directory=Path(__file__).resolve().parents[1]/'tmp'
fixture_directory.mkdir(exist_ok=True,mode=0o700)
with tempfile.TemporaryDirectory(prefix='ui-fixture-',dir=fixture_directory) as temporary:
    store=Store(Path(temporary)/'data',Path(temporary),import_existing=False,export=False)
    for i,alias in enumerate(['server-a','server-b','server-c','server-d','server-e','server-f','server-g','server-h']):
        row=store.add({'alias':alias,'name':alias,'host':f'192.0.2.{i+1}','provider':['Provider A','Provider A','Provider B','Provider C','Provider A','Provider D','Provider C','Unknown'][i],
                       'provider_url':'https://example.com/hosting' if i<7 else '', 'lease_hint':['01-10','02-10','03-10','04-10','05-10','06-10','07-10',''][i],
                       'price':['900 ₽','10$','600 ₽','','300 ₽','600 ₽','',''][i]})
        if i not in [4,5,7]:store.snapshot(row['id'],SNAPSHOT)
        else:store.failure(row['id'],Exception('Test: access required'))
        if i == 1:store.update(row['id'], {'notes': 'Example server notes.\nReview the renewal date.'})
    app=Application(store,FakeEngine());app.rentals.sender=lambda *args: None;app.rentals.available=True
    app.expenses.fetcher=lambda: {'usd_rub':'90','date':dt.datetime.now(dt.timezone.utc).date().isoformat()}
    app.expenses.refresh()
    checked='2026-09-05T12:00:00+00:00'
    apt={'id':'apt','name':'APT','state':'ok','error':'','actions':['check','refresh-lists','upgrade'],
         'count':2,'security_count':1,'installed_count':1200,'checked_at':checked,'updates_checked_at':checked,
         'last_refresh_at':'2026-09-04T10:00:00+00:00','last_refresh_source':'APT periodic success stamp',
         'last_upgrade_at':'2026-09-03T09:00:00+00:00','last_upgrade_source':'Completed APT history transaction',
         'list_timestamp':'2026-09-05T11:00:00+00:00',
         'packages':[{'name':'openssl','installed':'1.0','candidate':'1.1','security':True,'trusted':True,'held':False},
                     {'name':'warp-terminal','installed':'1.0','candidate':'1.2','security':False,'trusted':True,'held':False}]}
    snap={'id':'snap','name':'Snap','state':'ok','error':'','actions':['check','upgrade'],
          'count':1,'security_count':0,'installed_count':8,'checked_at':checked,'updates_checked_at':checked,
          'last_upgrade_at':'2026-09-04T08:00:00+00:00','last_upgrade_source':'Completed snapd refresh change',
          'last_auto_refresh_at':'2026-09-04T08:00:00+00:00','next_auto_refresh_at':'2026-09-05T14:00:00+00:00',
          'packages':[{'name':'firefox','installed':'128.0','candidate':'129.0','security':False,'trusted':True,'held':False}]}
    def tool_row(name,installed,candidate,path,source='Go module',updatable=True,reason='',unknown=False):
        return {'id':name,'name':name,'installed':installed,'candidate':candidate,'path':path,'source':source,
                'updatable':updatable,'reason':reason,'version_unknown':unknown,'updates_checked_at':checked}
    def tool_manager(manager,name,rows):
        return {'id':manager,'name':name,'state':'ok','error':'','actions':['check'],'packages':rows,
                'installed_packages':rows,'installed_count':len(rows),'count':sum(bool(row['candidate']) for row in rows),
                'checked_at':checked,'updates_checked_at':checked,'last_upgrade_at':None}
    go=tool_manager('go','Go toolchain',[
        tool_row('go','go1.27.0','go1.27.1','/usr/local/go','go.dev')])
    pdtm=tool_manager('pdtm','ProjectDiscovery',[
        tool_row('httpx','1.10.0','v1.10.1','/home/operator/.pdtm/go/bin/httpx','PDTM directory'),
        tool_row('nuclei','Unknown','v3.5.0','/home/operator/.pdtm/go/bin/nuclei','PDTM directory',
                 reason='Installed release cannot be verified. Installing stable may replace a newer or custom build; a backup is retained.',unknown=True),
        tool_row('pdtm','v0.1.5','v0.1.6','/home/operator/go/bin/pdtm')])
    go_tools=tool_manager('go-tools','Go tools',[
        tool_row('katana','v1.7.0','v1.7.1','/home/operator/go/bin/katana'),
        tool_row('ffuf','v2.0.0-20260829133750-0f654620eef6',None,'/home/operator/go/bin/ffuf',updatable=False,
                 reason='Development or custom version; automatic replacement is disabled.'),
        tool_row('gau','v2.2.4','v2.2.5','/home/operator/go/bin/gau')])
    pipx=tool_manager('pipx','pipx',[
        tool_row('sqlmap','1.10.8','1.10.9','/home/operator/.local/share/pipx/venvs/sqlmap','PyPI'),
        tool_row('ai-ffuf','0.8.1',None,'/home/operator/.local/share/pipx/venvs/ai-ffuf','pipx',False,
                 'Local, editable, custom-index, or custom-source install; preserved unchanged.'),
        tool_row('penelope-shell-handler','0.21.0',None,'/home/operator/.local/share/pipx/venvs/penelope-shell-handler','pipx',False,
                 'Version-pinned package; its pin will not be changed automatically.')])
    tool_records={record['id']:record for record in [go,pdtm,go_tools,pipx]}
    app.desktop.set_setting('pc_inventory',{**apt,'manager':'APT','managers':[apt,snap,go,pdtm,go_tools,pipx]})
    app.desktop.set_setting('burp_release',{'version':'2026.8','sha256':'a'*64,'release_url':'https://portswigger.net/burp/downloads','checked_at':'2026-09-05T12:00:00+00:00'})
    app.desktop.detect_burp=lambda: {'launcher':'/test/burpsuite.desktop','jar':'/test/burpsuite_desktop_v2026.7.3.jar','version':'2026.7.3','custom_launcher':False,'installed':True}
    app.desktop.running_burp=lambda: []
    def fake_pc(operation='check'):
        app.desktop.jobs['pc']={'operation':operation,'manager':'apt','state':'done','message':'Test package action: '+operation}
    def fake_manager_check(manager):
        if manager=='apt':return fake_pc('check')
        app.desktop.jobs['pc']={'operation':'check-'+manager,'manager':manager,'state':'done','message':{'snap':'Snap','flatpak':'Flatpak'}[manager]+' check complete'}
    def fake_manager_launch(manager,operation):
        if manager=='apt':return fake_pc(operation)
        app.desktop.jobs['pc']={'operation':operation,'manager':manager,'state':'done','message':{'snap':'Snap','flatpak':'Flatpak'}[manager]+' update complete'}
    def fake_burp():
        app.desktop.jobs['burp']={'operation':'check','state':'done','message':'Test release checked'}
    def fake_install(version,preserve_launch):
        if preserve_launch is not True:raise RuntimeError('Update confirmation required')
        app.desktop.jobs['burp']={'operation':'install','state':'done','message':'Test official install: '+version}
    def fake_tools_check(manager):
        if manager not in tool_records:raise RuntimeError('Unsupported tool manager')
        app.desktop.jobs['pc']={'operation':'check-'+manager,'manager':manager,'state':'done',
                                'message':tool_records[manager]['name']+' check complete'}
    def fake_tool_launch(manager,item_id,version,allow_unknown=False):
        if manager not in tool_records or type(allow_unknown) is not bool:raise RuntimeError('Invalid tool request')
        item=next((row for row in tool_records[manager]['packages'] if row['id']==item_id),None)
        if not item or not item['updatable'] or not item['candidate'] or item['candidate']!=version:
            raise RuntimeError('Select an available individual tool update')
        if bool(item['version_unknown'])!=allow_unknown:raise RuntimeError('Unknown-version confirmation does not match')
        app.desktop.jobs['pc']={'operation':'upgrade','manager':manager,'item':item_id,'version':version,'state':'done',
                                'message':'Tool action: '+manager+'/'+item_id+' '+version+' · unknown='+str(allow_unknown).lower()}
    app.desktop.launch_apt=fake_pc;app.desktop.check_pc=fake_pc;app.desktop.check_burp=fake_burp;app.desktop.install_burp=fake_install
    app.desktop.check_manager=fake_manager_check;app.desktop.launch_manager=fake_manager_launch
    app.desktop.check_tools=fake_tools_check;app.desktop.launch_tool=fake_tool_launch
    http=make_server(app,8788)
    print('UI fixture ready on 127.0.0.1:8788',flush=True)
    try:http.serve_forever()
    except KeyboardInterrupt:pass
    finally:http.server_close();app.pool.shutdown(wait=True);store.db.close()

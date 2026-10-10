"""Group selection, coverage-preserving migration and catalogue adoption."""
from copy import deepcopy
from html.parser import HTMLParser
import json
import pytest
from fastapi.testclient import TestClient
from app.db import Database
from app.notice_selection import *
from app.web.notice_routes import router
from test_troubleshooting import environment, seed, rows

CASES=[(s,d,label) for s,ds in LABELS.items() for d,labels in ds.items() for label in labels]

def grouped(service, mode="selected"):
    p=grouped_policy(service,proposal(service,{}))
    for c in p['dimensions'].values():
        c['special']=[]
        for g in c['groups'].values():g.update(mode=mode,selected=[])
    return p

def settings(service,p):return {service+'_notice_selection':p,'traffic_types':['incident','roadwork','regional']}

@pytest.mark.parametrize('service,dim,label',CASES)
def test_reviewed_membership_and_independent_all_group(service,dim,label):
    p=grouped(service);entry=classify(service,dim,label,p['catalogue']);gid=entry['group']
    assert gid in p['dimensions'][dim]['groups']
    p['dimensions'][dim]['groups'][gid]['mode']='all'
    # Other dimensions independently match, preserving AND semantics.
    for other,c in p['dimensions'].items():
        if other!=dim:
            for g in c['groups'].values():g['mode']='all'
    assert evaluate(service,{dim:label,'feed':'incident'},settings(service,p)).included == (len(p['dimensions'])==1)
    if len(p['dimensions'])>1:
        values={d:LABELS[service][d][0] for d in p['dimensions']}|{dim:label,'feed':'incident'}
        assert evaluate(service,values,settings(service,p)).included
    for raw in ('Future provider value',''):
        assert not evaluate(service,{dim:raw,'feed':'incident'},settings(service,p)).included

@pytest.mark.parametrize('service',list(LABELS))
@pytest.mark.parametrize('mode',['all','selected'])
def test_migration_preserves_known_unknown_missing_and_saved_choices(service,mode):
    old=proposal(service,{})
    for d,c in old['dimensions'].items():c.update(mode=mode,selected=[identifier(LABELS[service][d][0]),UNKNOWN])
    new=grouped_policy(service,old);validate(service,new)
    assert grouped_policy(service,new)==new
    assert new['catalogue']['version']==old['catalogue']['version']
    for dim in old['dimensions']:
        for raw in LABELS[service][dim]+['Unknown new value','']:
            values={d:LABELS[service][d][0] for d in old['dimensions']}|{dim:raw,'feed':'incident'}
            assert evaluate(service,values,settings(service,old)).included==evaluate(service,values,settings(service,new)).included
        preserved=[x for g in new['dimensions'][dim]['groups'].values() for x in g['selected']]
        assert identifier(LABELS[service][dim][0]) in preserved
        assert new['dimensions'][dim]['special']==([UNKNOWN,MISSING] if mode=='all' else [UNKNOWN])

@pytest.mark.parametrize('special,raw',[(UNKNOWN,'Unknown Warning'),(MISSING,'')])
def test_special_is_independent_of_all_ordinary_groups(special,raw):
    p=grouped('bom','all');s=settings('bom',p)
    assert not evaluate('bom',{'type':raw},s).included
    p['dimensions']['type']['special']=[special]
    assert evaluate('bom',{'type':raw},s).included
    other=MISSING if special==UNKNOWN else UNKNOWN
    assert not evaluate('bom',{'type':'' if other==MISSING else 'Another unknown'},s).included


def test_invalid_membership_fails_closed_even_for_all_groups():
    p=grouped('traffic','all')
    next(e for e in p['catalogue']['dimensions']['category'] if e['id']=='crash')['group']='invalid'
    with pytest.raises(ValueError):validate('traffic',p)
    result=evaluate('traffic',{'category':'CRASH','feed':'incident'},settings('traffic',p))
    assert not result.included and 'invalid group membership' in result.reason


def test_group_validation_rejects_cross_group_and_special_ids():
    for value in ('crash',UNKNOWN):
        p=grouped('traffic');p['dimensions']['category']['groups']['fire']['selected']=[value]
        with pytest.raises(ValueError):validate('traffic',p)


def test_adoption_handles_additions_moves_new_groups_and_unknown_reclassification(monkeypatch):
    import app.notice_selection as module
    p=grouped('traffic');p['dimensions']['category']['special']=[UNKNOWN]
    p['dimensions']['category']['groups']['fire']['mode']='all'
    latest=catalogue('traffic');latest['version']=3
    latest['dimensions']['category'].append({'id':'new-fire','label':'NEW FIRE','group':'fire','aliases':[]})
    latest['groups']['category'].append({'id':'new-group','label':'New group'})
    latest['dimensions']['category'].append({'id':'new-item','label':'NEW ITEM','group':'new-group','aliases':[]})
    next(e for e in latest['dimensions']['category'] if e['id']=='grass-fire')['group']='other'
    monkeypatch.setattr(module,'catalogue',lambda service:deepcopy(latest))
    assert evaluate('traffic',{'category':'NEW FIRE','feed':'incident'},settings('traffic',p)).included # still Unrecognized
    adopted=adopt_catalogue('traffic',p);validate('traffic',adopted)
    assert adopted['dimensions']['category']['groups']['new-group']['mode']=='selected'
    assert evaluate('traffic',{'category':'NEW FIRE','feed':'incident'},settings('traffic',adopted)).included
    assert not evaluate('traffic',{'category':'NEW ITEM','feed':'incident'},settings('traffic',adopted)).included
    assert not evaluate('traffic',{'category':'GRASS FIRE','feed':'incident'},settings('traffic',adopted)).included
    assert evaluate('traffic',{'category':'GRASS FIRE','feed':'incident'},settings('traffic',p)).included


def test_startup_migration_is_atomic_idempotent_and_retains_backup(tmp_path):
    path=str(tmp_path/'migration.db');db=Database(path)
    old=proposal('traffic',{});db.set_setting('traffic_notice_selection',old)
    db.add_service_history('traffic','one','Example','',transmit_status='success')
    with db._conn:
        db._conn.execute("UPDATE service_history SET transmitted_at=ts WHERE id=1")
    history=rows(db,'SELECT * FROM service_history');db.close()
    db=Database(path)
    migrated=db.get_setting('traffic_notice_selection');assert migrated['schema_version']==2
    assert db.get_setting('traffic_notice_selection_before_groups')==old
    assert rows(db,'SELECT * FROM service_history')==history
    db.close();db=Database(path)
    assert db.get_setting('traffic_notice_selection')==migrated
    assert db.get_setting('traffic_notice_selection_before_groups')==old;db.close()


def form_for(p,action='preview'):
    f={'action':action,'catalogue':'active'}
    for dim,c in p['dimensions'].items():
        f[dim+'_special']=c['special'];f[dim+'_special_submitted']='1'
        for gid,g in c['groups'].items():
            prefix=dim+'_'+gid;f[prefix+'_mode']=g['mode']
            if g['mode']=='selected':f[prefix+'_selected']=g['selected']
    return f

@pytest.mark.parametrize('service',list(LABELS))
def test_group_ui_special_remains_enabled_preview_is_read_only_and_save_preserves_choices(environment,service):
    app=environment;app.include_router(router);db=app.state.db;seed(app,service)
    p=grouped(service,'all');dim=next(iter(p['dimensions']));gid=next(iter(p['dimensions'][dim]['groups']))
    member=next(e['id'] for e in p['catalogue']['dimensions'][dim] if e['group']==gid)
    p['dimensions'][dim]['groups'][gid]['selected']=[member];p['dimensions'][dim]['special']=[UNKNOWN]
    db.set_setting(service+'_notice_selection',p);before=db.all_settings();history=rows(db,'SELECT * FROM service_history')
    client=TestClient(app);form=form_for(p);response=client.post('/settings/notices/'+service,data=form)
    assert response.status_code==200
    class Parser(HTMLParser):
        def __init__(self):super().__init__();self.inputs=[]
        def handle_starttag(self,tag,attrs):
            if tag in ('input','select'):self.inputs.append(dict(attrs))
    parsed=Parser();parsed.feed(response.text)
    special=[x for x in parsed.inputs if x.get('name')==dim+'_special']
    assert len(special)==2 and all('disabled' not in x for x in special)
    boxes=[x for x in parsed.inputs if x.get('name')==dim+'_'+gid+'_selected']
    assert boxes and all('disabled' in x for x in boxes)
    assert any(x.get('value')==member and 'checked' in x for x in boxes)
    assert db.all_settings()==before and rows(db,'SELECT * FROM service_history')==history and not app.state.tx.sent
    diagnostics=client.get('/troubleshoot/notice-selection').json()[service]
    assert diagnostics['selection']['schema_version']==2
    assert diagnostics['selection']['dimensions'][dim]['special']==[UNKNOWN]
    assert diagnostics['applied'] is True
    form['action']='save';client.post('/settings/notices/'+service,data=form,follow_redirects=False)
    assert db.get_setting(service+'_notice_selection')['dimensions'][dim]['groups'][gid]['selected']==[member]
    form[dim+'_'+gid+'_choices_submitted']='1'
    client.post('/settings/notices/'+service,data=form,follow_redirects=False)
    assert db.get_setting(service+'_notice_selection')['dimensions'][dim]['groups'][gid]['selected']==[]

@pytest.mark.parametrize('source',['bom','rfs','traffic'])
@pytest.mark.asyncio
async def test_grouped_policies_apply_in_replay_and_queue_guard(environment,source):
    import asyncio
    app=environment;item=seed(app,source);db=app.state.db
    p=grouped(source,'all')
    db.set_setting(source+'_notice_selection',p)
    preview=await app.state.troubleshooting.preview(source,'resend');assert preview['notices']==1
    app.state.tx.pause=True;app.state.troubleshooting.start(preview['token'])
    for _ in range(100):
        if app.state.tx.pending:break
        await asyncio.sleep(.01)
    parts,callback,guard=app.state.tx.pending[0];assert guard()
    for c in p['dimensions'].values():
        for g in c['groups'].values():g['mode']='selected'
    db.set_setting(source+'_notice_selection',p);assert not guard()
    for i in range(len(parts)):callback(i,False,'Selection changed')
    await app.state.troubleshooting.task

@pytest.mark.asyncio
@pytest.mark.parametrize('source',['bom','rfs','traffic'])
async def test_group_all_does_not_send_unknown_until_special_is_checked(environment,source):
    from dataclasses import replace
    from app.filters import FilterRules
    app=environment;item=seed(app,source);db=app.state.db;p=grouped(source,'all')
    dim=next(iter(p['dimensions']));raw='New provider classification'
    if source=='bom':item=dict(item,event=raw)
    elif source=='rfs':item=replace(item,level=raw)
    else:item=replace(item,category=raw,raw_category=raw)
    db.set_setting(source+'_notice_selection',p)
    async def process():
        if source=='bom':
            s=db.all_settings();await app.state.poller._process(dict(item),FilterRules.from_settings(s),'Australia/Sydney',0,False,s)
        else:
            class Client:
                async def fetch(self):return [item]
                async def boundaries(self):return []
            poller=app.state.rfs_poller if source=='rfs' else app.state.traffic_poller
            poller.client=Client();await poller.poll_once()
    await process();assert not app.state.tx.sent
    p['dimensions'][dim]['special']=[UNKNOWN];db.set_setting(source+'_notice_selection',p)
    await process();assert len(app.state.tx.sent)==1
    evidence=json.loads(rows(db,'SELECT * FROM service_history ORDER BY id DESC LIMIT 1')[0]['metadata'])['notice_selection']
    assert evidence['schema_version']==2 and evidence['dimensions'][dim]['group_label']=='Special'

@pytest.mark.asyncio
@pytest.mark.parametrize('source',['bom','rfs','traffic'])
async def test_grouped_selection_retains_previously_transmitted_terminal_exception(environment,source):
    from dataclasses import replace
    from app.geography import configuration,KEY
    from app.filters import FilterRules
    app=environment;item=seed(app,source);db=app.state.db
    db.set_setting(KEY,configuration(all_nsw=True))
    if source=='bom':
        s=db.all_settings();await app.state.poller._process(dict(item),FilterRules.from_settings(s),'Australia/Sydney',0,False,s)
        app.state.tx.sent.clear();item=dict(item,message_type='Cancel')
    elif source=='rfs':
        db.rfs_add_history(item,'sent','success');item=replace(item,status='Out')
    else:
        db.add_service_history('traffic',item.item_id,'Previously sent','',transmit_status='success');item=replace(item,ended=True)
    db.set_setting(source+'_notice_selection',grouped(source))
    if source=='bom':
        s=db.all_settings();await app.state.poller._process(item,FilterRules.from_settings(s),'Australia/Sydney',0,False,s)
    else:
        poller=app.state.rfs_poller if source=='rfs' else app.state.traffic_poller
        await poller.poll_once(replay_items=[item])
    assert len(app.state.tx.sent)==1


def test_catalogue_adoption_preview_reports_membership_changes_without_saving(environment,monkeypatch):
    import app.notice_selection as module
    import app.web.notice_routes as routes
    app=environment;app.include_router(router);db=app.state.db;p=grouped('traffic','all')
    db.set_setting('traffic_notice_selection',p)
    latest=catalogue('traffic');latest['version']=3
    next(e for e in latest['dimensions']['category'] if e['id']=='grass-fire')['group']='other'
    latest['groups']['category'].append({'id':'new-group','label':'New group'})
    latest['dimensions']['category'].append({'id':'new-value','label':'NEW VALUE','group':'new-group','aliases':[],'family':'New group','description':'Example'})
    monkeypatch.setattr(module,'catalogue',lambda s:deepcopy(latest));monkeypatch.setattr(routes,'catalogue',lambda s:deepcopy(latest))
    form=form_for(p);form['catalogue']='latest'
    response=TestClient(app).post('/settings/notices/traffic',data=form)
    assert response.status_code==200
    assert 'GRASS FIRE moves from fire to other' in response.text
    assert 'new group New group starts unselected' in response.text
    assert 'new classification NEW VALUE' in response.text
    assert db.get_setting('traffic_notice_selection')==p and not app.state.tx.sent



def test_migration_failure_rolls_back_all_services_and_backups():
    db=Database(':memory:');bom=proposal('bom',{});rfs=proposal('rfs',{})
    rfs['dimensions']['level']['mode']='invalid'
    db.set_setting('bom_notice_selection',bom);db.set_setting('rfs_notice_selection',rfs)
    with pytest.raises(ValueError):db._migrate_notice_groups()
    assert db.get_setting('bom_notice_selection')==bom
    assert db.get_setting('bom_notice_selection_before_groups') is None
    db.close()


@pytest.mark.parametrize('change',['mode','membership','special','unknown-group','missing-mode'])
def test_invalid_group_submission_keeps_saved_policy(environment,change):
    app=environment;app.include_router(router);db=app.state.db;p=grouped('traffic');db.set_setting('traffic_notice_selection',p)
    f=form_for(p,'save')
    if change=='mode':f['category_fire_mode']='invalid'
    elif change=='membership':f['category_fire_selected']=['crash']
    elif change=='special':f['category_special']=['crash']
    elif change=='unknown-group':f['category_invented_mode']='all'
    else:f.pop('category_fire_mode')
    response=TestClient(app).post('/settings/notices/traffic',data=f)
    assert response.status_code==422
    assert db.get_setting('traffic_notice_selection')==p and not app.state.tx.sent


def test_traffic_feed_policy_preview_and_explicit_adoption(environment):
    from app.traffic.web import router as traffic_router
    app=environment;app.include_router(router);app.include_router(traffic_router);db=app.state.db;seed(app,'traffic')
    p=grouped('traffic','all');db.set_setting('traffic_notice_selection',p)
    db.set_setting('traffic_types',[])
    client=TestClient(app);before=db.all_settings()
    f=form_for(p);f['traffic_feed_mode']='categories'
    response=client.post('/settings/notices/traffic',data=f)
    assert response.status_code==200
    assert 'Excluded → Eligible' in response.text
    assert db.all_settings()==before and not app.state.tx.sent
    f['action']='save'
    assert client.post('/settings/notices/traffic',data=f,follow_redirects=False).status_code==303
    assert db.get_setting('traffic_notice_selection')['traffic_feed_mode']=='categories'
    assert db.get_setting('traffic_types')==[] and not app.state.tx.sent
    assert client.post('/settings/traffic',data={'preserve_notice_selection':'1','traffic_enabled':'1','traffic_poll_minutes':'10'},follow_redirects=False).status_code==303
    assert db.get_setting('traffic_types')==[]
    page=client.get('/settings/traffic').text
    assert 'name="traffic_types"' not in page
    assert 'Categories control transmission across all feeds' in page

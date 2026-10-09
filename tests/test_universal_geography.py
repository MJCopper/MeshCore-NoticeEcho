"""Universal coverage, read-only migration, and delivery integration."""
import json
from dataclasses import replace
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.geography import KEY, configuration, evaluate, contains, proposal, bom_coverage
from app.bom_area import CouncilMatch
from app.bom_enricher import WarningSection
from app.models import Alert
from app.db import Database
from app.poller import BomPoller
from app.filters import FilterRules
from app.rfs.feed import Incident
from app.rfs.poller import RFSPoller
from app.traffic.feed import TrafficItem
from app.traffic.poller import TrafficPoller
from app.troubleshooting import Troubleshooting
from app.web.geography_routes import router


@pytest.mark.parametrize('councils,field,included', [
    (('Tamworth Regional',),'Elsewhere',True),
    (('Central Coast',),'Moonbi',True),
    (('Tamworth Regional',),'Moonbi',True),
    (('Central Coast',),'Moonbiville',False),
    ((),'Moonbi',True),
])
def test_or_matrix(councils,field,included):
    policy=configuration(councils=['Tamworth'],location_terms=['Moonbi'])
    result=evaluate(policy,'rfs',councils,(('incident location',field),))
    assert result.included is included
    if field=='Moonbi':
        assert result.term_matches==(('Moonbi','incident location'),)


@pytest.mark.parametrize('text,term,expected',[
    ('<b>NEW ENGLAND</b>—Highway','New England Highway',True),
    ('Werris   Creek Rd','werris creek',True),
    ('Hunterville','Hunter',False),
    ('Moonbi &amp; Kootingal','Moonbi',True),
    ('Highway New England','New England Highway',False),
])
def test_literal_normalisation(text,term,expected):
    assert contains(text,term) is expected


def test_empty_all_nsw_unknown_and_jurisdiction():
    assert not evaluate(configuration(include_uncertain=True),'rfs',uncertain=True).included
    assert evaluate(configuration(all_nsw=True),'traffic').included
    assert not evaluate(configuration(all_nsw=True,include_uncertain=True),'traffic',jurisdiction='VIC',uncertain=True).included
    assert not evaluate(configuration(all_nsw=True),'traffic',jurisdiction='unknown',uncertain=True).included
    assert evaluate(configuration(location_terms=['Moonbi'],include_uncertain=True),'rfs',uncertain=True).included
    districts=configuration(bom_districts=['Hunter'],include_uncertain=True)
    assert evaluate(districts,'rfs',fields=(('incident location','Hunter'),)).included
    assert evaluate(districts,'bom',district_fields=('Hunter',)).included


def test_configuration_validation_and_aliases():
    policy=configuration(councils=['Tamworth','Tamworth Regional'],location_terms='Moonbi\n moonbi \n\nWerris Creek')
    assert policy['councils']==['Tamworth Regional']
    assert policy['location_terms']==['Moonbi','Werris Creek']
    for kwargs in ({'councils':['Invented council']},{'location_terms':['a'*121]},
                   {'location_terms':[f'term {i}' for i in range(401)]}):
        with pytest.raises(ValueError):
            configuration(**kwargs)


def test_bom_cancelled_scopes_and_background_do_not_match_terms():
    policy=configuration(location_terms=['Hunter'])
    base=Alert('geo','Flood Warning','Flood Warning','NSW','','','Alert')
    background=replace(base,warning_summary='Background rainfall for Hunter.')
    assert not bom_coverage(background,CouncilMatch('unknown'),{KEY:policy}).included
    mixed=replace(base,area_desc='Hunter, Illawarra',warning_summary=(
        'Flooding in parts of Illawarra. Flooding is no longer occurring in the Hunter district '
        'and the warning for this district is CANCELLED.'))
    assert not bom_coverage(mixed,CouncilMatch('unknown'),{KEY:policy}).included
    marine=replace(base,warning_sections=(WarningSection('Cancellation','Hunter','CAN'),
                                        WarningSection('Strong Wind Warning','Illawarra','REN')))
    assert not bom_coverage(marine,CouncilMatch('unknown'),{KEY:policy}).included
    assert bom_coverage(replace(base,area_desc='Hunter'),CouncilMatch('unknown'),{KEY:policy}).included


class Radio:
    message_budget=126
    supports_notice_guards=True
    def __init__(self):
        self.pending=[]
    def enqueue_notice(self,parts,on_result=None,priority=1,valid_if=None):
        self.pending.append((parts,on_result,valid_if))
        return True
    def enqueue_verification(self,*args,**kwargs):
        return True


def rfs_item():
    return Incident('rfs-geo','Grass Fire','Watch and Act','Central Coast','Moonbi Road',
                    'Being controlled','Grass Fire','','https://www.rfs.nsw.gov.au/')


def traffic_item():
    return TrafficItem('incident:geo','incident','HAZARD','HAZARD','New England Highway','Moonbi','',
                       'Northbound','Affected','Exercise caution',None,None,None,None,False,None,state='NSW')


def prepare(service,dry_run=True):
    db=Database(':memory:')
    for key,value in {KEY:configuration(location_terms=['Moonbi']),f'{service}_enabled':True,
                      'dry_run':dry_run,'rfs_levels':['Watch and Act'],'traffic_types':['incident'],
                      'traffic_baseline_done':True,'filter_include_suffix':['Warning']}.items():
        db.set_setting(key,value)
    return db,Radio()


@pytest.mark.asyncio
@pytest.mark.parametrize('service',['bom','rfs','traffic'])
async def test_terms_only_poll_preview_resend_and_deduplication(service):
    db,radio=prepare(service)
    app=FastAPI()
    app.state.db,app.state.tx=db,radio
    if service=='bom':
        poller=BomPoller(db,radio)
        app.state.poller=poller
        item=dict(id='bom-geo',region='NSW',event='Flood Warning',headline='Flood Warning',
                  area_desc='Moonbi',message_type='Alert',references=[])
        await poller._process(item,FilterRules([],['Warning'],[]),'Australia/Sydney',0,True)
        db.replace_bom_current([item],{'NSW'},'2026-10-05T00:00:00Z')
    elif service=='rfs':
        poller=RFSPoller(db,radio,SimpleNamespace(fetch=None))
        app.state.rfs_poller=poller
        db.rfs_save_incident(rfs_item(),rfs_item().revision)
        await poller.poll_once(replay_items=[rfs_item()])
    else:
        poller=TrafficPoller(db,radio)
        poller._councils=[]
        app.state.traffic_poller=poller
        db.traffic_save_item(traffic_item(),'',True)
        await poller.poll_once(replay_items=[traffic_item()])
    first=db.query_service_history(source=service)
    assert first[0]['transmit_status']=='dry-run'
    assert 'location term' in first[0]['detail']
    service_runner=Troubleshooting(app)
    for mode in ('reprocess','resend'):
        preview=await service_runner.preview(service,mode)
        assert preview['notices']==1 and not preview['errors']
        service_runner.start(preview['token'])
        await service_runner.task
        assert service_runner.job['status']=='completed'
    assert not radio.pending
    db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('service',['bom','rfs','traffic'])
async def test_queued_messages_use_current_universal_coverage(service):
    db,radio=prepare(service,False)
    if service=='bom':
        poller=BomPoller(db,radio)
        await poller._process(dict(id='queue',region='NSW',event='Flood Warning',headline='Flood Warning',
                                  area_desc='Moonbi',references=[],message_type='Alert'),
                              FilterRules([],['Warning'],[]),'Australia/Sydney',0,False)
    elif service=='rfs':
        class Client:
            async def fetch(self): return [rfs_item()]
        await RFSPoller(db,radio,Client()).poll_once()
    else:
        class Client:
            async def fetch(self): return [traffic_item()]
            async def boundaries(self): return []
        await TrafficPoller(db,radio,Client()).poll_once()
    assert radio.pending
    guard=radio.pending[0][2]
    assert guard()
    db.set_setting(KEY,configuration(location_terms=['Elsewhere']))
    assert not guard()
    db.set_setting(KEY,configuration(all_nsw=True))
    assert guard()
    db.set_setting(f'{service}_enabled',False)
    assert not guard()
    db.close()


def web_app(db):
    app=FastAPI()
    app.include_router(router)
    app.state.db=db
    app.state.tx=Radio()
    for name in ('poller','rfs_poller','traffic_poller'):
        setattr(app.state,name,SimpleNamespace(poke=lambda:None))
    return app


def test_migration_preview_is_read_only_and_activation_is_explicit():
    db=Database(':memory:')
    db.set_setting('rfs_councils',['Tamworth Regional'])
    db.set_setting('bom_districts',['Hunter'])
    db.rfs_save_incident(rfs_item(),rfs_item().revision)
    settings=db.all_settings()
    app=web_app(db)
    client=TestClient(app)
    page=client.get('/settings/geography')
    assert page.status_code==200 and 'Review and activate' in page.text
    assert proposal(settings)['all_nsw']  # Existing BOM All NSW must be disclosed.
    preview=client.post('/settings/geography',data={'action':'preview','location_terms':'Moonbi'})
    assert preview.status_code==200 and 'location term' in preview.text
    assert db.all_settings()==settings
    assert not db.query_service_history()
    assert not app.state.tx.pending
    result=client.post('/settings/geography',data={'action':'save','location_terms':'Moonbi\nmoonbi','bom_districts':'Hunter'})
    assert result.status_code==200
    assert db.get_setting(KEY)['location_terms']==['Moonbi','Hunter']
    assert 'bom_districts' not in db.get_setting(KEY)
    assert db.get_setting('geographic_legacy_settings')['bom_all_councils'] is True
    assert db.get_setting('bom_enabled')==settings['bom_enabled']
    assert db.get_setting('rfs_enabled')==settings['rfs_enabled']
    assert not app.state.tx.pending
    assert not db.query_service_history()
    db.close()


def test_invalid_settings_do_not_activate():
    db=Database(':memory:')
    client=TestClient(web_app(db))
    result=client.post('/settings/geography',data={'action':'save','location_terms':'a'*121})
    assert result.status_code==422 and '120 characters' in result.text
    assert not db.get_setting(KEY)
    db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('service',['rfs','traffic'])
async def test_explicit_closure_survives_narrowed_coverage_without_repeat(service):
    db,radio=prepare(service,False)
    if service=='rfs':
        class Client:
            value=rfs_item()
            async def fetch(self): return [self.value]
        client=Client()
        poller=RFSPoller(db,radio,client)
    else:
        class Client:
            value=traffic_item()
            async def fetch(self): return [self.value]
            async def boundaries(self): return []
        client=Client()
        poller=TrafficPoller(db,radio,client)
    await poller.poll_once()
    for parts,callback,guard in radio.pending:
        assert guard()
        for index in range(len(parts)):
            callback(index,True,'')
    radio.pending.clear()
    db.set_setting(KEY,configuration(location_terms=['Elsewhere']))
    client.value=replace(client.value, status='Out') if service=='rfs' else replace(client.value,ended=True)
    await poller.poll_once()
    assert radio.pending
    for parts,callback,guard in radio.pending:
        assert guard()
        text=' '.join(part[0] for part in parts)
        assert ('CLOSED' if service=='rfs' else 'ENDED') in text
        if service=='traffic':
            assert 'reopening unconfirmed' in text
        for index in range(len(parts)):
            callback(index,True,'')
    radio.pending.clear()
    await poller.poll_once()
    assert not radio.pending
    db.close()


@pytest.mark.asyncio
async def test_boundary_failure_does_not_block_terms_only_traffic():
    from app.traffic.feed import TrafficFeedError
    db,radio=prepare('traffic')
    class Client:
        async def fetch(self): return [traffic_item()]
        async def boundaries(self): raise TrafficFeedError('unavailable')
    await TrafficPoller(db,radio,Client()).poll_once()
    assert db.latest_service_history('traffic','incident:geo')['transmit_status']=='dry-run'
    db.close()


@pytest.mark.asyncio
async def test_universal_upgrade_and_expansion_do_not_resend_confirmed_bom():
    db,radio=prepare('bom',False)
    poller=BomPoller(db,radio)
    item=dict(id='confirmed',region='NSW',event='Flood Warning',headline='Flood Warning',
              area_desc='Moonbi',references=[],message_type='Alert')
    await poller._process(item,FilterRules([],['Warning'],[]),'Australia/Sydney',0,False)
    for parts,callback,guard in radio.pending:
        for index in range(len(parts)): callback(index,True,'')
    radio.pending.clear()
    db.set_setting(KEY,configuration(location_terms=['Moonbi','New England']))
    await poller._process(item,FilterRules([],['Warning'],[]),'Australia/Sydney',0,False)
    assert not radio.pending
    assert db.get_state('confirmed')['disposition']=='sent'
    db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('service',['bom','rfs','traffic'])
async def test_geographic_match_does_not_override_other_filters(service):
    db,radio=prepare(service,False)
    if service=='bom':
        await BomPoller(db,radio)._process(dict(id='blocked',region='NSW',event='Flood Watch',
                                              headline='Flood Watch',area_desc='Moonbi',references=[]),
                                          FilterRules([],['Warning'],[]),'Australia/Sydney',0,False)
    elif service=='rfs':
        db.set_setting('rfs_levels',['Emergency Warning'])
        class Client:
            async def fetch(self): return [rfs_item()]
        await RFSPoller(db,radio,Client()).poll_once()
    else:
        db.set_setting('traffic_types',['fire'])
        class Client:
            async def fetch(self): return [traffic_item()]
            async def boundaries(self): return []
        await TrafficPoller(db,radio,Client()).poll_once()
    assert not radio.pending
    db.close()


def test_all_nsw_preserves_saved_councils_and_terms():
    db=Database(':memory:')
    client=TestClient(web_app(db))
    result=client.post('/settings/geography',data={'action':'save','all_nsw':'1',
                       'councils':'Tamworth Regional','location_terms':'Moonbi'})
    assert result.status_code==200
    assert db.get_setting(KEY)['councils']==['Tamworth Regional']
    assert db.get_setting(KEY)['location_terms']==['Moonbi']
    assert 'value="Tamworth Regional" checked' in result.text
    assert 'disabled checked' not in result.text
    db.close()


def test_generic_incident_cause_is_not_a_location_term():
    from app.geography import incident_coverage
    item=rfs_item()
    assert not incident_coverage(item,{KEY:configuration(location_terms=['Grass Fire'])}).included
    assert incident_coverage(replace(item,name='Moonbi grass fire'),{KEY:configuration(location_terms=['Moonbi'])}).included


def test_missing_saved_evidence_is_reported_without_breaking_preview():
    db=Database(':memory:')
    db.rfs_save_incident(rfs_item(),rfs_item().revision)
    db._conn.execute("UPDATE rfs_incidents SET normalized_data='{}'")
    db._conn.commit()
    response=TestClient(web_app(db)).get('/settings/geography')
    assert response.status_code==200 and 'saved geographic evidence is incomplete' in response.text
    db.close()


@pytest.mark.parametrize('text,included', [
    ('near Moonbi Street', False), ('Moonbi Street, Moonbi', True),
    ('Moonbi Street and Moonbi Street', False), ('Moonbi St', True),
    ('Moonbird Street', False), ('near MOONBI—STREET', False),
    ('near Ｍｏｏｎｂｉ   Street', False), ('Moonbi Street then Kootingal', True),
])
@pytest.mark.parametrize('service', ['bom', 'rfs', 'traffic'])
def test_location_exclusions_suppress_only_contained_occurrences(text, included, service):
    policy = configuration(location_terms=['Moonbi', 'Kootingal'], location_exclusions=['Moonbi Street'])
    result = evaluate(policy, service, fields=(('location', text),))
    assert result.included is included
    if 'Street, Moonbi' in text:
        assert result.term_matches == (('Moonbi', 'location'),)
        assert result.suppressed_matches
        assert 'ignored' in result.reason


def test_exclusions_keep_separate_fields_and_other_geographic_criteria():
    policy = configuration(location_terms=['Moonbi'], location_exclusions=['Moonbi Street'])
    fields = (('road details', 'near Moonbi Street'), ('suburb', 'Moonbi'))
    assert evaluate(policy, 'traffic', fields=fields).term_matches == (('Moonbi', 'suburb'),)
    blocked = fields[:1]
    assert evaluate(policy | {'councils': ['Tamworth Regional']}, 'traffic', ('Tamworth Regional',), blocked).included
    assert evaluate(policy | {'all_nsw': True}, 'traffic', fields=blocked).included
    assert evaluate(policy | {'include_uncertain': True}, 'traffic', fields=blocked, uncertain=True).included
    assert not evaluate(policy | {'all_nsw': True}, 'traffic', fields=blocked, jurisdiction='VIC').included
    legacy = policy | {'version': 1, 'bom_districts': ['Hunter']}
    assert evaluate(legacy, 'bom', fields=blocked, district_fields=('Hunter',)).included
    assert not evaluate(policy, 'traffic', fields=(('road', 'Moonbi'), ('suburb', 'Street'))).suppressed_matches


def test_overlapping_exclusions_and_longer_positive_phrase():
    policy = configuration(location_terms=['Moonbi', 'Moonbi Street Bridge'],
                           location_exclusions=['Moonbi Street', 'near Moonbi Street'])
    result = evaluate(policy, 'traffic', fields=(('road', 'near Moonbi Street Bridge'),))
    assert result.term_matches == (('Moonbi Street Bridge', 'road'),)
    assert len(result.exclusion_matches) == 2
    assert len(result.suppressed_matches) == 2


def test_exclusion_configuration_validation_and_old_policy_compatibility():
    policy = configuration(location_terms=['Moonbi'], location_exclusions=' Moonbi Street\nmoonbi street\n\nMoonbi St')
    assert policy['location_exclusions'] == ['Moonbi Street', 'Moonbi St']
    assert proposal({KEY: policy}) == policy
    old = dict(policy); old.pop('location_exclusions')
    assert proposal({KEY: old})['location_exclusions'] == []
    assert evaluate(old, 'traffic', fields=(('road', 'Moonbi Street'),)).included
    for entries in [['x'*121], [str(i) for i in range(401)]]:
        with pytest.raises(ValueError):
            configuration(location_exclusions=entries)


def test_exclusion_settings_preview_save_reload_and_validation():
    db = Database(':memory:')
    db.set_setting(KEY, configuration(location_terms=['Moonbi']))
    original = db.all_settings()
    client = TestClient(web_app(db))
    form = {'location_terms': 'Moonbi', 'location_exclusions': 'Moonbi Street', 'action': 'preview'}
    response = client.post('/settings/geography', data=form)
    assert response.status_code == 200
    assert db.all_settings() == original
    assert 'Moonbi Street' in response.text
    form['action'] = 'save'
    assert client.post('/settings/geography', data=form, follow_redirects=False).status_code == 303
    assert db.get_setting(KEY)['location_exclusions'] == ['Moonbi Street']
    assert 'Moonbi Street' in client.get('/settings/geography').text
    response = client.post('/settings/geography', data=form | {'location_exclusions': 'x'*121})
    assert response.status_code == 422
    assert db.get_setting(KEY)['location_exclusions'] == ['Moonbi Street']


def test_real_new_lambton_false_positive_and_genuine_moonbi():
    from app.geography import traffic_coverage, incident_coverage
    policy = configuration(councils=['Tamworth Regional'], location_terms=['Moonbi', 'New England'], location_exclusions=['Moonbi Street'])
    settings = {KEY: policy}
    false_positive = replace(traffic_item(), title='CRASH Car', road='Bridges Road', suburb='New Lambton', road_details='near Moonbi Street')
    before = traffic_coverage(false_positive, 'Newcastle', {KEY: policy | {'location_exclusions': []}})
    after = traffic_coverage(false_positive, 'Newcastle', settings)
    assert before.included and not after.included
    assert after.suppressed_matches == (('Moonbi', 'road details', 'Moonbi Street', 1, 2),)
    assert traffic_coverage(replace(false_positive, suburb='Moonbi'), 'Newcastle', settings).included
    assert traffic_coverage(traffic_item(), 'Tamworth Regional', settings).included
    assert incident_coverage(replace(rfs_item(), name='Moonbi Street', location='Moonbi Street', state='NSW'), settings).included is False
    assert incident_coverage(replace(rfs_item(), location='Moonbi Street, Moonbi'), settings).included


def test_bom_exclusions_and_closure_evidence_preserved():
    from app.geography import closure_coverage
    policy = configuration(location_terms=['Moonbi'], location_exclusions=['Moonbi Street'])
    alert = Alert('geo-exclusion', 'Flood Warning', 'Flood Warning', 'NSW', '', '', 'Alert')
    blocked = bom_coverage(replace(alert, area_desc='Moonbi Street'), CouncilMatch('unknown'), {KEY: policy})
    assert not blocked.included
    assert bom_coverage(replace(alert, area_desc='Moonbi Street, Moonbi'), CouncilMatch('unknown'), {KEY: policy}).included
    db = SimpleNamespace(latest_successful_broadcast=lambda *args: True)
    for service in ('bom', 'rfs', 'traffic'):
        closed = closure_coverage(blocked, db, service, 'geo-exclusion', True)
        assert closed.included and closed.suppressed_matches == blocked.suppressed_matches
    mixed = replace(alert, area_desc='Moonbi Street, Moonbi', warning_summary='Flooding is no longer occurring in Moonbi and the warning for this district is CANCELLED.')
    assert not bom_coverage(mixed, CouncilMatch('unknown'), {KEY: policy}).included


@pytest.mark.asyncio
@pytest.mark.parametrize('service', ['bom', 'rfs', 'traffic'])
async def test_reprocessing_and_resend_apply_current_exclusions(service):
    db, radio = prepare(service)
    app = web_app(db)
    if service == 'bom':
        item = dict(id='bom-excluded', region='NSW', event='Flood Warning', headline='Flood Warning', area_desc='Moonbi', references=[])
        app.state.poller = BomPoller(db, radio)
        db.replace_bom_current([item], {'NSW'}, '2026-10-05T00:00:00Z')
    elif service == 'rfs':
        app.state.rfs_poller = RFSPoller(db, radio, SimpleNamespace(fetch=None))
        db.rfs_save_incident(rfs_item(), rfs_item().revision)
    else:
        app.state.traffic_poller = TrafficPoller(db, radio)
        app.state.traffic_poller._councils = []
        db.traffic_save_item(traffic_item(), '', True)
    db.set_setting(KEY, configuration(location_terms=['Moonbi'], location_exclusions=['Moonbi']))
    runner = Troubleshooting(app)
    for mode in ('reprocess', 'resend'):
        preview = await runner.preview(service, mode)
        assert not preview['errors']
        assert preview['notices'] == 0
    assert not radio.pending
    db.close()

"""Local BOM scope/lifecycle decisions with reconstructed historical examples."""
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.db import Database
from app.bom_scope import ScopeStore, names, bootstrap
from app.bom_enricher import BOMEnrichment, WarningSection
from app.bom_episodes import destination
from app.bom_area import CouncilMatch
from app.geography import configuration, KEY, bom_delivery_coverage
from app.filters import FilterRules
from app.models import Alert
from app.poller import BomPoller
from app.transmission import TxResult
from app.web.routes import router
from app.maintenance import clean, DEFAULTS, backup
from test_bom_episodes import Radio, PRODUCT


@pytest.fixture
def db():
    db=Database(':memory:')
    db.set_setting(KEY,configuration(location_terms=['Tamworth','Quirindi','Armidale']))
    yield db
    db.close()


async def run(db, *, locations='Tamworth, Quirindi', summary='Damaging winds expected.',
              cancel=False, sections=(), radio=None, dry=True, force=False, observe=True, headline=None, **extra):
    radio=radio or Radio()
    event='Marine Wind Warning' if sections else 'Severe Thunderstorm Warning'
    raw=dict(id=PRODUCT,region='NSW',event=event,headline=headline or event,area_desc=locations,
             message_type='Cancel' if cancel else 'Alert',references=[] if cancel else [PRODUCT],
             effective=extra.pop('effective',''),expires=extra.pop('expires',''),detail=extra.pop('detail',''))
    enrichment=BOMEnrichment(locations=locations,summary=summary,sections=sections,status='available',**extra)
    async def enrich(url):return enrichment
    db.set_setting('dry_run',dry)
    poller=BomPoller(db,radio);poller._enricher=SimpleNamespace(enrich=enrich)
    result=await poller._process(raw,FilterRules.from_settings(db.all_settings()),'Australia/Sydney',0,dry,
                                 settings=db.all_settings(),force=force,observe=observe)
    return result,raw,radio


def last(db):return db.latest_history(PRODUCT)
def meta(db):return json.loads(last(db)['metadata'])


@pytest.mark.asyncio
async def test_nov14_reconstructed_cancellations_only_close_announced_local_scope(db):
    fixture=json.loads((Path(__file__).parent/'fixtures/bom_scope_history.json').read_text())
    assert len([r for r in fixture['records'] if r['episode']=='2331-L' and r['action']=='CANCELLED'])==6
    assert (await run(db))[0]
    for area in ('North West Slopes and Plains','Mid North Coast','Central West Slopes and Plains',
                 'Mid North Coast, North West Slopes and Plains, Upper Western','Central Tablelands'):
        result,raw,_=await run(db,locations='Illawarra',summary=f'Damaging winds in Illawarra. The warning for {area} is CANCELLED.')
        assert not result and last(db)['transmit_status'] is None
        assert 'previously announced selected scope' in raw['selection_reason']
    assert (await run(db,locations='Illawarra, Southern Tablelands',cancel=True))[0]
    text=last(db)['transmitted_text']
    assert 'CANCELLED' in text and 'Tamworth' in text and 'Quirindi' in text
    assert 'Illawarra' not in text and 'Southern Tablelands' not in text
    assert not (await run(db,locations='Elsewhere',cancel=True))[0]


@pytest.mark.asyncio
async def test_nov13_reconstructed_unrelated_scopes_do_not_authorize_cancellation(db):
    assert (await run(db,locations='Armidale, Tenterfield, Glen Innes'))[0]
    for area in ('Illawarra, Southern Tablelands','Northern Rivers, Northern Tablelands',
                 'Metropolitan, Central Tablelands, North West Slopes and Plains'):
        assert not (await run(db,locations='Elsewhere',summary=f'The warning for {area} is CANCELLED.'))[0]
    assert (await run(db,locations='Mid North Coast, Hunter',cancel=True))[0]
    assert 'Armidale' in last(db)['transmitted_text'] and 'Hunter' not in last(db)['transmitted_text']


@pytest.mark.asyncio
async def test_partial_cancellation_once_then_reactivation_can_cancel_again(db):
    assert (await run(db))[0]
    opts=dict(locations='Armidale',summary='Damaging winds expected. The warning for Tamworth is CANCELLED.')
    # Armidale is newly active; only Tamworth from the previous announcement closes.
    assert (await run(db,**opts))[0]
    assert meta(db)['bom_scope']['cancel'][0]['label']=='Tamworth'
    assert not (await run(db,**opts))[0]
    assert not (await run(db,locations='Armidale',summary=opts['summary']+' The warning for Sydney is CANCELLED.'))[0]
    assert (await run(db,locations='Tamworth, Armidale'))[0]
    assert meta(db)['bom_episode']['action']=='UPDATE'
    assert (await run(db,**opts))[0]
    assert meta(db)['bom_scope']['cancel'][0]['label']=='Tamworth'


@pytest.mark.asyncio
@pytest.mark.parametrize('dry',[True,False])
async def test_cancellation_switch_suppresses_and_does_not_replay_backlog(db,dry):
    radio=Radio()
    assert (await run(db,dry=dry,radio=radio))[0]
    if not dry:radio.finish(TxResult('accepted'))
    db.set_setting('bom_send_cancellations',False)
    assert not (await run(db,locations='Elsewhere',cancel=True,dry=dry,radio=radio))[0]
    assert 'notifications disabled' in last(db)['detail']
    assert meta(db)['bom_episode']['state']=='closed'
    db.set_setting('bom_send_cancellations',True)
    assert not (await run(db,locations='Elsewhere',cancel=True,dry=dry,radio=radio))[0]


@pytest.mark.asyncio
async def test_switch_removes_cancelled_content_from_mixed_updates(db):
    await run(db)
    db.set_setting('bom_send_cancellations',False)
    assert (await run(db,locations='Armidale',summary='Destructive winds expected. The warning for Tamworth is CANCELLED.'))[0]
    assert 'CANCELLED' not in last(db)['transmitted_text'] and 'Armidale' in last(db)['transmitted_text']
    db.set_setting('bom_send_cancellations',True)
    assert not (await run(db,locations='Armidale',summary='Destructive winds expected. The warning for Tamworth is CANCELLED.'))[0]


@pytest.mark.asyncio
async def test_setting_change_alone_does_not_create_update(db):
    await run(db);count=len(db.query_service_history())
    for flag in (False,True):
        db.set_setting('bom_send_cancellations',flag)
        assert not (await run(db))[0]
    assert len(db.query_service_history())==count


@pytest.mark.asyncio
async def test_dry_run_does_not_introduce_live_or_other_destination_scope(db):
    await run(db)
    assert not (await run(db,cancel=True,dry=False))[0]
    assert 'selected scope' in last(db)['detail']
    await run(db,dry=False)
    db.set_setting('meshcore_channel',2)
    assert not (await run(db,cancel=True,dry=False))[0]


@pytest.mark.asyncio
async def test_queued_cancellation_guards_switch_selection_and_superseding_revision(db):
    radio=Radio();await run(db,dry=False,radio=radio);radio.finish(TxResult('accepted'))
    assert (await run(db,cancel=True,dry=False,radio=radio))[0]
    guard=radio.notices[-1]['valid'];assert guard()
    db.set_setting('bom_send_cancellations',False);assert not guard()
    db.set_setting('bom_send_cancellations',True)
    db.set_setting(KEY,configuration(location_terms=['Elsewhere']));assert not guard()


@pytest.mark.asyncio
async def test_pending_cancellation_is_not_replaced_by_another_provider_revision(db):
    radio=Radio();await run(db,dry=False,radio=radio);radio.finish(TxResult('accepted'))
    assert (await run(db,cancel=True,dry=False,radio=radio))[0]
    guard=radio.notices[-1]['valid'];old=last(db)['id']
    assert not (await run(db,locations='Other final area',cancel=True,dry=False,radio=radio))[0]
    assert last(db)['id']==old and guard()
    radio.finish(TxResult('accepted'))


@pytest.mark.asyncio
async def test_nov3_unstructured_updates_remain_conservative(db):
    for locations,summary in [('Armidale, Tamworth, Scone','Damaging winds expected.'),
                              ('Armidale, Grafton, Katoomba','Damaging winds expected.'),
                              ('Armidale, Grafton, Nowra','Damaging winds expected.'),
                              ('Armidale, Grafton, Nowra','Destructive winds expected.')]:
        assert (await run(db,locations=locations,summary=summary))[0]
        assert meta(db)['bom_scope']['comparison']=='conservative whole warning'
    assert meta(db)['bom_episode']['action']=='UPDATE'


@pytest.mark.asyncio
async def test_only_proven_unrelated_structured_changes_are_suppressed(db):
    db.set_setting(KEY,configuration(location_terms=['Hunter']))
    local=WarningSection('Gale Warning','Hunter Coast','NEW')
    remote=WarningSection('Strong Wind Warning','Sydney Coast','NEW')
    opts=dict(locations='Hunter Coast, Sydney Coast',summary='',sections=(local,remote))
    assert (await run(db,**opts))[0]
    assert meta(db)['bom_scope']['comparison']=='structured selected sections'
    assert not (await run(db,**(opts|dict(sections=(local,replace(remote,phenomenon='Storm Force Wind Warning'))))))[0]
    assert (await run(db,**(opts|dict(sections=(replace(local,phenomenon='Storm Force Wind Warning'),remote)))))[0]


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value',[('severity','Severe'),('urgency','Immediate'),('certainty','Observed'),('expires','2099-01-01T00:00:00Z')])
async def test_shared_warning_changes_always_trigger_update(db,field,value):
    await run(db)
    assert (await run(db,**{field:value}))[0]
    assert meta(db)['bom_episode']['action']=='UPDATE'


@pytest.mark.asyncio
async def test_publication_only_refresh_is_ignored_and_scope_reversion_is_material(db):
    await run(db,effective='2026-11-01T00:00:00Z')
    assert not (await run(db,effective='2026-11-01T00:10:00Z'))[0]
    assert (await run(db,summary='Destructive winds expected.'))[0]
    assert (await run(db))[0]


@pytest.mark.asyncio
async def test_all_can_sections_do_not_cancel_unrelated_expired_coasts(db):
    db.set_setting(KEY,configuration(all_nsw=True))
    await run(db,locations='Hunter Coast, Sydney Coast',summary='',sections=(WarningSection('Gale Warning','Hunter Coast'),WarningSection('Gale Warning','Sydney Coast')))
    assert (await run(db,locations='Sydney Coast',summary='',sections=(WarningSection('Cancellation','Sydney Coast','CAN'),)))[0]
    assert 'Sydney Coast' in last(db)['transmitted_text'] and 'Hunter' not in last(db)['transmitted_text']


@pytest.mark.asyncio
async def test_replay_and_geographic_diagnostics_use_same_scope_rules(db):
    await run(db)
    result,raw,_=await run(db,locations='Illawarra',summary='The warning for Sydney is CANCELLED.')
    assert not result
    alert=Alert.from_bom(raw);alert.warning_summary=raw['warning_summary']
    before=db._conn.total_changes
    coverage=bom_delivery_coverage(alert,CouncilMatch('unknown'),db.all_settings(),db)
    assert not coverage.included and db._conn.total_changes==before
    assert not (await run(db,locations='Illawarra',summary='The warning for Sydney is CANCELLED.',force=True,observe=False))[0]


def test_compound_names_are_not_split_on_and():
    assert names('North West Slopes and Plains, Hunter')==['North West Slopes and Plains','Hunter']


@pytest.mark.asyncio
async def test_restart_and_migration_are_idempotent_and_preserve_text(tmp_path):
    path=str(tmp_path/'upgrade.db');db=Database(path)
    db.set_setting(KEY,configuration(location_terms=['Tamworth']))
    await run(db);saved=dict(last(db));db.close()
    for _ in range(2):
        db=Database(path);bootstrap(db)
        assert dict(last(db))==saved
        assert db._conn.execute("SELECT COUNT(*) FROM history_migrations WHERE name='bom_scope_v1'").fetchone()[0]==1
        db.close()


@pytest.mark.asyncio
async def test_retention_keeps_active_scope_evidence_and_backups_include_it(tmp_path):
    db=Database(str(tmp_path/'retained.db'));db.set_setting(KEY,configuration(location_terms=['Tamworth']))
    await run(db)
    with db.transaction('age scope history') as c:c.execute("UPDATE service_history SET ts='2020-01-01'")
    clean(db,DEFAULTS)
    assert db.query_service_history() and db._conn.execute('SELECT COUNT(*) FROM bom_scope_events').fetchone()[0]
    snapshot=backup(db,1)
    with sqlite3.connect(snapshot['path']) as c:assert c.execute('SELECT COUNT(*) FROM bom_scope_events').fetchone()[0]
    db.close()


def test_settings_default_and_round_trip(db):
    app=FastAPI();app.state.db=db;app.state.tx=SimpleNamespace(status=lambda:[])
    app.state.poller=SimpleNamespace(poke=lambda:None,status=SimpleNamespace(last_raw_response=''))
    app.include_router(router);client=TestClient(app)
    assert db.get_setting('bom_send_cancellations') is True
    data=dict(poll_interval='5',bom_enabled='1',preserve_notice_selection='1',cancellation_setting_submitted='1')
    assert client.post('/settings/bom',data=data,follow_redirects=False).status_code==303
    assert db.get_setting('bom_send_cancellations') is False
    client.post('/settings/bom',data=data|dict(bom_send_cancellations='1'),follow_redirects=False)
    assert db.get_setting('bom_send_cancellations') is True


@pytest.mark.asyncio
async def test_scope_introduction_requires_its_recorded_message_parts(db):
    from app.bom_scope import scope, attach_parts
    raw=dict(id=PRODUCT,region='NSW',event='Severe Thunderstorm Warning',headline='Warning',area_desc='Tamworth, Quirindi')
    episode=db.bom_episodes.observe(Alert.from_bom(raw));audience=destination(db.all_settings(),0)
    evidence=dict(version=1,active=[scope('term','Tamworth'),scope('term','Quirindi')],cancel=[],fingerprint='test')
    parts=['BOM NEW Warning for Tamworth','Quirindi; check bom.gov.au'];attach_parts(evidence,parts)
    row=db.add_history(PRODUCT,raw['event'],raw['area_desc'],'sent',' || '.join(parts),transmit_status='queued',
                       metadata={'bom_scope':evidence},bom_announcement=dict(episode=episode,mode='live',audience=audience,revision='parts',action='NEW'))
    db.record_delivery_part(row,0,2,TxResult('accepted'))
    state=ScopeStore(db).state(episode,'live',audience)
    assert {x['label'] for x in state['active'].values()}=={'Tamworth'}
    db.record_delivery_part(row,1,2,TxResult('unconfirmed',submitted=True))
    state=ScopeStore(db).state(episode,'live',audience)
    assert {x['label'] for x in state['active'].values()}=={'Tamworth','Quirindi'}


@pytest.mark.asyncio
async def test_failed_multipart_cancellation_recovers_exact_recorded_parts(db):
    radio=Radio();radio.message_budget=80
    assert (await run(db,dry=False,radio=radio))[0];radio.finish(TxResult('accepted'))
    assert (await run(db,cancel=True,dry=False,radio=radio))[0]
    original=radio.notices[-1];text=last(db)['transmitted_text'];assert len(original['entries'])>1
    original['callback'](0,TxResult('accepted'),'')
    for i in range(1,len(original['entries'])):original['callback'](i,False,'Not attempted after failed transmission')
    assert (await run(db,cancel=True,dry=False,radio=radio))[0]
    assert last(db)['transmitted_text']==text
    assert [p[0] for p in radio.notices[-1]['entries']]==[p[0] for p in original['entries'][1:]]


@pytest.mark.asyncio
async def test_unrelated_update_does_not_rescue_a_stale_queued_warning(db):
    radio=Radio();await run(db,dry=False,radio=radio)
    guard=radio.notices[-1]['valid'];assert guard()
    await run(db,summary='Destructive winds expected.',dry=False,radio=radio)
    assert not guard()


@pytest.mark.asyncio
async def test_legacy_unrelated_cancellation_does_not_invent_scope_on_upgrade(tmp_path):
    from app.bom_scope import bootstrap
    path=str(tmp_path/'legacy.db');db=Database(path)
    db.set_setting(KEY,configuration(location_terms=['Tamworth']))
    await run(db)
    # Simulate a pre-upgrade unrelated cancellation record lacking reliable scope.
    episode=db.bom_episodes.lookup(Alert.from_bom(dict(id=PRODUCT)))
    db.add_history(PRODUCT,'Severe Thunderstorm Warning','Sydney','cancelled','BOM CANCELLED Sydney',transmit_status='dry-run',
                   metadata={'bom_episode':{'scope':'selected areas'}},
                   bom_announcement=dict(episode=episode,mode='dry-run',audience=destination(db.all_settings(),0),revision='legacy-cancel',action='CANCELLED'))
    with db.transaction('simulate pre-upgrade') as c:
        c.execute('DELETE FROM bom_scope_events');c.execute("DELETE FROM history_migrations WHERE name='bom_scope_v1'")
    before=[tuple(r) for r in db._conn.execute('SELECT * FROM service_history ORDER BY id')]
    bootstrap(db)
    state=ScopeStore(db).state(episode,'dry-run',destination(db.all_settings(),0))
    assert any(x['label']=='Tamworth' for x in state['active'].values())
    assert before==[tuple(r) for r in db._conn.execute('SELECT * FROM service_history ORDER BY id')]
    db.close()


@pytest.mark.asyncio
async def test_bom_headline_publication_prefix_is_not_material(db):
    await run(db,headline='15/06:24 EDT Severe Thunderstorm Warning')
    assert not (await run(db,headline='15/06:34 EDT Severe Thunderstorm Warning'))[0]


@pytest.mark.asyncio
async def test_pre_scope_revision_is_not_rebroadcast_on_upgrade(db):
    await run(db);saved=dict(last(db))
    # Recreate a genuine pre-scope key/material record, rather than retaining new metadata.
    rowmeta=json.loads(saved['metadata']);rowmeta.pop('bom_scope')
    raw=dict(id=PRODUCT,region='NSW',event='Severe Thunderstorm Warning',headline='Severe Thunderstorm Warning',
             area_desc='Tamworth, Quirindi',references=[PRODUCT])
    alert=Alert.from_bom(raw);alert.specific_locations='Tamworth, Quirindi';alert.warning_summary='Damaging winds expected.'
    old_revision=':'.join(saved['revision_hash'].split(':')[:2])
    with db.transaction('pre-scope fixture') as c:
        c.execute('DELETE FROM bom_scope_events')
        c.execute('UPDATE service_history SET metadata=?,revision_hash=? WHERE id=?',(json.dumps(rowmeta),old_revision,saved['id']))
        c.execute('UPDATE bom_announcements SET revision_hash=?,material_hash=? WHERE history_id=?',(old_revision,alert.content_hash(),saved['id']))
    assert not (await run(db))[0]
    assert last(db)['id']==saved['id'] and last(db)['transmitted_text']==saved['transmitted_text']


@pytest.mark.asyncio
async def test_expiry_does_not_generate_or_replay_late_cancellation(db):
    await run(db)
    with db.transaction('expire episode') as c:c.execute('UPDATE bom_episodes SET expires_at=1')
    assert not (await run(db,cancel=True))[0]
    assert 'Expired episode' in last(db)['detail']


def test_concurrent_issue_only_scope_revisions_reserve_once(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import asyncio
    path=str(tmp_path/'concurrent-scopes.db');db=Database(path)
    db.set_setting(KEY,configuration(location_terms=['Tamworth']));db.close()
    def process(index):
        value=Database(path)
        try:return asyncio.run(run(value,headline=f'15/06:{20+index} EDT Severe Thunderstorm Warning'))[0]
        finally:value.close()
    with ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(process,range(4)))
    db=Database(path)
    try:
        assert sum(results)==1
        assert len(db.query_service_history(source='bom'))==1
    finally:db.close()


@pytest.mark.asyncio
async def test_disabling_a_queued_cancellation_does_not_leave_a_reenable_backlog(db):
    radio=Radio();await run(db,dry=False,radio=radio);radio.finish(TxResult('accepted'))
    await run(db,cancel=True,dry=False,radio=radio)
    cancelled_notice=radio.notices[-1]
    db.set_setting('bom_send_cancellations',False)
    assert not (await run(db,cancel=True,dry=False,radio=radio))[0]
    assert not cancelled_notice['valid']()
    for i in range(len(cancelled_notice['entries'])):
        cancelled_notice['callback'](i,TxResult('not_attempted','Cancellation notifications disabled'),'Cancellation notifications disabled')
    db.set_setting('bom_send_cancellations',True)
    assert not (await run(db,cancel=True,dry=False,radio=radio))[0]
    assert len(radio.notices)==2


@pytest.mark.asyncio
async def test_deselected_scope_closes_silently_without_later_backlog(db):
    await run(db)
    db.set_setting(KEY,configuration(location_terms=['Elsewhere']))
    assert not (await run(db,cancel=True))[0]
    db.set_setting(KEY,configuration(location_terms=['Tamworth','Quirindi']))
    assert not (await run(db,cancel=True))[0]


@pytest.mark.asyncio
async def test_unclassified_structured_area_keeps_conservative_hazard_updates(db):
    db.set_setting(KEY,configuration(location_terms=['Hunter']))
    local=WarningSection('Gale Warning','Hunter Coast','NEW')
    broad=WarningSection('Strong Wind Warning','New South Wales coastal waters','NEW')
    opts=dict(locations='Hunter Coast',summary='',sections=(local,broad))
    assert (await run(db,**opts))[0]
    assert meta(db)['bom_scope']['comparison']=='conservative whole warning'
    assert (await run(db,**(opts|dict(sections=(local,replace(broad,phenomenon='Storm Force Wind Warning'))))))[0]
    assert 'Storm Force Wind' in last(db)['transmitted_text']


@pytest.mark.asyncio
async def test_all_six_saved_nov3_updates_remain_deliverable(db):
    import re
    fixture=json.loads((Path(__file__).parent/'fixtures/bom_scope_history.json').read_text())
    rows=[r for r in fixture['records'] if r['episode']=='2331-F']
    assert len(rows)==6
    await run(db,locations='Armidale, Tamworth, Scone, Inverell, Glen Innes, Nowendoc')
    for row in rows:
        text=re.sub(r'\b\d/\d\s+','',row['text'].replace(' || ',' '))
        places=text.rsplit(' for ',1)[1].split('; check')[0]
        summary='Destructive winds expected.' if 'destructive winds' in text else 'Damaging winds expected.'
        assert (await run(db,locations=places,summary=summary))[0]
        assert meta(db)['bom_episode']['action']=='UPDATE'


def test_backup_can_be_taken_before_scope_schema_upgrade(tmp_path):
    db=Database(str(tmp_path/'pre-scope-backup.db'))
    with db.transaction('pre-scope database') as conn:conn.execute('DROP TABLE bom_scope_events')
    assert backup(db,1)['status']=='verified'
    db.close()


@pytest.mark.asyncio
async def test_location_exclusions_apply_to_partial_cancellations(db):
    db.set_setting(KEY,configuration(location_terms=['Armidale'],location_exclusions=['Armidale Road']))
    await run(db,locations='Armidale')
    assert not (await run(db,locations='Elsewhere',summary='The warning for Armidale Road is CANCELLED.'))[0]
    assert (await run(db,cancel=True))[0]
    assert 'Armidale' in last(db)['transmitted_text']


@pytest.mark.asyncio
async def test_legacy_product_history_without_selection_evidence_remains_unknown(db):
    db.set_setting(KEY,configuration(all_nsw=True))
    raw=dict(id=PRODUCT,event='Severe Thunderstorm Warning',headline='Warning',area_desc='Hunter, Sydney',region='NSW')
    episode=db.bom_episodes.observe(Alert.from_bom(raw))
    row=db.add_history(PRODUCT,raw['event'],raw['area_desc'],'sent','Earlier warning',transmit_status='dry-run')
    db.bom_episodes.register(episode,'dry-run',destination(db.all_settings(),0),'legacy','NEW',row)
    assert not (await run(db,cancel=True))[0]
    assert 'selected scope' in last(db)['detail']


@pytest.mark.asyncio
async def test_all_nsw_tracks_separate_named_coasts_joined_by_and(db):
    db.set_setting(KEY,configuration(all_nsw=True))
    await run(db,locations='Hunter Coast and Sydney Coast',summary='',sections=(WarningSection('Gale Warning','Hunter Coast and Sydney Coast'),))
    assert {x['label'] for x in meta(db)['bom_scope']['active']}=={'Hunter Coast','Sydney Coast'}
    assert (await run(db,locations='Sydney Coast',summary='',sections=(WarningSection('Gale Warning','Sydney Coast'),WarningSection('Cancellation','Hunter Coast','CAN'))))[0]
    assert {x['label'] for x in meta(db)['bom_scope']['cancel']}=={'Hunter Coast'}

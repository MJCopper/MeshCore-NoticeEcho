"""Useful local radio information, including saved November 15 dry-run replay."""
import json
from pathlib import Path

import pytest

from app.bom_information import facts, legacy
from app.geography import KEY, configuration
from app.transmission import TxResult
from test_bom_scope import db, run, last, meta
from test_bom_episodes import Radio, PRODUCT
from test_troubleshooting import environment


@pytest.mark.parametrize('left,right', [
    ('Risk: large hail; Risk: hail', 'Risk: large hail'),
    ('Risk: heavy rain, damaging winds', 'Risk: damaging winds; Risk: heavy rain'),
    ('ACTIVE: damaging winds', 'Risk: damaging winds'),
    (' Risk: damaging winds; Risk: damaging winds', 'Risk: damaging winds'),
])
def test_equivalent_facts(left,right):
    assert facts(left)==facts(right)


@pytest.mark.parametrize('left,right', [
    ('Risk: damaging winds', 'Risk: destructive winds'),
    ('Risk: large hail', 'Risk: giant hail'),
    ('Damaging winds are not expected', 'Risk: damaging winds'),
    ('Damaging winds are unlikely', 'Risk: damaging winds'),
    ('Avoid travel', 'Shelter indoors'),
    ('until 15 Nov 18:00', 'until 15 Nov 19:00'),
    ('Temperature -5 C', 'Temperature 5 C'),
    ('River level 1.5 m', 'River level 15 m'),
])
def test_meaningful_facts_remain_distinct(left,right):
    assert facts(left)!=facts(right)


@pytest.mark.asyncio
async def test_nov15_recorded_messages_with_reconstructed_provider_inputs(db):
    fixture=json.loads((Path(__file__).parent/'fixtures/bom_information_nov15.json').read_text())
    results=[]
    for row in fixture['records']:
        # Retain each recorded full town list, rather than testing selected towns only.
        text=row['text'].replace(' || ', ' ')
        import re
        text=re.sub(r'\b\d+/\d+\s+', '', text)
        places=text.rsplit(' for ',1)[1].split('; check')[0]
        summary='Damaging winds and large hailstones expected.'
        if row['history_id']!=20713:
            summary+=' Hail reported near an unrelated town.'
        result,raw,_=await run(db,locations=places,summary=summary)
        results.append(result)
        if not result:
            assert raw['selection_reason']=='Suppressed: no additional information for selected locations'
        else:
            # The implementation must preserve recorded radio wording for the
            # chosen replay input, including the duplicate generic hail label.
            assert last(db)['transmitted_text']==row['text'].replace('#2331-M','#2331-A')
    assert results==[True,False,False,False,True,False]
    assert len(db.query_service_history())==2


@pytest.mark.asyncio
@pytest.mark.parametrize('dry',[True,False])
async def test_superset_scope_and_real_hazard_reversions(db,dry):
    radio=Radio()
    async def send(**kwargs):
        result,_,_=await run(db,radio=radio,dry=dry,**kwargs)
        if result and not dry: radio.finish(TxResult('accepted'))
        return result
    assert await send(locations='Tamworth, Sydney')
    assert not await send(locations='Sydney, Tamworth, Newcastle')
    assert await send(locations='Armidale, Tamworth')
    assert not await send(locations='Armidale')
    assert not await send(locations='Tamworth, Armidale')
    assert await send(summary='Destructive winds expected.')
    assert await send(summary='Damaging winds expected.')
    assert not await send(summary='Damaging winds expected.')


@pytest.mark.asyncio
async def test_hidden_provider_details_do_not_advance_baseline(db):
    await run(db)
    old=last(db)['id']
    for i in range(4):
        result,raw,_=await run(db,summary=f'Damaging winds expected. Rainfall measured {i} mm in Sydney.')
        assert not result and 'no additional information' in raw['selection_reason']
    assert last(db)['id']==old
    assert (await run(db,summary='Destructive winds expected.'))[0]
    assert meta(db)['bom_scope']['baseline']['term:tamworth']==old


@pytest.mark.asyncio
async def test_explicit_instruction_change_and_qualifications(db):
    assert (await run(db,summary='Shelter indoors.'))[0]
    assert (await run(db,summary='Avoid travel.'))[0]
    assert (await run(db,summary='Damaging winds are unlikely.'))[0]
    assert (await run(db,summary='Damaging winds are not expected.'))[0]


@pytest.mark.asyncio
async def test_failed_attempt_retains_exact_recovery_and_does_not_consume_information(db):
    radio=Radio()
    await run(db,radio=radio,dry=False);radio.finish(TxResult('accepted'))
    assert (await run(db,radio=radio,dry=False,summary='Destructive winds expected.'))[0]
    saved=last(db)['transmitted_text'];identity=last(db)['id']
    radio.finish(False,'Not attempted before submission')
    assert (await run(db,radio=radio,dry=False,summary='Destructive winds expected. Other details changed.'))[0]
    assert last(db)['id']==identity and last(db)['transmitted_text']==saved
    radio.finish(TxResult('accepted'))
    assert not (await run(db,radio=radio,dry=False,summary='Destructive winds expected.'))[0]


@pytest.mark.asyncio
async def test_unrelated_refresh_keeps_queue_valid_material_change_invalidates_it(db):
    radio=Radio()
    await run(db,locations='Tamworth, Sydney',radio=radio,dry=False)
    guard=radio.notices[-1]['valid'];assert guard()
    assert not (await run(db,locations='Tamworth, Newcastle',radio=radio,dry=False))[0]
    assert len(radio.notices)==1 and guard()
    assert (await run(db,locations='Tamworth',summary='Destructive winds expected.',radio=radio,dry=False))[0]
    assert not guard() and radio.notices[-1]['valid']()


@pytest.mark.asyncio
async def test_modes_destinations_and_explicit_resend_are_independent(db):
    await run(db)
    radio=Radio()
    assert (await run(db,dry=False,radio=radio))[0]
    radio.finish(TxResult('accepted'))
    assert not (await run(db,dry=False,radio=radio))[0]
    assert (await run(db,dry=False,radio=radio,force=True))[0]
    assert meta(db)['bom_episode']['action']=='RESEND'
    radio.finish(TxResult('accepted'))
    db.set_setting('meshcore_channel',2)
    assert (await run(db,dry=False,radio=radio))[0]
    assert meta(db)['bom_episode']['action']=='NEW'


@pytest.mark.asyncio
async def test_routine_expiry_refresh_does_not_transmit(db):
    assert (await run(db,expires='2099-01-01T00:00:00Z'))[0]
    assert not (await run(db,expires='2099-01-01T01:00:00Z'))[0]


@pytest.mark.asyncio
@pytest.mark.parametrize('report', [
    'Destructive winds reported near Sydney.',
    'Giant hail observed near Sydney.',
    'Destructive winds forecast to affect Sydney.',
    'Destructive winds are not expected near Sydney.',
])
async def test_remote_report_does_not_add_local_hazard(db,report):
    await run(db,locations='Tamworth, Sydney')
    assert not (await run(db,locations='Tamworth, Sydney',summary='Damaging winds expected. '+report))[0]


@pytest.mark.asyncio
async def test_local_report_adds_information_for_only_its_scope(db):
    await run(db,locations='Tamworth, Armidale')
    assert (await run(db,locations='Tamworth, Armidale',summary='Damaging winds expected. Destructive winds reported near Armidale.'))[0]
    assert meta(db)['bom_scope']['changed_scopes']==['term:armidale']


@pytest.mark.asyncio
async def test_explicitly_shared_report_applies_to_selected_locations(db):
    await run(db)
    assert (await run(db,summary='Destructive winds observed throughout the warning area.'))[0]


@pytest.mark.asyncio
async def test_explicit_hazard_timing_is_conveyed_and_material(db):
    options=dict(ends='2099-01-01T03:00:00Z',onset='2099-01-01T00:00:00Z')
    assert (await run(db,**options))[0]
    assert (await run(db,**(options|dict(ends='2099-01-01T04:00:00Z'))))[0]
    assert (await run(db,**(options|dict(onset='2099-01-01T01:00:00Z'))))[0]


@pytest.mark.asyncio
async def test_partial_update_establishes_no_complete_information_baseline(db):
    radio=Radio();radio.message_budget=80
    assert (await run(db,radio=radio,dry=False))[0]
    notice=radio.notices[-1];assert len(notice['entries'])>1
    notice['callback'](0,TxResult('accepted'),'')
    for i in range(1,len(notice['entries'])):
        notice['callback'](i,False,'Not attempted after failure')
    saved=last(db)['transmitted_text']
    assert (await run(db,radio=radio,dry=False))[0]
    assert last(db)['transmitted_text']==saved
    assert [p[0] for p in radio.notices[-1]['entries']]==[p[0] for p in notice['entries'][1:]]
    radio.finish(TxResult('unconfirmed',submitted=True))
    assert not (await run(db,radio=radio,dry=False))[0]


@pytest.mark.asyncio
async def test_non_ascii_and_byte_budget_unchanged(db):
    from app.brief import brief_bom_parts
    from app.models import Alert
    alert=Alert.from_bom(dict(id=PRODUCT,event='Flood Warning',area_desc='Tamworth, Quirindi',detail='River rise — avoid travel.'))
    evidence={}
    plain=brief_bom_parts(alert,'Australia/Sydney','NEW',126,episode_reference='2331-A')
    traced=brief_bom_parts(alert,'Australia/Sydney','NEW',126,scope_evidence=evidence,episode_reference='2331-A')
    assert plain==traced and all(len(p.encode())<=126 for p in plain)
    assert evidence['conveyed_sections']


def test_unknown_legacy_content_is_not_invented():
    assert legacy({'active':[{'key':'term:tamworth'}]}, {'transmitted_text':'Old unstructured message'})=={}


@pytest.mark.asyncio
async def test_legacy_flat_message_reconstructs_comparison_without_rewriting_history(db):
    await run(db)
    old=last(db)['id'];old_text=last(db)['transmitted_text']
    with db.transaction('test pre-information ledger') as conn:
        rows=conn.execute("SELECT id,payload FROM bom_scope_events WHERE kind='announcement'").fetchall()
        for row in rows:
            payload=json.loads(row['payload'])
            for key in ('information','information_version','baseline','changed_scopes'): payload.pop(key,None)
            conn.execute('UPDATE bom_scope_events SET payload=? WHERE id=?',(json.dumps(payload),row['id']))
    assert not (await run(db,locations='Tamworth, Quirindi, Sydney',headline='Refreshed headline'))[0]
    assert last(db)['id']==old and last(db)['transmitted_text']==old_text


@pytest.mark.asyncio
async def test_restart_preserves_information_and_suppression(tmp_path):
    from app.db import Database
    path=str(tmp_path/'information.db')
    instance=Database(path);instance.set_setting(KEY,configuration(location_terms=['Tamworth']))
    await run(instance,locations='Tamworth, Sydney');text=last(instance)['transmitted_text'];instance.close()
    instance=Database(path)
    try:
        assert not (await run(instance,locations='Tamworth, Newcastle'))[0]
        assert last(instance)['transmitted_text']==text
    finally:instance.close()


def test_concurrent_useful_update_is_reserved_once(tmp_path):
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    from app.db import Database
    path=str(tmp_path/'concurrent-information.db')
    instance=Database(path);instance.set_setting(KEY,configuration(location_terms=['Tamworth']))
    asyncio.run(run(instance,locations='Tamworth'));instance.close()
    def update(index):
        database=Database(path)
        try:
            return asyncio.run(run(database,locations='Tamworth, Sydney',summary='Destructive winds expected.',
                                   headline=f'Changed publication {index}'))[0]
        finally:database.close()
    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes=list(pool.map(update,range(4)))
    assert outcomes.count(True)==1
    instance=Database(path)
    try:assert len(instance.query_service_history())==2
    finally:instance.close()


@pytest.mark.asyncio
async def test_reprocess_preview_obeys_suppression_and_resend_remains_explicit(environment):
    app=environment;database=app.state.db
    database.set_setting(KEY,configuration(location_terms=['Tamworth']))
    await run(database,locations='Tamworth, Sydney')
    sent,raw,_=await run(database,locations='Tamworth, Newcastle')
    assert not sent
    database.replace_bom_current([raw],{'NSW'},'2026-11-15T08:00:00+00:00')
    changes=database._conn.total_changes
    preview=await app.state.troubleshooting.preview('bom','reprocess')
    assert not preview['errors'] and preview['notices']==0
    assert any('no additional information' in x['reason'] for x in preview['exclusions'])
    assert database._conn.total_changes==changes
    app.state.troubleshooting.start(preview['token']);await app.state.troubleshooting.task
    assert app.state.troubleshooting.job['status']=='completed'
    assert len(database.query_service_history())==1
    assert not app.state.tx.sent
    resend=await app.state.troubleshooting.preview('bom','resend')
    assert not resend['errors'] and resend['notices']==1
    assert 'RESEND' in resend['details'][0]['text']

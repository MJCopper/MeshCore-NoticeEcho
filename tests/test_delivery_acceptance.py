"""Shared acceptance/evidence contract, counter diagnostics and retry identity."""
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from meshcore import EventType

from app.db import Database
from app.delivery import remaining_parts
from app.transmission import TxResult, aggregate_parts, channel_payload
from app.transmit import TransmitManager
from app.web.routes import router
from app.web.troubleshooting_routes import router as troubleshooting_router
from test_transmission_reliability import make_radio, manager, history, received, SECRET, FAST


@pytest.mark.parametrize('outcome,accepted,confirmed,retry', [
    ('accepted',True,False,False), ('local_confirmed',False,True,False),
    ('repeat_confirmed',False,True,False), ('unconfirmed',False,False,True),
    ('rejected',False,False,False), ('not_attempted',False,False,False),
])
def test_evidence_and_retry_classification(outcome,accepted,confirmed,retry):
    result=TxResult(outcome,submitted=True)
    assert result.accepted==accepted and result.confirmed==confirmed and result.retry_eligible==retry
    assert TxResult.legacy(True).outcome=='accepted' and not TxResult.legacy(True).confirmed
    assert TxResult('unconfirmed',submitted=True,accepted=True).outcome=='accepted'


@pytest.mark.parametrize('statuses,expected', [
    (['accepted'],'accepted'), (['accepted','local_confirmed'],'accepted'),
    (['accepted','repeat_confirmed'],'accepted'), (['accepted','unconfirmed'],'unconfirmed'),
    (['local_confirmed','repeat_confirmed'],'success'), (['repeat_confirmed']*2,'repeat_confirmed'),
    (['accepted','pending'],'queued'), (['accepted','rejected'],'failed'),
])
def test_notice_aggregation(statuses,expected):
    assert aggregate_parts([dict(status=s) for s in statuses])==expected


@pytest.mark.asyncio
@pytest.mark.parametrize('counter',['unavailable','missing','flat','reset'])
async def test_accepted_parts_never_consume_uncertainty_retry_budget(counter):
    db=Database(':memory:');radio,commands=make_radio(counter=counter);tx,t=manager(db,radio)
    row=history(db,1)
    result,_=await tx._try_send(t,'warning',0,policy=replace(FAST,retry_unconfirmed=True),context=(row,0,1))
    db.update_service_history(row,'success')
    assert result.outcome=='accepted' and len(commands.calls)==1
    assert not db.uncertainty_retry_used((row,0,1)) and not db.recent_errors()
    assert not t.error and db.get_transmit_log(result.log_id)['success'] is None
    saved=db.latest_service_history('rfs','test')
    assert saved['transmit_status']=='accepted' and remaining_parts(saved,saved['transmitted_text'],['1/1 part'])==[]
    assert not db.successful_service_activity(datetime.now(timezone.utc))
    db.close()


@pytest.mark.asyncio
async def test_acceptance_is_persisted_before_observation_completes():
    radio,commands=make_radio(counter='flat');observed=[]
    def begin(result):
        result.listeners.append(lambda r: observed.append((r.outcome,r.elapsed_seconds)))
    radio.on_submission=begin
    result=await radio.send_text('warning',0)
    assert observed[0]==('accepted',0.0) and result.outcome=='accepted'


@pytest.mark.asyncio
async def test_automatic_retry_reuses_wire_identity_and_repeat_is_counted_once():
    db=Database(':memory:');radio,commands=make_radio(response='timeout');tx,t=manager(db,radio);row=history(db,1)
    result,_=await tx._try_send(t,'warning',0,policy=replace(FAST,retry_unconfirmed=True),context=(row,0,1))
    assert len(commands.calls)==2 and commands.calls[0]==commands.calls[1]
    logs=db.query_transmit_log();evidence=[json.loads(log['evidence']) for log in logs]
    assert len({e['packet_id'] for e in evidence})==1 and len({e['logical_id'] for e in evidence})==1
    db.update_service_history(row,'success')
    packet=received(channel_payload(SECRET,'NoticeEcho','warning',result.timestamp))
    for count in (1,2):
        await radio.repeat_tracker.receive(packet)
        assert all(json.loads(log['evidence'])['repeats']==count for log in db.query_transmit_log())
        assert json.loads(db.latest_service_history('rfs','test')['delivery_parts'])[0]['repeats']==count
    assert db.latest_service_history('rfs','test')['transmit_status']=='repeat_confirmed'
    db.close()


@pytest.mark.asyncio
async def test_statistics_budget_is_shared_across_uncertainty_retry():
    db=Database(':memory:');radio,commands=make_radio(response='timeout',counter='flat');tx,t=manager(db,radio)
    result,_=await tx._try_send(t,'warning',0,policy=replace(FAST,retry_unconfirmed=True))
    assert len(commands.calls)==2 and commands.stats_calls==3
    assert result.counter_reads_used==3 and result.counter_before is None
    assert result.counter_status=='budget_exhausted' and result.outcome=='unconfirmed'
    db.close()


@pytest.mark.asyncio
async def test_key_change_suppresses_uncertain_retry_before_any_command_bytes():
    db=Database(':memory:');radio,commands=make_radio(response='timeout');tx,t=manager(db,radio)
    original=commands.get_channel
    async def channel(index):
        response=await original(index)
        if commands.calls:response.payload['channel_secret']=b'z'*16
        return response
    commands.get_channel=channel
    result,_=await tx._try_send(t,'warning',0,policy=replace(FAST,retry_unconfirmed=True))
    assert result.outcome=='unconfirmed' and len(commands.calls)==1
    assert 'identity' in result.detail and len(db.query_transmit_log())==1
    db.close()


@pytest.mark.asyncio
async def test_sdk_without_explicit_timestamps_cannot_automatically_retry():
    db=Database(':memory:');radio,commands=make_radio();tx,t=manager(db,radio);sent=[]
    async def old_send(channel,text):sent.append((channel,text));return None
    commands.send_chan_msg=old_send
    result,_=await tx._try_send(t,'warning',0,policy=replace(FAST,retry_unconfirmed=True))
    assert len(sent)==1 and result.outcome=='unconfirmed' and 'identity' in result.detail
    db.close()


@pytest.mark.asyncio
async def test_statistics_timeout_is_diagnostic_and_quarantines_late_samples():
    radio,commands=make_radio(counter='flat')
    original=radio._bounded_counter
    async def bounded(timeout,**kwargs):return await original(.005,**kwargs)
    radio._bounded_counter=bounded
    async def stuck():await asyncio.sleep(10)
    commands.get_stats_packets=stuck
    result=await radio.send_text('warning',0)
    assert result.outcome=='accepted' and result.counter_status=='timed_out'
    assert len(result.counter_samples)==1 and not radio._counter_session_trusted
    async def good():return SimpleNamespace(type=EventType.STATS_PACKETS,payload={'flood_tx':123})
    commands.get_stats_packets=good
    probe=await radio.check_local_statistics()
    assert probe['value']==123 and not probe['verification_usable'] and len(commands.calls)==1
    later=await radio.send_text('another warning',0)
    assert later.outcome=='accepted' and later.counter_before is None and later.counter_status=='session_untrusted'
    assert 'reconnect' in later.counter_error and not radio.transmission_diagnostics()['counter_verification_usable']


@pytest.mark.asyncio
async def test_late_generic_error_after_stats_timeout_is_not_a_definite_send_rejection():
    radio,commands=make_radio()
    radio._counter_session_trusted=False
    async def late_error(channel,text,timestamp=None):return SimpleNamespace(type=EventType.ERROR,payload={'error_code':1})
    commands.send_chan_msg=late_error
    assert (await radio.send_text('warning',0)).outcome=='unconfirmed'


@pytest.mark.asyncio
@pytest.mark.parametrize('value',[None,True,-1,2**32,'invalid',1.5])
async def test_malformed_counter_values_do_not_invent_rf_activity(value):
    radio,commands=make_radio(counter='flat');calls=[]
    async def invalid():calls.append(1);return SimpleNamespace(type=EventType.STATS_PACKETS,payload={'flood_tx':value})
    commands.get_stats_packets=invalid
    first=await radio.send_text('warning',0)
    second=await radio.send_text('next warning',0)
    assert first.outcome==second.outcome=='accepted' and first.counter_before is None
    assert len(calls)==1 and second.counter_status=='cooldown'
    assert first.counter_error


@pytest.mark.asyncio
async def test_counters_are_not_compared_after_a_later_transmission():
    radio,commands=make_radio(counter='flat')
    first=await radio.send_text('first',0)
    await radio.send_text('second',0)
    commands.value+=10
    before=len(first.counter_samples)
    await radio.refresh_confirmation(first,.01)
    assert first.outcome=='accepted' and len(first.counter_samples)==before


@pytest.mark.asyncio
async def test_reconnect_resets_capability_and_discards_in_flight_counter_sample():
    radio,commands=make_radio(counter='flat')
    radio._counter_session_trusted=False;radio._counter_retry_at=999999999999
    radio.counter_capability='unsupported';old=radio._counter_session
    radio._reset_counter_session()
    assert radio._counter_session_trusted and radio._counter_retry_at==0
    assert radio.counter_capability=='unknown' and radio._counter_session!=old
    original=commands.get_stats_packets
    async def reconnect_during_read():
        response=await original();radio._reset_counter_session();return response
    commands.get_stats_packets=reconnect_during_read
    result=await radio.send_text('warning',0)
    assert result.outcome=='accepted' and result.counter_before is None
    assert result.counter_status=='session_changed' and 'discarded' in result.counter_error


@pytest.mark.asyncio
async def test_accepted_part_survives_restart_and_late_repeat_upgrades_it(tmp_path):
    path=str(tmp_path/'accepted.db');db=Database(path);radio,commands=make_radio();tx,t=manager(db,radio);row=history(db,1)
    result,_=await tx._try_send(t,'warning',0,policy=FAST,context=(row,0,1))
    # Simulate stopping after durable acceptance but before the normal callback.
    db.close();db=Database(path)
    assert db.latest_service_history('rfs','test')['transmit_status']=='accepted'
    restored=TransmitManager(db)
    await restored._repeat_tracker.receive(received(channel_payload(SECRET,'NoticeEcho','warning',result.timestamp)))
    assert db.latest_service_history('rfs','test')['transmit_status']=='repeat_confirmed'
    db.close()


@pytest.mark.asyncio
async def test_accepted_bom_introduction_keeps_dry_run_and_live_separate():
    from test_bom_episodes import process,Radio,warning,metadata
    db=Database(':memory:');tx=Radio()
    await process(db,tx)
    assert metadata(db)['action']=='NEW' and metadata(db)['mode']=='dry-run'
    await process(db,tx,dry=False)
    assert metadata(db)['action']=='NEW' and metadata(db)['mode']=='live'
    tx.finish(TxResult('accepted'))
    assert not await process(db,tx,dry=False)
    assert await process(db,tx,warning(headline='Updated warning'),dry=False)
    assert metadata(db)['action']=='UPDATE'
    tx.finish(TxResult('accepted'))
    assert await process(db,tx,warning(message_type='Cancel'),dry=False)
    assert metadata(db)['action']=='CANCELLED' and metadata(db)['reference']=='2331-A'
    db.close()


def test_statistics_api_is_read_only_and_shares_busy_guard():
    db=Database(':memory:');radio,commands=make_radio(counter='flat');tx,t=manager(db,radio)
    t.target='/dev/test'
    app=FastAPI();app.state.db=db;app.state.tx=tx;app.include_router(troubleshooting_router)
    before=[tuple(row) for row in db._conn.execute('SELECT * FROM service_history')]
    client=TestClient(app)
    result=client.post('/troubleshoot/radio/statistics')
    assert result.status_code==200 and result.json()['value']==10
    assert result.json()['verification_usable'] and not commands.calls
    tx._active_notice=object()
    assert client.post('/troubleshoot/radio/statistics').status_code==409
    assert [tuple(row) for row in db._conn.execute('SELECT * FROM service_history')]==before
    db.close()


def test_accepted_history_and_counter_diagnostics_render_without_failure_style():
    db=Database(':memory:');row=history(db,1)
    result=TxResult('accepted',counter_status='timed_out',counter_error='Statistics request timed out')
    result.log_id=db.begin_transmission(0,'original text','meshcore',False,(row,0,1))
    db.finish_transmission(result);db.update_service_history(row,'success')
    app=FastAPI();app.state.db=db;app.state.tx=SimpleNamespace();app.include_router(router)
    client=TestClient(app)
    for url in ('/history','/partials/history','/transmit-log','/partials/transmit-log'):
        response=client.get(url)
        assert response.status_code==200 and 'Accepted by companion' in response.text
        assert 'Statistics request timed out' in response.text
        if 'transmit-log' in url:
            assert 'original text' in response.text
    assert db.get_transmit_log(result.log_id)['success'] is None
    db.close()


def test_legacy_transmit_success_does_not_claim_new_counter_observation():
    db=Database(':memory:')
    db._conn.execute("INSERT INTO transmit_log (ts,channel,byte_count,success,text,manual,transport,outcome,evidence) VALUES ('2026-01-01',0,3,1,'old',0,'meshcore','local_confirmed','{}')")
    db._conn.commit()
    app=FastAPI();app.state.db=db;app.state.tx=SimpleNamespace();app.include_router(router)
    response=TestClient(app).get('/transmit-log')
    assert response.status_code==200
    assert 'Completed — legacy success (RF evidence not recorded)' in response.text
    assert 'Local TX activity observed' not in response.text
    db.close()

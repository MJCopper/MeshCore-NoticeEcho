"""Delivery evidence, repeat correlation, multipart recovery and bounded retries."""
import asyncio
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest
from meshcore import EventType
from meshcore.meshcore_parser import MeshcorePacketParser

from app.db import Database
from app.delivery import remaining_parts
from app.transmission import (TransmissionPolicy, TxResult, RepeatTracker, channel_payload,
                              received_channel_payload)
from app.transmit import MeshCoreTransmitter, TransmitManager, TxUnsent

SECRET = bytes(range(16))
FAST = TransmissionPolicy(confirmation_seconds=0.01, retry_delay_seconds=0.02)


def received(payload, path=b'\x12', width=1, scoped=False):
    header = b'\x14' if scoped else b'\x15'
    scope = b'\x00\x00\x00\x00' if scoped else b''
    raw = header + scope + bytes([((width - 1) << 6) | (len(path) // width)]) + path + payload
    return SimpleNamespace(payload={'payload': raw.hex()})


class Commands:
    def __init__(self, radio, *, echo=False, counter='unavailable', response='ok'):
        self.radio = radio
        self.echo = echo
        self.counter = counter
        self.response = response
        self.calls = []
        self.stats_calls = 0
        self.value = 10
        self.after_send = None

    async def get_channel(self, channel):
        return SimpleNamespace(type=EventType.CHANNEL_INFO, payload={'channel_secret': SECRET, 'channel_idx': channel})

    async def get_stats_packets(self):
        self.stats_calls += 1
        if self.counter == 'unavailable':
            return SimpleNamespace(type=EventType.ERROR, payload={'error_code': 1})
        if self.counter == 'missing':
            return SimpleNamespace(type=EventType.OK, payload={})
        return SimpleNamespace(type=EventType.OK, payload={'flood_tx': self.value})

    async def send_chan_msg(self, channel, text, timestamp=None):
        self.calls.append((channel, text, timestamp))
        if self.counter == 'advances':
            self.value += 1
        elif self.counter == 'reset':
            self.value = 0
        if self.echo:
            await self.radio.repeat_tracker.receive(received(channel_payload(SECRET, 'NoticeEcho', text, timestamp)))
        if self.after_send:
            self.after_send()
        if self.response == 'disconnect':
            raise ConnectionError('link lost after writing')
        if self.response == 'reject':
            return SimpleNamespace(type=EventType.ERROR, payload={'reason': 'radio rejected message'})
        if self.response == 'timeout':
            return None
        return SimpleNamespace(type=EventType.OK, payload={})


def make_radio(**kwargs):
    radio = MeshCoreTransmitter('serial', '/dev/test')
    commands = Commands(radio, **kwargs)
    subscriptions = []

    def subscribe(event_type, callback):
        subscription = SimpleNamespace(unsubscribe=lambda: subscriptions.remove(subscription))
        subscriptions.append(subscription)
        return subscription

    radio._mc = SimpleNamespace(commands=commands, is_connected=True, subscribe=subscribe)
    radio._sender_name = 'NoticeEcho'
    radio.policy = FAST
    radio._subscribe_repeats()
    return radio, commands


def manager(db, radio):
    tx = TransmitManager(db)
    tx._policy = lambda: FAST
    transport = tx._transports['meshcore']
    transport.tx, transport.connected = radio, True
    return tx, transport


def history(db, parts=2):
    return db.add_service_history('rfs', 'test', title='Test notice', transmit_status='queued',
                                  transmitted_text=' || '.join(f'{i + 1}/{parts} part' for i in range(parts)), revision_hash='revision')


@pytest.mark.asyncio
@pytest.mark.parametrize('counter,outcome', [('unavailable','unconfirmed'), ('missing','unconfirmed'),
                                            ('flat','unconfirmed'), ('reset','unconfirmed'),
                                            ('advances','local_confirmed')])
async def test_counter_outcomes_are_honest(counter, outcome):
    radio, commands = make_radio(counter=counter)
    result = await radio.send_text('warning', 0)
    assert result.outcome == outcome
    assert bool(result)
    assert result.confirmed == (outcome == 'local_confirmed')
    assert len(commands.calls) == 1
    if counter == 'unavailable':
        assert commands.stats_calls == 1
        assert radio.counter_capability == 'unsupported'
        assert radio.counter_error


@pytest.mark.asyncio
async def test_fast_repeat_confirms_before_command_response_without_counter():
    radio, commands = make_radio(echo=True)
    result = await radio.send_text('warning', 0)
    assert result.outcome == 'repeat_confirmed'
    assert result.repeats == 1
    assert result.counter_before is None
    assert result.packet_id
    assert commands.calls[0][2] == result.timestamp


@pytest.mark.asyncio
@pytest.mark.parametrize('response', ['timeout', 'disconnect'])
async def test_response_interruption_is_ambiguous_and_not_automatically_resent(response):
    db = Database(':memory:')
    radio, commands = make_radio(response=response)
    tx, transport = manager(db, radio)
    result, _ = await tx._try_send(transport, 'warning', 0, policy=FAST)
    assert result.outcome == 'unconfirmed'
    assert len(commands.calls) == 1
    logs = db.query_transmit_log()
    assert len(logs) == 1
    assert logs[0]['success'] is None
    assert logs[0]['outcome'] == 'unconfirmed'


@pytest.mark.asyncio
async def test_rejection_is_not_confirmation_even_if_counter_is_available():
    radio, _ = make_radio(response='reject', counter='advances')
    result = await radio.send_text('warning', 0)
    assert result.outcome == 'rejected'
    assert not result
    assert not radio.repeat_tracker.pending


@pytest.mark.asyncio
@pytest.mark.parametrize('width', [1, 2, 3, 4])
@pytest.mark.parametrize('scoped', [False, True])
async def test_matching_ignores_repeater_path_and_scope_transport(width, scoped):
    payload = channel_payload(SECRET, 'NoticeEcho', 'warning', 123)
    tracker = RepeatTracker()
    result = TxResult('unconfirmed', submitted=True)
    tracker.register(payload, result, 60)
    await tracker.receive(received(payload, b'\x12' * width, width, scoped))
    assert result.confirmed


@pytest.mark.asyncio
async def test_unrelated_old_or_zero_hop_packets_cannot_confirm():
    payload = channel_payload(SECRET, 'NoticeEcho', 'warning', 123)
    tracker = RepeatTracker()
    result = TxResult('unconfirmed', submitted=True)
    tracker.register(payload, result, 60)
    for other in (channel_payload(SECRET, 'NoticeEcho', 'warning', 122),
                  channel_payload(SECRET, 'Other sender', 'warning', 123),
                  channel_payload(bytes(reversed(SECRET)), 'NoticeEcho', 'warning', 123),
                  channel_payload(SECRET, 'NoticeEcho', 'other part', 123)):
        await tracker.receive(received(other))
    await tracker.receive(received(payload, path=b''))
    assert not result.confirmed
    assert result.repeats == 0


@pytest.mark.asyncio
async def test_ambiguous_identical_packet_registration_never_guesses():
    payload = channel_payload(SECRET, 'NoticeEcho', 'warning', 123)
    tracker = RepeatTracker()
    results = [TxResult('unconfirmed', submitted=True) for _ in range(2)]
    for result in results:
        tracker.register(payload, result, 60)
    await tracker.receive(received(payload))
    assert all(not result.confirmed for result in results)


@pytest.mark.asyncio
async def test_expired_tracking_and_malformed_packets_are_ignored():
    tracker = RepeatTracker(limit=2)
    result = TxResult('unconfirmed', submitted=True)
    payload = channel_payload(SECRET, 'NoticeEcho', 'warning', 123)
    tracker.register(payload, result, -1)
    await tracker.receive(received(payload))
    assert not result.confirmed
    for raw in ('', 'zz', '1501', '1502ff', 'd501ff' + payload.hex()):
        assert received_channel_payload({'payload': raw}) is None
    for i in range(5):
        tracker.register(payload, TxResult('unconfirmed'), 60)
    assert len(tracker.pending) == 2


@pytest.mark.asyncio
async def test_full_ciphertext_matching_works_with_real_sdk_parser():
    payload = channel_payload(SECRET, 'NoticeEcho', 'Unicode warning 暴風', 123)
    parser = MeshcorePacketParser()
    parser.decrypt_channels = True
    await parser.newChannel({'channel_idx':0, 'channel_secret':SECRET,
                             'channel_hash':payload[:1].hex(), 'channel_name':'Alerts'})
    event = received(payload)
    decoded = await parser.parsePacketPayload(bytes.fromhex(event.payload['payload']), {})
    assert decoded['sender_timestamp'] == 123
    assert decoded['message'] == 'NoticeEcho: Unicode warning 暴風'
    assert received_channel_payload(decoded) == payload


@pytest.mark.asyncio
async def test_unconfirmed_first_part_does_not_cancel_second_and_history_is_truthful():
    db = Database(':memory:')
    db.set_setting('dry_run', False)
    row = history(db)
    radio, commands = make_radio()
    tx, _ = manager(db, radio)
    outcomes = []

    def result(index, outcome, error):
        outcomes.append(outcome.outcome)
        db.record_delivery_part(row, index, 2, outcome, error)
        if index == 1:
            db.update_service_history(row, 'success')
            tx._stopped = True

    assert tx.enqueue_notice([('1/2 part',0), ('2/2 part',0)], result,
                             delivery_context=(row,(0,1),2))
    await asyncio.wait_for(tx._worker(), 1)
    assert [call[1] for call in commands.calls] == ['1/2 part','2/2 part']
    assert outcomes == ['unconfirmed', 'unconfirmed']
    saved = db.latest_service_history('rfs', 'test')
    assert saved['transmit_status'] == 'unconfirmed'
    assert remaining_parts(saved, saved['transmitted_text'], ['1/2 part','2/2 part']) == []
    assert all(log['success'] is None for log in db.query_transmit_log())


@pytest.mark.asyncio
async def test_optional_retry_once_before_next_part_and_budget_is_persisted():
    db = Database(':memory:')
    radio, commands = make_radio()
    tx, transport = manager(db, radio)
    row = history(db)
    policy = replace(FAST, retry_unconfirmed=True)
    result, _ = await tx._try_send(transport, '1/2 part', 0, policy=policy, context=(row,0,2))
    assert result.outcome == 'unconfirmed'
    assert len(commands.calls) == 2
    assert db.uncertainty_retry_used((row,0,2))
    part = json.loads(db.latest_service_history('rfs','test')['delivery_parts'])[0]
    assert part['attempts'] == 2
    assert part['uncertainty_retries'] == 1
    assert len(part['attempt_ids']) == 2
    # A recovered caller cannot obtain a fresh uncertainty retry budget.
    await tx._try_send(transport, '1/2 part', 0, policy=policy, context=(row,0,2))
    assert len(commands.calls) == 3
    assert sum(log['uncertainty_retry'] for log in db.query_transmit_log()) == 1


@pytest.mark.asyncio
async def test_late_repeat_cancels_optional_retry_and_updates_database():
    db = Database(':memory:')
    radio, commands = make_radio()
    tx, transport = manager(db, radio)
    row = history(db,1)

    async def echo_later():
        await asyncio.sleep(0.015)
        channel, text, timestamp = commands.calls[0]
        await radio.repeat_tracker.receive(received(channel_payload(SECRET,'NoticeEcho',text,timestamp)))

    task = asyncio.create_task(echo_later())
    result, _ = await tx._try_send(transport, 'warning',0, policy=replace(FAST,retry_unconfirmed=True), context=(row,0,1))
    await task
    assert result.outcome == 'repeat_confirmed'
    assert len(commands.calls) == 1
    db.update_service_history(row,'success')
    assert db.latest_service_history('rfs','test')['transmit_status'] == 'repeat_confirmed'
    assert db.query_transmit_log()[0]['outcome'] == 'repeat_confirmed'


@pytest.mark.asyncio
async def test_late_repeat_after_completion_upgrades_history_and_never_downgrades():
    db = Database(':memory:')
    radio, commands = make_radio()
    tx, transport = manager(db,radio)
    row = history(db,1)
    result,_ = await tx._try_send(transport,'warning',0,policy=FAST,context=(row,0,1))
    db.update_service_history(row,'success')
    assert db.latest_service_history('rfs','test')['transmit_status'] == 'unconfirmed'
    await radio.repeat_tracker.receive(received(channel_payload(SECRET,'NoticeEcho','warning',result.timestamp)))
    assert db.latest_service_history('rfs','test')['transmit_status'] == 'repeat_confirmed'
    weaker = TxResult('local_confirmed')
    db.record_delivery_part(row,0,1,weaker)
    assert json.loads(db.latest_service_history('rfs','test')['delivery_parts'])[0]['status'] == 'repeat_confirmed'
    assert SECRET.hex() not in db.query_transmit_log()[0]['evidence']


@pytest.mark.asyncio
async def test_delayed_counter_recovery_suppresses_retry():
    db = Database(':memory:')
    radio, commands = make_radio(counter='flat')
    tx, transport = manager(db,radio)
    async def advance():
        await asyncio.sleep(0.015)
        commands.value += 1
    task = asyncio.create_task(advance())
    result,_ = await tx._try_send(transport,'warning',0,policy=replace(FAST,retry_unconfirmed=True))
    await task
    assert result.outcome == 'local_confirmed'
    assert len(commands.calls) == 1


@pytest.mark.asyncio
async def test_live_guard_rechecked_after_preparation_and_before_retry():
    db = Database(':memory:')
    radio, commands = make_radio()
    tx, transport = manager(db,radio)
    eligible = [True]
    commands.after_send = lambda: eligible.__setitem__(0,False)
    result,_ = await tx._try_send(transport,'warning',0,policy=replace(FAST,retry_unconfirmed=True), valid_if=lambda:eligible[0])
    assert result.outcome == 'unconfirmed'
    assert len(commands.calls) == 1
    eligible[0] = True
    original = commands.get_channel
    async def change_during_preparation(channel):
        eligible[0] = False
        return await original(channel)
    commands.get_channel = change_during_preparation
    result,_ = await tx._try_send(transport,'new warning',0,policy=FAST,valid_if=lambda:eligible[0])
    assert result.outcome == 'not_attempted'
    assert len(commands.calls) == 1


def test_restart_preserves_submission_uncertainty_and_only_retries_untouched_parts(tmp_path):
    path = str(tmp_path/'delivery.db')
    db = Database(path)
    row = history(db)
    db.begin_transmission(0,'1/2 part','meshcore',False,(row,0,2),policy=FAST)
    db.reserve_uncertainty_retry((row,0,2))
    db.close()
    reopened = Database(path)
    saved = reopened.latest_service_history('rfs','test')
    assert saved['transmit_status'] == 'interrupted'
    assert remaining_parts(saved,saved['transmitted_text'],['1/2 part','2/2 part']) == [1]
    assert reopened.uncertainty_retry_used((row,0,2))
    assert reopened.query_transmit_log()[0]['outcome'] == 'unconfirmed'


def test_legacy_unknown_counter_migrates_without_inventing_confirmation(tmp_path):
    path = str(tmp_path/'legacy.db')
    db = Database(path)
    row = history(db)
    db.record_delivery_part(row,0,2,False,'local TX counter unavailable; transmission cannot be confirmed')
    db.update_service_history(row,'failed')
    db.close()
    reopened = Database(path)
    saved = reopened.latest_service_history('rfs','test')
    assert remaining_parts(saved,saved['transmitted_text'],['1/2 part','2/2 part']) == [1]
    assert json.loads(saved['delivery_parts'])[0]['status'] == 'unconfirmed'


@pytest.mark.asyncio
async def test_manual_test_resend_and_verification_share_evidence_handling():
    db = Database(':memory:')
    db.set_setting('dry_run',False)
    radio, commands = make_radio()
    tx,_ = manager(db,radio)
    assert (await tx.send_manual('manual')).outcome == 'unconfirmed'
    assert (await tx.send_test('test')).outcome == 'unconfirmed'
    assert (await tx.send_to('meshcore','bench'))[0].outcome == 'unconfirmed'
    assert (await tx.resend('meshcore','resend',2))[0].outcome == 'unconfirmed'
    assert [entry[0] for entry in commands.calls] == [0,1,1,2]
    outcomes = []
    def callback(result,error):
        outcomes.append(result.outcome)
        tx._stopped = True
    tx.enqueue_verification('verify',callback)
    await asyncio.wait_for(tx._worker(),1)
    assert outcomes == ['unconfirmed']
    assert all(log['outcome'] == 'unconfirmed' and log['success'] is None for log in db.query_transmit_log())


def test_policy_bounds_and_defaults():
    policy = TransmissionPolicy.from_settings({})
    assert not policy.retry_unconfirmed
    assert policy.repeat_detection
    bounded = TransmissionPolicy.from_settings({'meshcore_confirmation_seconds':float('nan'),
                  'meshcore_retry_delay_seconds':-10,'meshcore_late_repeat_seconds':100000})
    assert bounded.confirmation_seconds == 5
    assert bounded.retry_delay_seconds == 1
    assert bounded.late_repeat_seconds == 300


@pytest.mark.asyncio
async def test_restart_restores_late_repeat_identity_and_updates_original_log(tmp_path):
    path = str(tmp_path/'late-repeat.db')
    db = Database(path)
    radio, _ = make_radio()
    tx, transport = manager(db,radio)
    row = history(db,1)
    result,_ = await tx._try_send(transport,'warning',0,policy=FAST,context=(row,0,1))
    db.update_service_history(row,'success')
    timestamp, log_id = result.timestamp, result.log_id
    db.close()
    reopened = Database(path)
    restored = TransmitManager(reopened)
    assert len(restored._repeat_tracker.pending) == 1
    await restored._repeat_tracker.receive(received(channel_payload(SECRET,'NoticeEcho','warning',timestamp)))
    assert reopened.get_transmit_log(log_id)['outcome'] == 'repeat_confirmed'
    assert reopened.latest_service_history('rfs','test')['transmit_status'] == 'repeat_confirmed'


@pytest.mark.asyncio
async def test_counter_requests_obey_real_elapsed_deadline():
    import time
    radio,_ = make_radio()
    radio.policy = replace(FAST,confirmation_seconds=0.025)
    calls = [0]
    async def stats():
        calls[0] += 1
        if calls[0] == 1:
            return 10
        await asyncio.sleep(10)
    radio._flood_tx = stats
    started = time.monotonic()
    result = await radio.send_text('warning',0)
    assert result.outcome == 'unconfirmed'
    assert time.monotonic()-started < 0.2
    assert radio.counter_error == 'statistics request timed out'


@pytest.mark.asyncio
async def test_explicit_invalid_channel_stops_remaining_parts(monkeypatch):
    db = Database(':memory:')
    db.set_setting('dry_run',False)
    radio,commands = make_radio()
    async def reject(channel,text,timestamp=None):
        commands.calls.append((channel,text,timestamp))
        return SimpleNamespace(type=EventType.ERROR,payload={'error_code':2})
    commands.send_chan_msg = reject
    async def missing_channel(channel):
        return SimpleNamespace(type=EventType.ERROR,payload={'error_code':2})
    commands.get_channel=missing_channel
    tx,_ = manager(db,radio)
    outcomes = []
    def callback(index,result,error):
        outcomes.append(result.outcome)
        if index == 1:
            tx._stopped=True
    tx.enqueue_notice([('1/2 part',0),('2/2 part',0)],callback)
    async def sleep(seconds):
        pass
    monkeypatch.setattr('app.transmit.asyncio.sleep',sleep)
    await tx._worker()
    assert len(commands.calls) == 1
    assert outcomes == ['rejected','not_attempted']


def test_queued_and_recovered_notice_keep_original_policy():
    db=Database(':memory:')
    tx=TransmitManager(db)
    row=history(db)
    assert tx.enqueue_notice([('first',0)],delivery_context=(row,(0,),2))
    assert not tx._queue[0].policy.retry_unconfirmed
    db.set_setting('meshcore_retry_unconfirmed',True)
    assert not tx._queue[0].policy.retry_unconfirmed
    db.begin_transmission(0,'first','meshcore',False,(row,0,2),policy=FAST)
    assert tx.enqueue_notice([('second',0)],delivery_context=(row,(1,),2))
    assert not tx._queue[-1].policy.retry_unconfirmed


def test_troubleshooting_replay_preserves_durable_delivery_context_and_guard():
    from app.troubleshooting import ReplayTx
    from app.delivery import submit_notice
    db=Database(':memory:')
    tx=TransmitManager(db)
    row=history(db)
    job={'cancel_requested':False}
    replay=ReplayTx(tx,job)
    assert submit_notice(replay,['first','second'],lambda *_:None,
                         delivery_context=(row,(0,1),2))
    notice=tx._queue[0]
    assert notice.delivery_context == (row,(0,1),2)
    assert notice.valid_if()
    job['cancel_requested']=True
    assert not notice.valid_if()


class CaptureTx:
    message_budget=126
    queue_depth=0
    supports_notice_guards=True
    supports_delivery_context=True
    def __init__(self):
        self.sent=[]
    def enqueue_notice(self,parts,on_result=None,priority=3,valid_if=None,delivery_context=None):
        self.sent.append((parts,on_result,delivery_context))
        return True
    def enqueue_verification(self,*args,**kwargs):
        return True


@pytest.mark.asyncio
@pytest.mark.parametrize('source',['bom','rfs','traffic'])
async def test_service_callbacks_persist_uncertainty_and_subsequent_polls_do_not_resend(source):
    from datetime import datetime,timedelta,timezone
    from app.filters import FilterRules
    from app.poller import BomPoller
    from app.rfs.feed import Incident
    from app.rfs.poller import RFSPoller
    from app.traffic.feed import TrafficItem
    from app.traffic.poller import TrafficPoller
    db=Database(':memory:')
    db.set_setting('dry_run',False)
    db.set_setting(source+'_enabled',True)
    db.set_setting(source+'_all_councils',True)
    db.set_setting('rfs_levels',['Advice'])
    db.set_setting('traffic_baseline_done',True)
    tx=CaptureTx()
    if source=='bom':
        item={'id':'warning','event':'Severe Thunderstorm Warning','region':'NSW','headline':'Storm','area_desc':'Hunter',
              'effective':'','expires':(datetime.now(timezone.utc)+timedelta(days=1)).isoformat(),'message_type':'Alert'}
        poller=BomPoller(db,tx)
        async def poll():
            await poller._process(item,FilterRules.from_settings(db.all_settings()),'Australia/Sydney',0,False)
    else:
        item=(Incident('fire','Grass Fire','Advice','Central Coast','Example Road','Being controlled','Grass Fire','','')
              if source=='rfs' else TrafficItem('incident:1','incident','CRASH','Crash','Example Road','Gosford','Central Coast',
                       'Northbound','Road closed','Avoid the area',None,None,None,None,False,None))
        class Client:
            async def fetch(self):
                return [item]
            async def boundaries(self):
                return []
        poller=RFSPoller(db,tx,Client()) if source=='rfs' else TrafficPoller(db,tx,Client())
        poll=poller.poll_once
    await poll()
    assert len(tx.sent)==1
    parts,callback,context=tx.sent[0]
    assert context is not None
    for index in range(len(parts)):
        callback(index,TxResult('unconfirmed',submitted=True,accepted=True),'local TX counter unavailable')
    saved=db.query_service_history(source=source)[0]
    assert saved['transmit_status']=='unconfirmed'
    assert all(part['status']=='unconfirmed' for part in saved['delivery_parts'])
    await poll()
    assert len(tx.sent)==1
    assert not db.recent_errors()


@pytest.mark.asyncio
async def test_subscription_cleanup_and_reconnect_share_tracker(monkeypatch):
    radio,_=make_radio()
    payload=channel_payload(SECRET,'NoticeEcho','warning',123)
    result=TxResult('unconfirmed',submitted=True)
    radio.repeat_tracker.register(payload,result,60)
    async def disconnect():
        pass
    radio._mc.disconnect=disconnect
    await radio.close()
    assert not radio._subscriptions
    assert not radio.connected
    assert len(radio.repeat_tracker.pending)==1
    db=Database(':memory:')
    tx=TransmitManager(db)
    async def connect():
        pass
    fresh,_=make_radio()
    fresh.connect=connect
    transport=tx._transports['meshcore']
    transport.make=lambda:fresh
    assert await tx._open_once(transport)==''
    assert fresh.repeat_tracker is tx._repeat_tracker


@pytest.mark.asyncio
async def test_connection_settings_save_validate_and_render_confirmation_controls():
    from fastapi import FastAPI
    import httpx
    from app.poller import BomPoller
    from app.web.routes import router
    db=Database(':memory:')
    tx=TransmitManager(db)
    called=[]
    async def reconfigure():
        called.append(True)
    tx.reconfigure=reconfigure
    app=FastAPI()
    app.state.db,app.state.tx,app.state.poller=db,tx,BomPoller(db,tx)
    app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        page=await client.get('/settings/meshcore')
        assert page.status_code==200
        assert 'Retry unconfirmed transmissions once' in page.text
        before=db.all_settings()
        invalid=await client.post('/settings/meshcore',data={'meshcore_confirmation_seconds':'31'})
        assert invalid.status_code==422
        assert db.all_settings()==before and not called
        saved=await client.post('/settings/meshcore',data={'meshcore_repeat_detection':'1',
                  'meshcore_confirmation_seconds':'8','meshcore_retry_unconfirmed':'1',
                  'meshcore_retry_delay_seconds':'4','meshcore_late_repeat_seconds':'90'})
        assert saved.status_code==303
        assert db.get_setting('meshcore_confirmation_seconds')==8
        assert db.get_setting('meshcore_retry_unconfirmed')
        assert db.get_setting('meshcore_repeat_detection')
        assert len(called)==1
        log=TxResult('unconfirmed',submitted=True,detail='Counter unavailable')
        db.add_transmit_log(0,7,log,'warning')
        response=await client.get('/transmit-log')
        assert response.status_code==200
        assert 'Unconfirmed' in response.text
        assert 'Heard repeats:' in response.text
        assert 'Counter unavailable' in response.text
        dashboard=await client.get('/')
        assert dashboard.status_code==200
        assert 'Unconfirmed -' in dashboard.text


@pytest.mark.asyncio
async def test_uncertainty_retry_preserves_complete_multipart_order():
    db=Database(':memory:')
    db.set_setting('dry_run',False)
    radio,commands=make_radio()
    tx,_=manager(db,radio)
    tx._policy=lambda:replace(FAST,retry_unconfirmed=True)
    def callback(index,result,error):
        if index==1:
            tx._stopped=True
    tx.enqueue_notice([('1/2 part',0),('2/2 part',0)],callback)
    await asyncio.wait_for(tx._worker(),1)
    assert [call[1] for call in commands.calls]==['1/2 part','1/2 part','2/2 part','2/2 part']
    assert sum(log['uncertainty_retry'] for log in db.query_transmit_log())==2


@pytest.mark.asyncio
async def test_database_intent_failure_is_before_submission_not_uncertain(monkeypatch):
    db=Database(':memory:')
    radio,commands=make_radio()
    tx,transport=manager(db,radio)
    def unavailable(*args,**kwargs):
        raise RuntimeError('database write unavailable')
    monkeypatch.setattr(db,'begin_transmission',unavailable)
    result,error=await tx._try_send(transport,'warning',0,policy=FAST)
    assert result.outcome=='not_attempted'
    assert 'before sending' in error
    assert not commands.calls


@pytest.mark.asyncio
async def test_transient_missing_baseline_is_retried_before_submission():
    radio,commands=make_radio(counter='advances')
    original=commands.get_stats_packets
    calls=[0]
    async def transient():
        calls[0]+=1
        if calls[0]==1:
            return SimpleNamespace(type=EventType.OK,payload={})
        return await original()
    commands.get_stats_packets=transient
    result=await radio.send_text('warning',0)
    assert result.outcome=='local_confirmed'
    assert calls[0]==3
    assert len(commands.calls)==1


@pytest.mark.asyncio
async def test_dry_run_change_between_parts_stops_remaining_submission():
    db=Database(':memory:')
    db.set_setting('dry_run',False)
    radio,commands=make_radio()
    tx,_=manager(db,radio)
    outcomes=[]
    def callback(index,result,error):
        outcomes.append(result.outcome)
        if index==0:
            db.set_setting('dry_run',True)
        else:
            tx._stopped=True
    tx.enqueue_notice([('1/2 part',0),('2/2 part',0)],callback)
    # Worker failure backoff is unnecessary once this controlled test is stopped.
    original=asyncio.sleep
    async def sleep(seconds):
        if not tx._stopped:
            await original(seconds)
    from unittest.mock import patch
    with patch('app.transmit.asyncio.sleep',sleep):
        await asyncio.wait_for(tx._worker(),1)
    assert outcomes==['unconfirmed','not_attempted']
    assert len(commands.calls)==1


def test_restart_after_rejected_retry_preserves_original_submission_uncertainty(tmp_path):
    path=str(tmp_path/'rejected-retry.db')
    db=Database(path)
    row=history(db)
    first=TxResult('unconfirmed',submitted=True)
    first.log_id=db.begin_transmission(0,'1/2 part','meshcore',False,(row,0,2),policy=FAST)
    db.finish_transmission(first)
    retry=TxResult('rejected',detail='Retry explicitly rejected')
    retry.log_id=db.begin_transmission(0,'1/2 part','meshcore',False,(row,0,2),retry=True,policy=FAST)
    db.finish_transmission(retry)
    # Crash before the queue callback reconciles the previous ambiguous attempt.
    db.close()
    reopened=Database(path)
    saved=reopened.latest_service_history('rfs','test')
    assert remaining_parts(saved,saved['transmitted_text'],['1/2 part','2/2 part'])==[1]
    assert reopened.uncertainty_retry_used((row,0,2))
    assert reopened.query_transmit_log()[0]['outcome']=='rejected'
    assert json.loads(saved['delivery_parts'])[0]['status']=='unconfirmed'


@pytest.mark.asyncio
async def test_exact_repeat_wins_over_contradictory_command_error():
    radio, commands = make_radio(echo=True, response='reject')
    result = await radio.send_text('warning', 0)
    assert result.outcome == 'repeat_confirmed'
    assert result.confirmed
    assert 'contradictory' in result.detail
    assert len(commands.calls) == 1


@pytest.mark.parametrize('statuses,expected', [
    (['local_confirmed', 'local_confirmed'], 'success'),
    (['repeat_confirmed', 'repeat_confirmed'], 'repeat_confirmed'),
    (['local_confirmed', 'repeat_confirmed'], 'success'),
    (['local_confirmed', 'unconfirmed'], 'unconfirmed'),
])
def test_restart_recovers_completed_aggregate_without_downgrading_evidence(tmp_path, statuses, expected):
    path = str(tmp_path / 'aggregate.db')
    db = Database(path)
    row = history(db)
    db._conn.execute("UPDATE service_history SET delivery_parts=? WHERE id=?",
                     (json.dumps([{'status': status} for status in statuses]), row))
    db._conn.commit()
    db.close()
    reopened = Database(path)
    recovered = reopened.latest_service_history('rfs', 'test')
    assert recovered['transmit_status'] == expected
    assert bool(recovered['transmitted_at']) == (expected != 'unconfirmed')
    reopened.close()


@pytest.mark.asyncio
async def test_restart_restores_active_repeat_window_even_when_log_is_older_than_300_seconds(tmp_path):
    import time
    path = str(tmp_path / 'long-window.db')
    db = Database(path)
    radio, _ = make_radio()
    tx, transport = manager(db, radio)
    result, _ = await tx._try_send(transport, 'warning', 0, policy=FAST)
    evidence = json.loads(db.get_transmit_log(result.log_id)['evidence'])
    evidence['repeat_expires_at'] = time.time() + 10
    db._conn.execute("UPDATE transmit_log SET ts='2000-01-01T00:00:00+00:00', evidence=? WHERE id=?",
                     (json.dumps(evidence), result.log_id))
    db._conn.commit()
    db.close()
    reopened = Database(path)
    restored = TransmitManager(reopened)
    assert len(restored._repeat_tracker.pending) == 1
    await restored._repeat_tracker.receive(received(channel_payload(SECRET, 'NoticeEcho', 'warning', result.timestamp)))
    assert reopened.get_transmit_log(result.log_id)['outcome'] == 'repeat_confirmed'
    reopened.close()

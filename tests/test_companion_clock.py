import asyncio
import json
import time
from types import SimpleNamespace
import pytest
from meshcore import EventType
from app.companion_clock import ClockSync
from app.db import Database
from app.transmit import TransmitManager


def setup(offset=0, response='ok', apply=True):
    db=Database(':memory:')
    clock=ClockSync(db)
    calls=[]
    origin=[time.monotonic(), int(time.time())+offset]
    async def read():
        calls.append('read')
        return SimpleNamespace(type=EventType.CURRENT_TIME,payload={'time':int(origin[1]+time.monotonic()-origin[0])})
    async def write(value):
        calls.append(('write',value))
        if apply and response != 'unsupported': origin[:]=[time.monotonic(),value]
        if response=='timeout':raise TimeoutError()
        if response=='none':return None
        if response=='unneeded':return SimpleNamespace(type=EventType.ERROR,payload={'error_code':6})
        if response=='unsupported':return SimpleNamespace(type=EventType.ERROR,payload={'error_code':1})
        return SimpleNamespace(type=EventType.OK,payload={})
    radio=SimpleNamespace(_mc=SimpleNamespace(commands=SimpleNamespace(get_time=read,set_time=write)))
    return db,clock,radio,calls


@pytest.mark.asyncio
@pytest.mark.parametrize('offset',[-100,100])
async def test_automatic_corrects_both_directions_and_verifies(offset):
    db,clock,radio,calls=setup(offset)
    result=await clock.check(radio)
    assert result['status']=='verified sync'
    assert len(calls)==3
    assert abs(result['drift_seconds'])<2
    assert db.get_setting('meshcore_clock_state')['last_sync']


@pytest.mark.asyncio
async def test_clock_within_tolerance_is_not_written():
    db,clock,radio,calls=setup(2)
    result=await clock.check(radio)
    assert result['status']=='within tolerance' and calls==['read']


@pytest.mark.asyncio
@pytest.mark.parametrize('response',['unneeded','timeout','none'])
@pytest.mark.parametrize('apply',[True,False])
async def test_write_response_does_not_replace_verified_evidence(response,apply):
    db,clock,radio,calls=setup(100,response,apply)
    result=await clock.check(radio,force=True)
    assert result['status']==('verified sync' if apply else 'failed')
    assert len([x for x in calls if isinstance(x,tuple)])==1
    assert bool(result.get('last_sync'))==apply


@pytest.mark.asyncio
@pytest.mark.parametrize('value',[None,True,'123',-1,2**32])
async def test_malformed_clock_payload_never_written(value):
    db,clock,radio,calls=setup()
    async def read():return SimpleNamespace(type=EventType.CURRENT_TIME,payload={'time':value})
    radio._mc.commands.get_time=read
    result=await clock.check(radio)
    assert result['status']=='failed' and not calls


@pytest.mark.asyncio
async def test_unsupported_backoff_and_repeated_error_logging():
    db,clock,radio,calls=setup(100,'unsupported')
    first=await clock.check(radio)
    assert first['status']=='unsupported'
    events=db._conn.execute('select count(*) from events').fetchone()[0]
    await clock.check(radio)
    assert clock.due-time.monotonic()>3500
    assert db._conn.execute('select count(*) from events').fetchone()[0]==events
    radio._mc.commands.get_time=None
    assert (await clock.check(radio))['status']=='unsupported'


@pytest.mark.asyncio
async def test_read_only_and_explicit_epoch_with_verification():
    db,clock,radio,calls=setup(100)
    result=await clock.check(radio,read_only=True)
    assert result['status']=='drift detected' and calls==['read']
    assert clock.ready()
    result=await clock.check(radio,target=100000)
    assert result['status']=='verified explicit time'
    assert not result.get('last_sync')
    assert abs(result['companion_epoch']-100000)<2


def test_disabled_stale_restart_policy_and_wall_clock_adjustment(monkeypatch):
    db,clock,radio,calls=setup()
    db.set_setting('meshcore_clock_state',{'status':'verified sync','last_sync':'2026-10-01T00:00:00+00:00'})
    restored=ClockSync(db)
    assert restored.state['status']=='stale'
    assert restored.status(False)['status']=='offline'
    db.set_setting('meshcore_clock_auto_sync',False)
    assert not restored.ready()
    db.set_setting('meshcore_clock_auto_sync',True)
    restored.pending=False;restored.due=time.monotonic()+1000
    restored.anchor=(time.time()-100,time.monotonic())
    assert restored.ready()
    assert restored.state['trigger']=='server clock adjusted'


@pytest.mark.asyncio
async def test_slow_or_adjusted_measurement_is_inconclusive(monkeypatch):
    db,clock,radio,calls=setup()
    actual=clock.read
    async def slow(radio):return dict(await actual(radio),reliable=False,request_seconds=1.9)
    monkeypatch.setattr(clock,'read',slow)
    result=await clock.check(radio)
    assert result['status']=='inconclusive' and calls==['read']


@pytest.mark.asyncio
async def test_read_timeout_is_bounded_and_cancelled():
    db,clock,radio,calls=setup()
    cancelled=[]
    async def stuck():
        try: await asyncio.sleep(10)
        finally:cancelled.append(True)
    radio._mc.commands.get_time=stuck
    clock.READ_TIMEOUT=.01
    result=await clock.check(radio)
    assert result['status']=='failed' and cancelled
    assert not clock.ready()


@pytest.mark.asyncio
async def test_manager_cli_shared_operations_aliases_json_and_notice_lock(monkeypatch):
    db,clock,radio,calls=setup()
    manager=TransmitManager(db)
    manager._clock=clock
    monkeypatch.setattr(manager,'_saved_radio',lambda:radio)
    assert 'Current time:' in await manager.execute_companion_command('clock')
    for command in ['clock sync','st','sync_time']:
        assert 'verified sync' in await manager.execute_companion_command(command)
    data=json.loads(await manager.execute_companion_command('.clock'))
    assert data['time']==data['companion_epoch']
    assert 'restore server time' in await manager.execute_companion_command('time 100000')
    manager._active_notice=object()
    with pytest.raises(RuntimeError,match='busy'):await manager.check_companion_clock(force=True)
    manager._active_notice=None
    async with manager._lock:
        with pytest.raises(RuntimeError,match='busy'):await manager.execute_companion_command('clock sync')


@pytest.mark.asyncio
async def test_scheduler_defers_until_notice_complete_and_stops(monkeypatch):
    db,clock,radio,calls=setup()
    manager=TransmitManager(db);manager._clock=clock
    monkeypatch.setattr(manager,'_saved_radio',lambda:radio)
    real_sleep=asyncio.sleep
    async def short_sleep(seconds):await real_sleep(.001)
    monkeypatch.setattr('app.transmit.asyncio.sleep',short_sleep)
    manager._active_notice=object()
    task=asyncio.create_task(manager._maintain_clock())
    await real_sleep(.005)
    assert not calls
    manager._active_notice=None
    await real_sleep(.02)
    assert calls==['read']
    manager._stopped=True
    await task


@pytest.mark.asyncio
async def test_connection_callbacks_request_check_and_are_cleaned_up(monkeypatch):
    from app.transmit import MeshCoreTransmitter
    db=Database(':memory:');manager=TransmitManager(db)
    radio=MeshCoreTransmitter('serial')
    subscriptions={}
    def subscribe(event,callback):
        subscriptions[event]=callback
        return SimpleNamespace(unsubscribe=lambda:subscriptions.pop(event))
    async def disconnect():pass
    async def connect():radio._mc=SimpleNamespace(is_connected=True,subscribe=subscribe,disconnect=disconnect)
    monkeypatch.setattr(radio,'connect',connect)
    transport=manager._transports['meshcore']
    monkeypatch.setattr(transport,'make',lambda:radio)
    assert await manager._open_once(transport)==''
    assert manager._clock.pending
    manager._clock.pending=False
    await subscriptions[EventType.CONNECTED](SimpleNamespace())
    assert manager._clock.pending
    await subscriptions[EventType.DISCONNECTED](SimpleNamespace())
    assert len(radio._clock_subscriptions)==2
    await radio.close()
    assert not subscriptions


@pytest.mark.asyncio
async def test_reconnect_trigger_arriving_during_check_is_not_lost():
    db,clock,radio,calls=setup()
    original=radio._mc.commands.get_time
    async def read():
        clock.request('reconnected during check')
        return await original()
    radio._mc.commands.get_time=read
    await clock.check(radio)
    assert clock.pending and clock.ready()


@pytest.mark.asyncio
async def test_enabled_disable_change_does_not_require_reconnect(monkeypatch):
    db,clock,radio,calls=setup()
    manager=TransmitManager(db);manager._clock=clock
    db.set_setting('meshcore_clock_auto_sync',False)
    manager.clock_settings_changed()
    assert not manager._clock.ready()
    db.set_setting('meshcore_clock_auto_sync',True)
    manager.clock_settings_changed()
    assert manager._clock.ready()


@pytest.mark.asyncio
async def test_device_read_rejection_is_failed_and_clock_recovery_clears_error():
    db,clock,radio,calls=setup()
    original=radio._mc.commands.get_time
    async def rejected():return SimpleNamespace(type=EventType.ERROR,payload={'error_code':2})
    radio._mc.commands.get_time=rejected
    assert (await clock.check(radio))['status']=='failed'
    radio._mc.commands.get_time=original
    result=await clock.check(radio)
    assert result['status']=='within tolerance' and not result['error']
    assert clock.failures==0


def test_clock_settings_validation_and_clock_only_save_does_not_reconnect():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.web.routes import router
    db=Database(':memory:')
    calls=[]
    async def reconfigure():calls.append('reconnect')
    def changed():calls.append('clock settings')
    app=FastAPI();app.include_router(router)
    app.state.db=db;app.state.tx=SimpleNamespace(reconfigure=reconfigure,clock_settings_changed=changed)
    client=TestClient(app)
    data={'meshcore_repeat_detection':'1','meshcore_clock_auto_sync':'1'}
    assert client.post('/settings/meshcore',data=data,follow_redirects=False).status_code==303
    calls.clear()
    assert client.post('/settings/meshcore',data=data|{'meshcore_clock_interval_minutes':30},follow_redirects=False).status_code==303
    assert calls==['clock settings']
    before=db.all_settings()
    for key,value in [('meshcore_clock_interval_minutes',0),('meshcore_clock_interval_minutes',1441),('meshcore_clock_tolerance_seconds',float('nan')),('meshcore_clock_tolerance_seconds',301)]:
        assert client.post('/settings/meshcore',data=data|{key:value},follow_redirects=False).status_code==422
        assert db.all_settings()==before


def test_clock_buttons_render_and_use_shared_operation():
    from test_meshcore_settings import _web_client
    client,radio=_web_client()
    original=radio.get_device_settings
    async def settings():
        data=await original()
        data['clock']={'status':'within tolerance','automatic':True,'server_time':'2026-10-09T00:00:00+00:00','companion_time':'2026-10-09T00:00:00+00:00',
                       'drift_seconds':0,'uncertainty_seconds':1,'last_check':'2026-10-09T00:00:00+00:00','last_sync':'','next_check':''}
        return data
    calls=[]
    async def check(force=False):
        calls.append(force)
        return {'status':'verified sync','error':''}
    radio.get_device_settings=settings;radio.check_companion_clock=check
    page=client.get('/meshcore/settings')
    assert 'Check now' in page.text and 'Sync now' in page.text and 'Estimated drift' in page.text
    assert client.post('/meshcore/settings/clock',data={'action':'check'},follow_redirects=False).status_code==303
    assert client.post('/meshcore/settings/clock',data={'action':'sync'},follow_redirects=False).status_code==303
    assert calls==[False,True]
    assert client.post('/meshcore/settings/clock',data={'action':'invalid'}).status_code==422


@pytest.mark.asyncio
async def test_midpoint_uncertainty_uses_elapsed_time_not_timezone(monkeypatch):
    import app.companion_clock as module
    db,clock,radio,calls=setup()
    wall=iter([1000.0,1000.4]);mono=iter([200.0,200.4])
    async def read():return SimpleNamespace(type=EventType.CURRENT_TIME,payload={'time':1001})
    radio._mc.commands.get_time=read
    monkeypatch.setattr(module, 'time', SimpleNamespace(time=lambda:next(wall,1000.4), monotonic=lambda:next(mono,200.4)))
    # Avoid wait_for/event loop using the patched monotonic clock.
    async def immediate(operation,timeout):return await operation
    monkeypatch.setattr(module.asyncio,'wait_for',immediate)
    sample=await clock.read(radio)
    assert sample['drift_seconds']==pytest.approx(.8)
    assert sample['uncertainty_seconds']==pytest.approx(1.2)
    assert sample['companion_time'].endswith('+00:00')


@pytest.mark.asyncio
async def test_clock_task_is_cancelled_on_stop(monkeypatch):
    db=Database(':memory:');manager=TransmitManager(db)
    async def wait():await asyncio.sleep(10)
    monkeypatch.setattr(manager,'_worker',wait)
    monkeypatch.setattr(manager,'_maintain_connections',wait)
    manager.start()
    task=manager._clock_task
    await asyncio.sleep(0)
    await manager.stop()
    assert task.cancelled()


@pytest.mark.asyncio
async def test_failure_does_not_change_transport_error_or_queue(monkeypatch):
    db,clock,radio,calls=setup(100,'unsupported')
    manager=TransmitManager(db);manager._clock=clock
    transport=manager._transports['meshcore'];transport.error='existing transport state'
    manager.enqueue_notice([('first',0),('second',0)])
    depth=manager.queue_depth
    monkeypatch.setattr(manager,'_saved_radio',lambda:radio)
    await manager.check_companion_clock(force=True)
    assert transport.error=='existing transport state'
    assert manager.queue_depth==depth


@pytest.mark.asyncio
@pytest.mark.parametrize('offset',[-5,5,8,-8])
async def test_tolerance_boundary_with_rounding_uncertainty(offset):
    db,clock,radio,calls=setup(offset)
    result=await clock.check(radio)
    assert result['status']==('verified sync' if abs(offset)==8 else 'within tolerance')

"""Native local companion CLI: exact text, exclusive requests, isolated replies."""
import asyncio
import logging
from types import SimpleNamespace

import pytest
from meshcore import EventType
from app.companion_cli import ConsoleError, validate
from app.transmit import MeshCoreTransmitter, TransmitManager
from app.db import Database


def radio(reply='  > Node\r\nsecond line\n', version=14):
    calls = []
    async def query():
        return SimpleNamespace(type=EventType.DEVICE_INFO, payload={'fw ver': version})
    async def cli(command):
        calls.append(command)
        return SimpleNamespace(type=EventType.CLI_REPLY, payload={'text': reply})
    async def disconnect():
        calls.append('DISCONNECTED')
    node = SimpleNamespace(is_connected=True, commands=SimpleNamespace(send_device_query=query, run_cli_command=cli), disconnect=disconnect)
    tx = MeshCoreTransmitter('serial'); tx._mc = node
    return tx, calls


@pytest.mark.asyncio
@pytest.mark.parametrize('command', ['get name', 'set name "My node"', 'set name unquoted value',
                                      'custom board command', 'get name > /tmp/file', 'clock',
                                      'time 100000', 'help', 'cli version', '.get radio', '  get name  ',
                                      'get wifi.pwd', 'custom "unterminated', 'set name Café'])
async def test_commands_are_forwarded_exactly_without_parsing(command):
    tx,calls=radio()
    assert await tx.execute_console(command) == '  > Node\r\nsecond line\n'
    assert calls == [command]


@pytest.mark.asyncio
@pytest.mark.parametrize('reply', ['', '\n', 'Unknown command', 'Error: unsupported parameter', 'PIN: 123456\n'+'ab'*16, '<script>alert(1)</script>', 'x'*10000])
async def test_all_device_reply_text_is_preserved(reply):
    tx,calls=radio(reply)
    assert await tx.execute_console('custom command') == reply


@pytest.mark.parametrize('command', ['', '  ', 'get name\nreboot', '\x00get name', 'get\tname', 'get\x7fname', 'x'*201, 'é'*101])
def test_frame_input_validation_only(command):
    with pytest.raises(ValueError): validate(command)
    assert validate('é'*100) == 'é'*100


@pytest.mark.asyncio
@pytest.mark.parametrize('version', [13, None, 'bad'])
async def test_unsupported_firmware_never_submits(version):
    tx,calls=radio(version=version)
    with pytest.raises(ConsoleError,match='unsupported') as exc: await tx.execute_console('get name')
    assert exc.value.state=='unsupported' and not calls


@pytest.mark.asyncio
async def test_unsupported_sdk_and_disconnected_state():
    tx,calls=radio();tx._mc.commands.run_cli_command=None
    with pytest.raises(ConsoleError) as exc: await tx.execute_console('get name')
    assert exc.value.state=='unsupported'
    tx._mc=None
    with pytest.raises(ConsoleError) as exc: await tx.execute_console('get name')
    assert exc.value.state=='disconnected'


@pytest.mark.asyncio
async def test_sdk_without_cli_reply_event_is_unsupported(monkeypatch):
    tx,calls=radio()
    monkeypatch.setattr('meshcore.EventType', SimpleNamespace(DEVICE_INFO=EventType.DEVICE_INFO, ERROR=EventType.ERROR))
    with pytest.raises(ConsoleError) as exc: await tx.execute_console('get name')
    assert exc.value.state=='unsupported' and not calls


@pytest.mark.asyncio
@pytest.mark.parametrize('response,state', [
    (SimpleNamespace(type=EventType.ERROR,payload={'error_code':1}), 'unsupported'),
    (SimpleNamespace(type=EventType.ERROR,payload={'error_code':6,'reason':'invalid input'}), 'device-error'),
    (SimpleNamespace(type=EventType.OK,payload={}), 'protocol-error'),
    (SimpleNamespace(type=EventType.CLI_REPLY,payload={}), 'protocol-error'),
])
async def test_protocol_failures_are_distinct_from_firmware_reply_text(response,state):
    tx,calls=radio()
    async def cli(command): return response
    tx._mc.commands.run_cli_command=cli
    with pytest.raises(ConsoleError) as exc: await tx.execute_console('get name')
    assert exc.value.state==state
    if state=='protocol-error': assert tx._mc is None


@pytest.mark.asyncio
async def test_timeout_drops_old_connection_and_does_not_repeat(monkeypatch):
    tx,calls=radio();old=tx._mc
    pending=asyncio.Event()
    async def stuck(command):
        calls.append(command);pending.set();await asyncio.sleep(10)
    old.commands.run_cli_command=stuck
    monkeypatch.setattr('app.companion_cli.COMMAND_TIMEOUT', .02)
    with pytest.raises(ConsoleError) as exc: await tx.execute_console('reboot')
    assert exc.value.state=='timed-out' and tx._mc is None
    assert calls==['reboot','DISCONNECTED']
    with pytest.raises(ConsoleError): await tx.execute_console('get name')
    assert calls==['reboot','DISCONNECTED']
    fresh,fresh_calls=radio('fresh response');tx._mc=fresh._mc
    assert await tx.execute_console('get name')=='fresh response'
    assert fresh_calls==['get name']


@pytest.mark.asyncio
@pytest.mark.parametrize('reply', [None, SimpleNamespace(type=EventType.ERROR,payload={'reason':'no_event_received'}), SimpleNamespace(type=EventType.ERROR,payload={'error':'link lost'})])
async def test_sdk_missing_response_resets_before_next_request(reply):
    tx,calls=radio()
    async def cli(command): calls.append(command);return reply
    tx._mc.commands.run_cli_command=cli
    with pytest.raises(ConsoleError) as exc: await tx.execute_console('reboot')
    assert exc.value.state=='timed-out' and calls==['reboot','DISCONNECTED']


@pytest.mark.asyncio
async def test_cancellation_releases_lock_and_discards_sdk_session():
    tx,calls=radio();started=asyncio.Event()
    async def cli(command): started.set();await asyncio.sleep(10)
    tx._mc.commands.run_cli_command=cli
    task=asyncio.create_task(tx.execute_console('get name'));await started.wait();task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert tx._mc is None and not tx._send_lock.locked()


@pytest.mark.asyncio
async def test_concurrent_direct_requests_are_busy():
    tx,calls=radio();started=asyncio.Event();release=asyncio.Event()
    async def cli(command): started.set();await release.wait();return SimpleNamespace(type=EventType.CLI_REPLY,payload={'text':'done'})
    tx._mc.commands.run_cli_command=cli
    task=asyncio.create_task(tx.execute_console('first'));await started.wait()
    with pytest.raises(ConsoleError) as exc: await tx.execute_console('second')
    assert exc.value.state=='busy'
    release.set();assert await task=='done'


@pytest.mark.asyncio
async def test_sdk_debug_command_and_reply_are_not_logged(caplog):
    tx,calls=radio()
    async def cli(command):
        logging.getLogger('meshcore').debug(command)
        logging.getLogger('meshcore').warning('private reply: 123456')
        return SimpleNamespace(type=EventType.CLI_REPLY,payload={'text':'PIN: 123456'})
    tx._mc.commands.run_cli_command=cli
    with caplog.at_level(logging.DEBUG):
        assert await tx.execute_console('password secret-value')=='PIN: 123456'
    assert 'secret-value' not in caplog.text and '123456' not in caplog.text
    assert not logging.getLogger('meshcore').filters


@pytest.mark.asyncio
async def test_manager_forwards_clock_without_running_clock_wrapper(monkeypatch):
    db=Database(':memory:');manager=TransmitManager(db);tx,calls=radio('device time')
    monkeypatch.setattr(manager,'_saved_radio',lambda:tx)
    before=[tuple(row) for row in db._conn.execute('SELECT * FROM service_history')]
    result=await manager.execute_companion_command('clock')
    assert calls[0]=='clock' and result['output']=='device time' and result['state']=='reply'
    assert result['refresh_note']  # Mock has no settings reader; response survives.
    assert [tuple(row) for row in db._conn.execute('SELECT * FROM service_history')]==before
    db.close()


@pytest.mark.asyncio
async def test_manager_refresh_keeps_reply_separate_and_recovers_sender(monkeypatch):
    db=Database(':memory:');manager=TransmitManager(db);tx,calls=radio('OK - reboot to apply\n')
    async def self_info():
        assert manager._lock.locked()
        return SimpleNamespace(type=EventType.SELF_INFO,payload={'name':'New sender'})
    tx._mc.commands.send_appstart=self_info
    async def channels(): calls.append('channels')
    monkeypatch.setattr(manager,'_saved_radio',lambda:tx)
    monkeypatch.setattr(manager,'_refresh_saved_channels',channels)
    result=await manager.execute_companion_command('set name New sender')
    assert result==dict(output='OK - reboot to apply\n',state='reply',refresh_required=True,refresh_note='')
    assert tx._sender_name=='New sender' and calls==['set name New sender','channels']
    manager._active_notice=object()
    with pytest.raises(ConsoleError) as exc: await manager.execute_companion_command('get name')
    assert exc.value.state=='busy'
    db.close()


@pytest.mark.asyncio
async def test_actual_sdk_frames_and_delayed_reply_isolation(monkeypatch):
    from meshcore.commands import CommandHandler
    from meshcore.events import Event, EventDispatcher
    from meshcore.packets import CommandType
    tx=MeshCoreTransmitter('serial');frames=[]
    async def session(label):
        dispatcher=EventDispatcher();commands=CommandHandler(default_timeout=.02)
        await dispatcher.start()
        commands.set_dispatcher(dispatcher)
        async def send(data):
            frames.append(bytes(data))
            if data[0]==0x16:
                await dispatcher.dispatch(Event(EventType.DEVICE_INFO,{'fw ver':14}))
            elif label=='fresh':
                await dispatcher.dispatch(Event(EventType.CLI_REPLY,{'text':'new reply'}))
        async def disconnect(): await dispatcher.stop()
        commands._sender_func=send
        return SimpleNamespace(is_connected=True,commands=commands,disconnect=disconnect),dispatcher
    old,old_dispatcher=await session('old');tx._mc=old
    with pytest.raises(ConsoleError): await tx.execute_console('get name')
    assert tx._mc is None
    tx._mc,new_dispatcher=await session('fresh')
    await old_dispatcher.dispatch(Event(EventType.CLI_REPLY,{'text':'late OLD response'}))
    assert await tx.execute_console('get name')=='new reply'
    assert [frame for frame in frames if frame[0]==CommandType.RUN_CLI_COMMAND.value]==[b'\x42get name',b'\x42get name']
    await tx.close()

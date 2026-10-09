import io
from types import SimpleNamespace
import pytest
from meshcore import EventType
from app.companion_cli import execute, parse
from app.transmit import MeshCoreTransmitter


def radio():
    node = SimpleNamespace(is_connected=True, self_info={'name':'Test', 'radio_freq':915.0, 'radio_bw':250.0, 'radio_sf':10, 'radio_cr':5})
    calls=[]
    async def query():
        calls.append(('query',))
        return SimpleNamespace(type=EventType.DEVICE_INFO, payload={'fw ver':10,'model':'Companion','ver':'1.14','fw_build':'test','path_hash_mode':0})
    async def self_info():
        calls.append(('self',))
        return SimpleNamespace(type=EventType.SELF_INFO, payload=node.self_info)
    async def name(value):
        calls.append(('name',value)); node.self_info['name']=value
        return SimpleNamespace(type=EventType.OK, payload={})
    async def stats():
        return SimpleNamespace(type=EventType.STATS_PACKETS,payload={'flood_tx':42})
    async def set_radio(*values):
        calls.append(('radio',*values))
        return SimpleNamespace(type=EventType.OK,payload={})
    node.commands=SimpleNamespace(send_device_query=query,send_appstart=self_info,set_name=name,get_stats_packets=stats,set_radio=set_radio)
    tx=MeshCoreTransmitter('serial');tx._mc=node
    return tx,calls


@pytest.mark.asyncio
@pytest.mark.parametrize('command',['ver','v','query','q','get radio','.get radio','infos','get name','get stats_packets'])
async def test_standard_commands_match_official_dispatcher(command):
    from meshcore_cli import meshcore_cli as official
    tx,calls=radio()
    actual=await tx.execute_console(command)
    remaining,expected=await official.next_cmd(tx._mc,command.split(),sink=io.StringIO())
    assert remaining==[]
    assert actual==expected.strip()


@pytest.mark.asyncio
async def test_standard_settings_syntax_updates_sender_and_radio():
    tx,calls=radio()
    assert await tx.execute_console('set name "New node"')=='ok'
    assert ('name','New node') in calls
    assert tx._sender_name=='New node'
    assert await tx.execute_console('set radio 915,250,10,5')=='ok'
    assert ('radio','915','250','10','5') in calls


@pytest.mark.parametrize('command',['script /tmp/file','handler_attach rxlog "touch /tmp/pwn"','chat','wait_key','alias foo ver','get radio > /tmp/file','ver get radio','set name','get radio extra','set path_hash_mode -1','set path_hash_mode 3','get color','set lc_output_format bad'])
def test_host_interactive_chained_and_invalid_commands_are_rejected_before_dispatch(command):
    with pytest.raises(ValueError):parse(command)


@pytest.mark.parametrize('command',['help','?get','?set','get help','set help'])
@pytest.mark.asyncio
async def test_standard_help_is_returned_in_console(command):
    tx,calls=radio()
    result=await tx.execute_console(command)
    assert result
    assert not calls


@pytest.mark.asyncio
async def test_official_swallowed_errors_are_reported_as_failures():
    tx,calls=radio()
    async def broken(): raise OSError('link lost')
    tx._mc.commands.send_device_query=broken
    with pytest.raises(RuntimeError,match='link lost'):
        await tx.execute_console('ver')


@pytest.mark.asyncio
async def test_official_error_event_is_not_success():
    tx,calls=radio()
    async def rejected():return SimpleNamespace(type=EventType.ERROR,payload={'error_code':1})
    tx._mc.commands.send_device_query=rejected
    with pytest.raises(RuntimeError,match='radio rejected'):
        await tx.execute_console('ver')


def test_page_has_only_one_console_and_title_contains_no_html_panel():
    from pathlib import Path
    source=Path('app/web/templates/meshcore_settings.html').read_text()
    assert source.count('id="console-form"')==1
    title=source.split('{% block title %}',1)[1].split('{% endblock %}',1)[0]
    assert '<section' not in title


@pytest.mark.asyncio
async def test_get_radio_does_not_report_cached_settings_after_failed_read():
    tx,calls=radio()
    async def rejected(): return SimpleNamespace(type=EventType.ERROR,payload={'error_code':1})
    tx._mc.commands.send_appstart=rejected
    with pytest.raises(RuntimeError,match='radio rejected'):
        await tx.execute_console('get radio')


@pytest.mark.asyncio
async def test_json_native_command_error_is_still_an_error():
    tx,calls=radio()
    async def query():return SimpleNamespace(type=EventType.DEVICE_INFO,payload={'fw ver':14})
    async def cli(command):return SimpleNamespace(type=EventType.CLI_REPLY,payload={'text':'Unknown command'})
    tx._mc.commands.send_device_query=query
    tx._mc.commands.run_cli_command=cli
    with pytest.raises(RuntimeError,match='Unknown command'):
        await tx.execute_console('.cli unsupported')

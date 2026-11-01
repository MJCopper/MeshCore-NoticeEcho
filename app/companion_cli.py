"""Direct local companion CLI transport; no host command dispatcher."""
from contextlib import contextmanager
import logging

MAX_COMMAND_BYTES = 200
COMMAND_TIMEOUT = 15


class ConsoleError(RuntimeError):
    def __init__(self, state, message):
        super().__init__(message)
        self.state = state


def validate(command):
    if (not isinstance(command, str) or not command.strip()
            or len(command.encode('utf-8')) > MAX_COMMAND_BYTES
            or any(ord(char) < 32 or ord(char) == 127 for char in command)):
        raise ValueError('Enter one command of at most 200 UTF-8 bytes without control characters')
    return command


@contextmanager
def private_sdk_exchange():
    # The SDK logs raw requests/replies at DEBUG, including passwords and keys.
    # Commands and transport RX run in different tasks, so suppress SDK capture
    # for the whole exclusive exchange rather than filtering just this task.
    sdk_logger = logging.getLogger('meshcore')
    reject = lambda record: False
    sdk_logger.addFilter(reject)
    try:
        yield
    finally:
        sdk_logger.removeFilter(reject)


async def execute(mc, command):
    from meshcore import EventType
    validate(command)
    if not callable(getattr(mc.commands, 'run_cli_command', None)) or not hasattr(EventType, 'CLI_REPLY'):
        raise ConsoleError('unsupported', 'Native CLI unsupported by this SDK; update the application dependencies')
    info = await mc.commands.send_device_query()
    if getattr(info, 'type', None) != EventType.DEVICE_INFO:
        raise ConsoleError('disconnected', 'Could not query the connected companion; refresh the connection')
    try:
        version = int((info.payload or {}).get('fw ver', 0))
    except (TypeError, ValueError):
        version = 0
    if version < 14:
        raise ConsoleError('unsupported', 'Native CLI unsupported; companion protocol 14+ is required (official firmware 1.18+)')
    response = await mc.commands.run_cli_command(command)
    if response is None:
        raise TimeoutError()
    if response.type == EventType.CLI_REPLY:
        text = (response.payload or {}).get('text')
        if not isinstance(text, str):
            raise ConsoleError('protocol-error', 'Companion returned a CLI reply without text')
        return text  # Empty replies, errors and line breaks are device output.
    if response.type == EventType.ERROR:
        payload = response.payload or {}
        if payload.get('reason') in ('timeout', 'no_event_received') or 'error_code' not in payload:
            raise TimeoutError()
        if payload['error_code'] == 1:
            raise ConsoleError('unsupported', 'Native CLI unsupported by this companion (code 1)')
        raise ConsoleError('device-error', str(payload.get('reason') or f"Companion rejected command (code {payload['error_code']})"))
    raise ConsoleError('protocol-error', 'Unexpected response from companion; refresh the connection')

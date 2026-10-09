"""Bounded browser adapter for the official meshcore-cli 1.6.5 dispatcher.

Never invoke process_line/main: those support shell pipelines, host files and
interactive sessions. next_cmd executes one prevalidated companion command.
"""
import asyncio
import io
import logging
import re
import shlex

GET_PARAMS = frozenset("name tx coords lat lon radio repeat path_hash_mode path.hash.mode bat fstats multi_acks manual_add_contacts autoadd_config telemetry_mode_base telemetry_mode_loc telemetry_mode_env advert_loc_policy custom stats_core stats_radio stats_packets stats status allowed_repeat_freq default_scope".split())
SET_PARAMS = frozenset("pin radio path_hash_mode path.hash.mode name tx lat lon coords tuning manual_add_contacts autoadd_config multi_acks telemetry_mode_base telemetry_mode_loc telemetry_mode_env advert_loc_policy default_scope".split())
# Argument counts exclude the command itself; no command chaining is accepted.
ARITIES = {**dict.fromkeys("ver query v q infos i clock st sync_time self_telemetry t advert a floodadv flood_advert get_channels gc reboot contacts list lc reload_contacts rc pending_contacts recv r sync_msgs sm".split(), (0,)),
           **dict.fromkeys("get_channel remove_channel scope time contact_info ci export_contact ec share_contact sc remove_contact".split(), (1,)),
           "set_channel": (3,), "chan": (2,), "ch": (2,), "public": (1,), "dch": (1,), "msg": (2,), "m": (2,)}
LIMITS = "One command per submission. Interactive sessions, scripts, shell pipelines/redirection, host-file operations, aliases and background subscriptions require a local meshcore-cli terminal. Quote names/messages containing spaces."


def help_text(topic=""):
    if topic == "get":
        return "get <parameter>\nParameters: " + ", ".join(sorted(GET_PARAMS))
    if topic == "set":
        return "set <parameter> <value>\nParameters: " + ", ".join(sorted(SET_PARAMS)) + "\nExamples: set radio 915,250,10,5; set path_hash_mode 1 (mode 0/1/2 = 1/2/3 bytes per hop)."
    return "MeshCore CLI 1.6.5 companion commands\n" + " | ".join(sorted(ARITIES)) + "\nget <parameter>; set <parameter> <value>; clock sync; cli <firmware command>\nHelp: get help; set help; ?get; ?set. Prefix a command with . for JSON output.\n" + LIMITS


def parse(command):
    if not command.strip() or len(command.encode("utf-8")) > 200 or any(ord(c) < 32 for c in command):
        raise ValueError("Enter one command of at most 200 UTF-8 bytes without control characters")
    words = shlex.split(command)
    if not words:
        raise ValueError("Enter a command")
    name = words[0].lstrip(".")
    if name == "help" or name.startswith("?") or words in (["get", "help"], ["set", "help"]):
        return words, help_text(name[1:] if name.startswith("?") else words[0] if words[-1] == "help" and len(words) == 2 else "")
    if name in ("get", "set"):
        params = GET_PARAMS if name == "get" else SET_PARAMS
        if len(words) != (2 if name == "get" else 3) or words[1] not in params:
            raise ValueError(f"Use {name} help for supported parameters and syntax")
        if name == "set" and words[1] in ("path_hash_mode", "path.hash.mode") and words[2] not in ("0", "1", "2"):
            raise ValueError("Path hash mode must be 0, 1, or 2 (1, 2, or 3 bytes per hop)")
    elif name == "cli":
        if len(words) < 2:
            raise ValueError("Use cli <firmware command>")
        words = [words[0], command.strip().split(None, 1)[1]]
    elif name == "clock" and words[1:] == ["sync"]:
        pass
    elif name not in ARITIES:
        raise ValueError("Command unavailable in the web console. " + LIMITS + " Run help for supported commands.")
    elif len(words) - 1 not in ARITIES[name]:
        raise ValueError("Incorrect arguments; submit one command and quote names/messages containing spaces. Run help.")
    return words, None


async def execute(mc, command):
    words, help_output = parse(command)
    if help_output is not None:
        return help_output
    try:
        from meshcore_cli import meshcore_cli as cli
    except ImportError as exc:
        raise RuntimeError("MeshCore CLI dependency unavailable; reinstall requirements or rebuild the Docker image") from exc
    output = io.StringIO()
    errors = []
    from meshcore import EventType

    class Commands:
        def __getattr__(self, name):
            method = getattr(mc.commands, name)
            async def invoke(*args, **kwargs):
                response = await method(*args, **kwargs)
                if getattr(response, "type", None) == EventType.ERROR:
                    errors.append(f"{name}: radio rejected command ({getattr(response, 'payload', {})})")
                elif response is None and name != "reboot":
                    errors.append(f"{name}: no command response; refresh before retrying")
                elif getattr(response, "type", None) == getattr(EventType, "CLI_REPLY", None):
                    text = (getattr(response, "payload", {}) or {}).get("text", "")
                    if re.match(r"(?i)^(error\b|err\b|unknown command|invalid command|unsupported|unrecognized)", text.strip()):
                        errors.append(text)
                return response
            return invoke

    class Companion:
        commands = Commands()
        def __getattr__(self, name):
            return getattr(mc, name)

    companion = Companion()
    task = asyncio.current_task()

    class CaptureErrors(logging.Handler):
        def emit(self, record):
            # The dispatcher catches exceptions and logs them instead of raising.
            try:
                current = asyncio.current_task()
            except RuntimeError:
                return
            if current is task and record.levelno >= logging.ERROR:
                errors.append(record.getMessage())
    handler = CaptureErrors()
    cli.logger.addHandler(handler)
    try:
        remaining, text = await cli.next_cmd(companion, words.copy(), sink=output)
    finally:
        cli.logger.removeHandler(handler)
    if text:
        output.write(text)
    result = output.getvalue().strip()
    if errors:
        raise RuntimeError("MeshCore CLI: " + "; ".join(errors))
    if remaining is None:
        raise ValueError("MeshCore CLI did not recognize the command")
    if remaining:
        raise ValueError("Only one command per submission is supported")
    if re.search(r"(?im)^\s*(?:error\b|err\b|unknown command|invalid command|unsupported|unrecognized)", result):
        raise RuntimeError(result)
    return result or "Command completed without output"

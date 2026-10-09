"""MeshCore transmit layer.

Each protocol is an independent transport with its own enable flag and
connection (serial or TCP). TransmitManager fans every outbound message out to
ALL enabled transports, paces bursts, and manages per-transport reconnects.
Dry-run lives in the poller; manual sends bypass pacing. Backends are imported
lazily so a missing optional dependency (e.g. meshcore) never breaks startup.
"""
from __future__ import annotations

import abc
import asyncio
import logging
from hashlib import sha256
import math
import secrets
import inspect
import json
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from .config import (BURST_GAP_SECONDS, QUEUE_MAX, QUEUE_BYTE_MAX, MAX_PAYLOAD_BYTES,
                     MESHCORE_CHANNEL_TEXT_BYTES)


class TxUnsent(Exception):
    """Categorised pre-submission/legacy failure.

    Explicit rejections can be recovered; unverified means potentially sent and
    uses the separate uncertainty policy. A link timeout after submission must
    never be treated as proof that a retry is safe.
    """
    def __init__(self, category: str, detail: str):
        super().__init__(detail)
        self.category = category
        self.detail = detail

from .transmission import TransmissionPolicy, TxResult, RepeatTracker, channel_payload

logger = logging.getLogger("wx_echo.tx")


class Transmitter(abc.ABC):
    label = "?"

    @abc.abstractmethod
    async def connect(self) -> None: ...
    @abc.abstractmethod
    async def send_text(self, text: str, channel: int) -> TxResult | None: ...
    @abc.abstractmethod
    async def close(self) -> None: ...
    @property
    @abc.abstractmethod
    def connected(self) -> bool: ...

    async def read_channels(self) -> list:
        """Return the channels configured on the device: [{index, name}]. Optional."""
        return []


class MeshCoreTransmitter(Transmitter):
    label = "MeshCore"

    def __init__(self, conn: str, port: str = "", host: str = "", baud: int = 115200):
        self.conn, self.port, self.host, self.baud = conn, port, host, baud
        self._mc = None
        self._sender_name = None
        self.policy = TransmissionPolicy()
        self.repeat_tracker = RepeatTracker()
        self._subscriptions = []
        self._clock_subscriptions = []
        self.counter_capability = "unknown"
        self.counter_error = ""
        self.counter_checked_at = None
        self.repeat_status = "not connected"
        self.last_result = None
        self._last_timestamp = 0
        self._send_lock = asyncio.Lock()
        self.on_submission = None

    @property
    def message_budget(self) -> int:
        if self._sender_name is None:
            return MAX_PAYLOAD_BYTES
        return max(0, MESHCORE_CHANNEL_TEXT_BYTES - len(self._sender_name.encode("utf-8")) - 2)

    async def connect(self) -> None:
        from meshcore import MeshCore  # lazy: optional dependency
        if self._mc is not None:
            await self.close()
        # default_timeout: cap how long we wait for the device's "OK" confirmation
        #   (channel broadcasts have no real ACK, so a long wait just stalls sends).
        # auto_reconnect: let the library recover a dropped USB/TCP link on its own.
        if self.conn == "tcp":
            h, _, p = self.host.partition(":")
            self._mc = await MeshCore.create_tcp(
                h, int(p or 4000), default_timeout=6.0, auto_reconnect=True)
        else:
            self._mc = await MeshCore.create_serial(
                self.port, self.baud, default_timeout=6.0, auto_reconnect=True)
        if self._mc is None:
            where = self.host if self.conn == "tcp" else self.port
            raise RuntimeError("no response from MeshCore node on %s" % (where or "(unset)"))
        from meshcore import EventType
        info = await self._mc.commands.send_appstart()
        if info.type != EventType.SELF_INFO or not isinstance(info.payload, dict):
            raise RuntimeError("could not read MeshCore sender name")
        self._sender_name = info.payload.get("name")
        if not isinstance(self._sender_name, str):
            raise RuntimeError("MeshCore sender name is unavailable")
        self.counter_capability = "unknown"
        self.counter_error = ""
        self._subscribe_repeats()

    def _subscribe_repeats(self):
        from meshcore import EventType
        if not self.policy.repeat_detection:
            self.repeat_status = "disabled"
            return
        try:
            self._subscriptions.append(self._mc.subscribe(EventType.RX_LOG_DATA, self.repeat_tracker.receive))
            self.repeat_status = "listening; awaiting channel capability check"
        except (AttributeError, TypeError) as exc:
            self.repeat_status = "unavailable: %s" % exc

    async def _prepare_repeat(self, result, text, channel):
        if not self.policy.repeat_detection or not self._subscriptions:
            return
        try:
            from meshcore import EventType
            response = await asyncio.wait_for(self._mc.commands.get_channel(channel), 2.0)
            if response is not None and response.type == EventType.ERROR and (response.payload or {}).get("error_code") == 2:
                self._channel_exists = False
            if response is None or response.type != EventType.CHANNEL_INFO:
                raise ValueError("channel information unavailable")
            self._channel_exists = True
            if not isinstance(self._sender_name, str):
                info = await asyncio.wait_for(self._mc.commands.send_appstart(), 2.0)
                if info.type != EventType.SELF_INFO or not isinstance((info.payload or {}).get("name"), str):
                    raise ValueError("sender name unavailable after companion command")
                self._sender_name = info.payload["name"]
            secret = response.payload.get("channel_secret")
            if isinstance(secret, str):
                secret = bytes.fromhex(secret)
            if not isinstance(secret, bytes):
                raise ValueError("channel secret unavailable")
            payload = channel_payload(secret, self._sender_name, text, result.timestamp)
            # Listen throughout command preparation and confirmation, even with a short late window.
            self.repeat_tracker.register(payload, result, 8 + self.policy.confirmation_seconds + self.policy.late_repeat_seconds)
            self.repeat_status = "listening; exact packet matching available"
        except Exception as exc:
            self.repeat_status = "unavailable for this send: %s" % (str(exc) or type(exc).__name__)

    async def send_text(self, text: str, channel: int) -> TxResult:
        async with self._send_lock:
            return await self._send_text(text, channel)

    async def _send_text(self, text: str, channel: int) -> TxResult:
        if self._mc is None:
            raise TxUnsent("link", "not connected before submission")
        size = len(text.encode("utf-8"))
        if size > self.message_budget:
            raise TxUnsent("too_large", f"message is {size} bytes; MeshCore allows {self.message_budget} with sender name")
        if not 0 <= channel <= 255:
            raise TxUnsent("no_channel", "channel must be between 0 and 255")
        if "\x00" in text:
            raise TxUnsent("too_large", "message contains a NUL character")
        from meshcore import EventType
        self._last_timestamp = max(int(time.time()), self._last_timestamp + 1)
        result = TxResult("unconfirmed", timestamp=self._last_timestamp)
        started = time.monotonic()
        self._channel_exists = None
        await self._prepare_repeat(result, text, channel)
        # The SDK requests also consume the deadline; baseline preparation is bounded.
        before = None
        for _ in range(2):
            before = await self._bounded_counter(0.75)
            if before is not None or self.counter_capability == "unsupported":
                break
        result.counter_before = before
        if self.on_submission is not None:
            self.on_submission(result)  # commit intent before any command bytes can be written
        result.submitted = True
        try:
            command = self._mc.commands.send_chan_msg
            parameters = inspect.signature(command).parameters
            if "timestamp" in parameters:
                response = await asyncio.wait_for(command(channel, text, timestamp=result.timestamp), 6.0)
            else:
                # Older SDKs cannot establish this packet identity reliably.
                self.repeat_tracker.discard(result)
                result.packet_id = ""
                self.repeat_status = "unavailable: SDK does not support explicit send timestamps"
                response = await asyncio.wait_for(command(channel, text), 6.0)
            if response is not None and response.type == EventType.ERROR:
                payload = response.payload or {}
                if result.confirmed:
                    result.detail = "Exact heard repeat confirmed transmission despite contradictory command error: %s" % (payload.get("reason") or payload.get("error_code", "unknown"))
                    result.elapsed_seconds = time.monotonic() - started
                    self.last_result = result
                    return result
                result.outcome = "rejected"
                result.rejection_category = {1: "unsupported", 6: "invalid"}.get(payload.get("error_code"), "")
                if payload.get("error_code") == 2 and self._channel_exists is False:
                    result.rejection_category = "no_channel"
                result.detail = str(payload.get("reason") or "radio rejected channel message (code %s)" % payload.get("error_code", "unknown"))
                self.repeat_tracker.discard(result)
                result.elapsed_seconds = time.monotonic() - started
                self.last_result = result
                return result
            result.accepted = response is not None and response.type == getattr(EventType, "OK", "ok")
            if not result.accepted:
                result.detail = "command response unavailable; message may have transmitted"
        except Exception as exc:
            result.detail = "command response interrupted; message may have transmitted: %s" % (str(exc) or type(exc).__name__)
        deadline = time.monotonic() + self.policy.confirmation_seconds
        while time.monotonic() < deadline and not result.confirmed:
            if before is not None:
                after = await self._bounded_counter(min(0.75, max(0.001, deadline - time.monotonic())))
                result.counter_after = after
                if after is not None and after > before:
                    if not result.confirmed:
                        result.outcome = "local_confirmed"
                    break
                if after is not None and after < before:
                    result.detail = "local TX counter reset; transmission cannot be confirmed locally"
                    result.counter_reset = True
                    before = None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                await asyncio.wait_for(result.repeat_event.wait(), min(0.3, remaining))
            except asyncio.TimeoutError:
                pass
        if not result.confirmed and not result.detail:
            result.detail = ("local TX counter unavailable: " + (self.counter_error or "no baseline")
                             if result.counter_before is None else "no local TX activity observed before confirmation timeout")
        tracking = self.policy.late_repeat_seconds
        if self.policy.retry_unconfirmed and not result.confirmed:
            tracking = max(tracking, self.policy.retry_delay_seconds + 8 + self.policy.confirmation_seconds)
        self.repeat_tracker.extend(result, tracking)
        result.elapsed_seconds = time.monotonic() - started
        self.last_result = result
        return result

    async def _bounded_counter(self, timeout):
        if self.counter_capability == "unsupported":
            return None
        try:
            return await asyncio.wait_for(self._flood_tx(), timeout)
        except asyncio.TimeoutError:
            self.counter_capability = "unavailable"
            self.counter_error = "statistics request timed out"
            self.counter_checked_at = time.time()
            return None

    async def _flood_tx(self):
        self.counter_checked_at = time.time()
        try:
            from meshcore import EventType
            response = await self._mc.commands.get_stats_packets()
            if response is None:
                raise ValueError("statistics response missing")
            payload = response.payload or {}
            if response.type == EventType.ERROR:
                if payload.get("error_code") == 1:
                    self.counter_capability = "unsupported"
                raise ValueError(str(payload.get("reason") or "statistics rejected (code %s)" % payload.get("error_code", "unknown")))
            value = payload.get("flood_tx")
            if value is None:
                raise ValueError("flood_tx field missing")
            value = int(value)
            if value < 0:
                raise ValueError("negative flood_tx counter")
            self.counter_capability, self.counter_error = "available", ""
            return value
        except Exception as exc:
            if self.counter_capability != "unsupported":
                self.counter_capability = "unavailable"
            self.counter_error = str(exc) or type(exc).__name__
            return None

    async def refresh_confirmation(self, result, timeout):
        if result.confirmed or result.counter_before is None or result.counter_reset or not self.connected:
            return
        after = await self._bounded_counter(timeout)
        result.counter_after = after
        if after is not None and after < result.counter_before:
            result.counter_reset = True
            return
        if after is not None and after > result.counter_before:
            if not result.confirmed:
                result.outcome = "local_confirmed"
                result.detail = "Local transmission confirmed after initial wait"
            for callback in tuple(result.listeners):
                callback(result)

    def transmission_diagnostics(self):
        self.repeat_tracker.prune()
        return {"counter_capability": self.counter_capability, "counter_error": self.counter_error,
                "counter_checked_at": self.counter_checked_at, "repeat_status": self.repeat_status,
                "repeat_subscription_active": bool(self._subscriptions) and self.connected,
                "last_rx_event_at": self.repeat_tracker.last_event_at,
                "tracked_packets": len(self.repeat_tracker.pending),
                "policy": vars(self.policy),
                "last_result": self.last_result.as_record() if self.last_result is not None else None}

    async def close(self) -> None:
        for subscription in self._subscriptions + self._clock_subscriptions:
            subscription.unsubscribe()
        self._subscriptions.clear()
        self._clock_subscriptions.clear()
        self.repeat_status = "not connected"
        self._sender_name = None
        if self._mc is not None:
            mc, self._mc = self._mc, None
            try:
                await mc.disconnect()
            except Exception:
                pass

    @property
    def connected(self) -> bool:
        return self._mc is not None and self._mc.is_connected

    async def _channel_capacity(self):
        from meshcore import EventType
        if not self.connected:
            raise RuntimeError("MeshCore radio is offline")
        result = await self._channel_command("Reading channel capacity", self._mc.commands.send_device_query())
        self._channel_result(result, EventType.DEVICE_INFO, "Reading channel capacity")
        capacity = int((result.payload or {}).get("max_channels") or 8)
        if not 1 <= capacity <= 256:
            raise RuntimeError("Invalid companion channel capacity")
        return capacity

    @staticmethod
    async def _channel_command(stage, operation):
        try:
            return await operation
        except (TimeoutError, OSError) as exc:
            raise RuntimeError(f"{stage} failed: companion communication interrupted; refresh to check whether the change was saved") from exc

    @staticmethod
    def _channel_result(result, expected, stage, index=None):
        payload = getattr(result, "payload", None) or {}
        if getattr(result, "type", None) != expected:
            details = []
            for field in ("code", "error_code", "reason"):
                value = payload.get(field)
                if isinstance(value, int) or value in ("timeout", "no_event_received", "unsupported"):
                    details.append(f"{field}={value}")
            suffix = f" ({', '.join(details)})" if details else ""
            logging.getLogger(__name__).warning("%s failed%s", stage, suffix)
            raise RuntimeError(f"{stage} failed{suffix}")
        if index is not None and payload.get("channel_idx", index) != index:
            raise RuntimeError(f"{stage} returned a different channel slot")
        return payload

    async def read_channels(self) -> list:
        if self._mc is None:
            raise RuntimeError("not connected")
        from meshcore import EventType
        out = []
        for idx in range(await self._channel_capacity()):
            res = await self._channel_command(f"Reading channel {idx}", self._mc.commands.get_channel(idx))
            p = self._channel_result(res, EventType.CHANNEL_INFO, f"Reading channel {idx}", idx)
            name = (p.get("channel_name") or "").strip()
            if not name:
                continue  # empty/unconfigured slot
            out.append({"index": int(p.get("channel_idx", idx)), "name": name})
        return out

    async def read_info(self) -> dict:
        if self._mc is None:
            return {}
        from meshcore import EventType
        try:
            res = await self._mc.commands.send_device_query()
        except Exception:
            return {}
        if getattr(res, "type", None) != EventType.DEVICE_INFO:
            return {}
        p = getattr(res, "payload", {}) or {}
        return {"model": (p.get("model") or "").strip(),
                "firmware": (p.get("ver") or "").strip()}

    async def read_settings(self) -> dict:
        from meshcore import EventType

        if not self.connected:
            raise RuntimeError("MeshCore radio is offline")
        info = await self._mc.commands.send_device_query()
        if info.type != EventType.DEVICE_INFO:
            raise RuntimeError("could not read MeshCore device info")
        self_info = await self._mc.commands.send_appstart()
        if self_info.type != EventType.SELF_INFO:
            raise RuntimeError("could not read MeshCore settings")

        device = info.payload or {}
        settings = self_info.payload or {}
        battery_mv = None
        try:
            battery = await self._mc.commands.get_bat()
            if battery.type == EventType.BATTERY:
                battery_mv = (battery.payload or {}).get("level")
        except Exception:
            pass
        channels = []
        available_slots = []
        max_channels = min(256, max(1, int(device.get("max_channels") or 8)))
        for index in range(max_channels):
            result = await self._channel_command(f"Reading channel {index}", self._mc.commands.get_channel(index))
            self._channel_result(result, EventType.CHANNEL_INFO, f"Reading channel {index}", index)
            payload = result.payload or {}
            name = (payload.get("channel_name") or "").strip()
            if name:
                channels.append({"index": index, "name": name,
                                 "hash": payload.get("channel_hash") or ""})
            elif index > 0 and payload.get("channel_secret") == bytes(16):
                available_slots.append(index)
        return {
            "name": (settings.get("name") or "").strip(),
            "model": (device.get("model") or "").strip(),
            "firmware": (device.get("ver") or "").strip(),
            "battery_mv": battery_mv,
            "tx_power": settings.get("tx_power"),
            "max_tx_power": settings.get("max_tx_power"),
            "radio_freq": settings.get("radio_freq"),
            "radio_bw": settings.get("radio_bw"),
            "radio_sf": settings.get("radio_sf"),
            "radio_cr": settings.get("radio_cr"),
            "channels": channels,
            "available_slots": available_slots,
            "path_hash_bytes": device.get("path_hash_mode") + 1 if device.get("path_hash_mode") in (0, 1, 2) else None,
            "path_hash_supported": device.get("path_hash_mode") in (0, 1, 2) and callable(getattr(self._mc.commands, "set_path_hash_mode", None)),
            "cli_supported": device.get("fw ver", 0) >= 14 and hasattr(EventType, "CLI_REPLY") and callable(getattr(self._mc.commands, "run_cli_command", None)),
            "max_channels": max_channels,
        }

    async def set_path_hash_bytes(self, size: int) -> int:
        from meshcore import EventType
        if size not in (1, 2, 3):
            raise ValueError("Path hash size must be 1, 2, or 3 bytes per hop")
        if not self.connected:
            raise RuntimeError("MeshCore radio is offline")
        info = await self._mc.commands.send_device_query()
        if info.type != EventType.DEVICE_INFO or (info.payload or {}).get("path_hash_mode") not in (0, 1, 2) or not callable(getattr(self._mc.commands, "set_path_hash_mode", None)):
            raise RuntimeError("Path hash settings are unsupported by this firmware or SDK")
        result = await self._mc.commands.set_path_hash_mode(size - 1)
        self._channel_result(result, EventType.OK, "Saving path hash size")
        verified = await self._mc.commands.send_device_query()
        if verified.type != EventType.DEVICE_INFO or (verified.payload or {}).get("path_hash_mode") != size - 1:
            raise RuntimeError("Could not verify the saved path hash size; refresh before retrying")
        return size

    @staticmethod
    def redact_console(text):
        import re
        text = re.sub(r"(?im)^.*(?:secret|private.?key|password|pin|token).*$", "[sensitive output hidden]", str(text))
        return re.sub(r"(?i)\b[0-9a-f]{32,}\b", "[key hidden]", text)[:8192]

    async def execute_console(self, command: str) -> str:
        from .companion_cli import execute, parse
        if not command.strip():
            raise ValueError("Enter a command")
        shortcuts = ("info", "channels", "stats", "path-hash", "name", "tx-power", "radio")
        if command.strip().split(None, 1)[0] in shortcuts:
            return await self._legacy_console(command)
        words, help_output = parse(command)
        if help_output is not None:
            return help_output
        if not self.connected:
            raise RuntimeError("MeshCore radio is offline")
        name = words[0].lstrip(".")
        if name == "cli":
            from meshcore import EventType
            info = await self._mc.commands.send_device_query()
            if info.type != EventType.DEVICE_INFO or (info.payload or {}).get("fw ver", 0) < 14 or not callable(getattr(self._mc.commands, "run_cli_command", None)) or not hasattr(EventType, "CLI_REPLY"):
                raise RuntimeError("Native CLI unsupported; requires companion protocol 14+ and a compatible SDK")
        changed = name in ("cli", "set", "set_channel", "remove_channel", "reboot")
        if changed:
            self._sender_name = None
        result = await execute(self._mc, command)
        if changed and name != "reboot":
            from meshcore import EventType
            info = await self._mc.commands.send_appstart()
            if info.type == EventType.SELF_INFO:
                self._sender_name = (info.payload or {}).get("name")
        return result

    async def _legacy_console(self, command: str) -> str:
        import json
        import shlex
        from meshcore import EventType
        if not command.strip() or len(command.encode("utf-8")) > 200 or any(ord(c) < 32 for c in command):
            raise ValueError("Enter one command of at most 200 UTF-8 bytes without control characters")
        words = shlex.split(command)
        if words == ["help"]:
            return "help | info | channels | stats core|radio|packets | path-hash [1|2|3] | name <name> | tx-power <dBm> | radio <MHz> <kHz> <SF> <CR> | cli <firmware command>"
        if not self.connected:
            raise RuntimeError("MeshCore radio is offline")
        if words == ["info"]:
            return json.dumps(await self.read_settings(), indent=2)
        if words == ["channels"]:
            return json.dumps(await self.read_channels(), indent=2)
        if words[0] == "stats" and len(words) == 2 and words[1] in ("core", "radio", "packets"):
            method = getattr(self._mc.commands, "get_stats_" + words[1], None)
            if not callable(method):
                raise RuntimeError("Statistics unsupported by this SDK")
            result = await method()
            if result is None or result.type == EventType.ERROR:
                raise RuntimeError("Radio could not return statistics")
            return json.dumps(result.payload or {}, indent=2, default=str)
        if words[0] == "path-hash" and len(words) in (1, 2):
            if len(words) == 1:
                size = (await self.read_settings())["path_hash_bytes"]
                return f"{size} bytes per hop" if size else "Path hash size not reported"
            return f"Verified: {await self.set_path_hash_bytes(int(words[1]))} bytes per hop"
        if words[0] == "name" and len(words) >= 2:
            return "Verified name: " + await self.set_device_name(" ".join(words[1:]))
        if words[0] == "tx-power" and len(words) == 2:
            return f"Verified TX power: {await self.set_tx_power(int(words[1]))} dBm"
        if words[0] == "radio" and len(words) == 5:
            return json.dumps(await self.set_radio_parameters(float(words[1]), float(words[2]), int(words[3]), int(words[4])))
        raise ValueError("Unknown command or arguments. Run help for supported commands")

    async def set_device_name(self, name: str) -> str:
        from meshcore import EventType

        name = name.strip()
        if not name or len(name.encode("utf-8")) > 32 or any(ord(char) < 32 for char in name):
            raise ValueError("device name must be 1-32 UTF-8 bytes without control characters")
        if not self.connected:
            raise RuntimeError("MeshCore radio is offline")
        result = await self._mc.commands.set_name(name)
        if result.type != EventType.OK:
            raise RuntimeError("radio rejected the device name")
        self._sender_name = name
        verified = await self._mc.commands.send_appstart()
        if verified.type != EventType.SELF_INFO or (verified.payload or {}).get("name", "").strip() != name:
            raise RuntimeError("could not verify the saved device name")
        return name

    async def set_tx_power(self, power: int) -> int:
        from meshcore import EventType

        if not self.connected:
            raise RuntimeError("MeshCore radio is offline")
        current = await self._mc.commands.send_appstart()
        if current.type != EventType.SELF_INFO:
            raise RuntimeError("could not read the radio's TX power limit")
        limit = (current.payload or {}).get("max_tx_power")
        if limit is None:
            raise RuntimeError("radio did not report its TX power limit")
        if not 0 <= power <= int(limit):
            raise ValueError(f"TX power must be between 0 and {limit} dBm")
        result = await self._mc.commands.set_tx_power(power)
        if result.type != EventType.OK:
            raise RuntimeError("radio rejected the TX power")
        verified = await self._mc.commands.send_appstart()
        if verified.type != EventType.SELF_INFO or (verified.payload or {}).get("tx_power") != power:
            raise RuntimeError("could not verify the saved TX power")
        return power

    async def set_radio_parameters(self, freq: float, bw: float, sf: int, cr: int) -> dict:
        from meshcore import EventType

        if not (math.isfinite(freq) and 100 <= freq <= 2500):
            raise ValueError("frequency must be between 100 and 2500 MHz")
        if not (math.isfinite(bw) and 1 <= bw <= 2500):
            raise ValueError("bandwidth must be between 1 and 2500 kHz")
        if not 5 <= sf <= 12:
            raise ValueError("spreading factor must be between 5 and 12")
        if not 5 <= cr <= 8:
            raise ValueError("coding rate must be between 5 and 8")
        if not self.connected:
            raise RuntimeError("MeshCore radio is offline")
        result = await self._mc.commands.set_radio(freq, bw, sf, cr)
        if result.type != EventType.OK:
            raise RuntimeError("radio rejected the radio parameters")
        verified = await self._mc.commands.send_appstart()
        if verified.type != EventType.SELF_INFO:
            raise RuntimeError("could not verify the saved radio parameters")
        saved = verified.payload or {}
        try:
            saved_freq = float(saved["radio_freq"])
            saved_bw = float(saved["radio_bw"])
        except (KeyError, TypeError, ValueError):
            raise RuntimeError("could not verify the saved radio parameters")
        if not (
            math.isclose(saved_freq, freq, rel_tol=0, abs_tol=0.0015) and
            math.isclose(saved_bw, bw, rel_tol=0, abs_tol=0.0015) and
            saved.get("radio_sf") == sf and saved.get("radio_cr") == cr
        ):
            raise RuntimeError("radio parameters did not match after saving")
        return {"radio_freq": saved["radio_freq"], "radio_bw": saved["radio_bw"],
                "radio_sf": sf, "radio_cr": cr}

    async def add_channel(self, name: str, secret_hex: str = "") -> dict:
        from meshcore import EventType

        name = name.strip()
        if not name or len(name.encode("utf-8")) > 32 or any(ord(char) < 32 for char in name):
            raise ValueError("channel name must be 1-32 UTF-8 bytes without control characters")
        if secret_hex.strip():
            supplied = secret_hex.strip()
            if len(supplied) != 32:
                raise ValueError("channel key must be exactly 32 hexadecimal characters")
            try:
                secret = bytes.fromhex(supplied)
            except ValueError as exc:
                raise ValueError("channel key must be exactly 32 hexadecimal characters") from exc
            if secret == bytes(16):
                raise ValueError("channel key cannot be all zeros")
            generated = False
        else:
            secret = secrets.token_bytes(16)
            generated = True
        if not self.connected:
            raise RuntimeError("MeshCore radio is offline")
        max_channels = await self._channel_capacity()
        if name.startswith("#"):
            derived = sha256(name.encode("utf-8")).digest()[:16]
            if secret_hex.strip() and secret != derived:
                raise ValueError("# channel keys derive from the name; leave the key blank")
            secret, generated = derived, False
        slot = None
        for index in range(1, max_channels):
            current = await self._channel_command(f"Reading channel {index}", self._mc.commands.get_channel(index))
            payload = self._channel_result(current, EventType.CHANNEL_INFO, f"Reading channel {index}", index)
            if not (payload.get("channel_name") or "").strip() and payload.get("channel_secret") == bytes(16):
                slot = index
                break
        if slot is None:
            raise RuntimeError("no empty private channel slot is available")
        result = await self._channel_command("Writing channel", self._mc.commands.set_channel(slot, name, secret))
        self._channel_result(result, EventType.OK, f"Writing new channel {slot}")
        verified = await self._channel_command(f"Reading channel {slot}", self._mc.commands.get_channel(slot))
        saved = self._channel_result(verified, EventType.CHANNEL_INFO, f"Verifying new channel {slot}", slot)
        if (saved.get("channel_name") != name
                or saved.get("channel_secret") != secret):
            raise RuntimeError("could not verify the new channel")
        return {"index": slot, "name": name, "secret_hex": secret.hex() if generated else ""}

    async def remove_channel(self, index: int) -> None:
        from meshcore import EventType

        if not 1 <= index < 256:
            raise ValueError("only non-default channel slots 1-255 can be removed")
        if not self.connected:
            raise RuntimeError("MeshCore radio is offline")
        current = await self._channel_command(f"Reading channel {index}", self._mc.commands.get_channel(index))
        self._channel_result(current, EventType.CHANNEL_INFO, f"Reading channel {index}", index)
        if not (current.payload or {}).get("channel_name"):
            raise RuntimeError("channel is unavailable")
        result = await self._channel_command("Writing channel", self._mc.commands.set_channel(index, "", bytes(16)))
        self._channel_result(result, EventType.OK, f"Removing channel {index}")
        verified = await self._channel_command(f"Reading channel {index}", self._mc.commands.get_channel(index))
        saved = self._channel_result(verified, EventType.CHANNEL_INFO, f"Verifying channel {index}", index)
        if (verified.type != EventType.CHANNEL_INFO or saved.get("channel_name")
                or saved.get("channel_secret") != bytes(16)):
            raise RuntimeError("could not verify channel removal")

    async def rename_channel(self, index: int, name: str, allow_key_change: bool = False) -> dict:
        from meshcore import EventType

        name = name.strip()
        if not 0 <= index < 256:
            raise ValueError("channel index must be 0-255")
        if not name or len(name.encode("utf-8")) > 32 or any(ord(char) < 32 for char in name):
            raise ValueError("channel name must be 1-32 UTF-8 bytes without control characters")
        if not self.connected:
            raise RuntimeError("MeshCore radio is offline")

        current = await self._channel_command(f"Reading channel {index}", self._mc.commands.get_channel(index))
        payload = self._channel_result(current, EventType.CHANNEL_INFO, f"Reading channel {index}", index)
        secret = payload.get("channel_secret")
        if not payload.get("channel_name") or not isinstance(secret, bytes) or len(secret) != 16:
            raise RuntimeError("channel cannot be renamed safely")
        if name.startswith("#"):
            derived = sha256(name.encode("utf-8")).digest()[:16]
            if derived != secret and not allow_key_change:
                raise ValueError("Renaming to a # channel changes its key. Confirm the key change before saving.")
            secret = derived
        result = await self._channel_command("Writing channel", self._mc.commands.set_channel(index, name, secret))
        self._channel_result(result, EventType.OK, f"Writing channel {index}")
        verified = await self._channel_command(f"Reading channel {index}", self._mc.commands.get_channel(index))
        updated = self._channel_result(verified, EventType.CHANNEL_INFO, f"Verifying channel {index}", index)
        if (verified.type != EventType.CHANNEL_INFO or updated.get("channel_name") != name
                or updated.get("channel_secret") != secret):
            raise RuntimeError("could not verify channel name and key preservation")
        return {"index": index, "name": name, "hash": updated.get("channel_hash") or ""}


def _fmt_model(info: dict) -> str:
    """Human label for a radio from its info dict, e.g. 'Heltec V3 (fw 2.5.9)'."""
    model = (info.get("model") or "").replace("_", " ").strip()
    fw = (info.get("firmware") or "").strip()
    if model and fw:
        return "%s (fw %s)" % (model, fw)
    return model or (("fw %s" % fw) if fw else "")


@dataclass
class Transport:
    name: str                     # "meshcore"
    label: str
    enabled: bool
    conn: str                     # "serial" | "tcp"
    channel: int
    target: str                   # serial path or host - display + "configured?" check
    make: object                  # callable() -> Transmitter
    test_channel: int = 1         # channel used for Troubleshoot tests only
    tx: Transmitter | None = None
    connected: bool = False
    error: str = ""


@dataclass
class QueueItem:
    text: str
    delay_after: float = BURST_GAP_SECONDS
    on_result: object = None   # optional callable(ok: bool, err: str) invoked after the send
    verification: bool = False
    priority: int = 3
    notice_id: object = None
    valid_if: object = None
    queued_at: float = field(default_factory=time.time)
    policy: TransmissionPolicy | None = None
    delivery_context: tuple | None = None


@dataclass
class QueuedNotice:
    """One admitted notice; materialise only its next radio part."""
    parts: tuple[tuple[str, float], ...]
    callback: object = None
    priority: int = 3
    valid_if: object = None
    index: int = 0
    notice_id: object = field(default_factory=object)
    verification: bool = False
    queued_at: float = field(default_factory=time.time)

    policy: TransmissionPolicy | None = None
    delivery_context: object = None

    @property
    def text(self):
        return self.parts[self.index][0]

    @property
    def remaining_bytes(self):
        return sum(len(text.encode("utf-8")) for text, _ in self.parts)

    def next_part(self):
        index = self.index
        text, delay = self.parts[index]
        result_callback = self.callback
        callback = (lambda ok, err="": result_callback(index, ok, err)) if result_callback else None
        self.index += 1
        context = None
        if self.delivery_context:
            row_id, indices, total = self.delivery_context
            context = (row_id, indices[index], total)
        return QueueItem(text, delay, callback, False, self.priority, self.notice_id, self.valid_if,
                         policy=self.policy, delivery_context=context)


def _build_transports(db) -> dict:
    def g(k, d=None):
        return db.get_setting(k, d)

    def num(k, d=0):
        try:
            return int(g(k, d) or d)
        except (TypeError, ValueError):
            return d

    mc_conn = g("meshcore_conn", "serial") or "serial"
    mc = Transport(
        name="meshcore", label="MeshCore",
        enabled=True,
        conn=mc_conn, channel=num("meshcore_channel", 0),
        target=(g("meshcore_host", "") if mc_conn == "tcp" else g("meshcore_port", "")) or "",
        make=lambda: MeshCoreTransmitter(mc_conn, g("meshcore_port", "") or "", g("meshcore_host", "") or ""),
        test_channel=num("meshcore_test_channel", 1),
    )
    return {"meshcore": mc}


class TransmitManager:
    """Serializes node access, paces bursts, fans out to all enabled transports."""

    supports_notice_guards = True
    supports_delivery_context = True

    def __init__(self, db):
        self._db = db
        self._repeat_tracker = RepeatTracker()
        self._restore_repeat_tracking()
        self._transports = _build_transports(db)
        self._queue: deque[QueueItem | QueuedNotice] = deque(maxlen=QUEUE_MAX + 1)
        self._queue_event = asyncio.Event()
        self._verification_in_flight = False
        self._active_notice = None
        self._lock = asyncio.Lock()
        self._worker_task: asyncio.Task | None = None
        self._connection_task: asyncio.Task | None = None
        self._reconnect_delay = 2.0
        self._stopped = False
        from .companion_clock import ClockSync
        self._clock = ClockSync(db)
        self._clock_task = None

    def _restore_repeat_tracking(self):
        fetch = getattr(self._db, "tracking_transmissions", None)
        if fetch is None:
            return
        for row in fetch():
            try:
                evidence = json.loads(row["evidence"] or "{}")
                digest = evidence.get("packet_id", "")
                window = evidence.get("policy", {}).get("late_repeat_seconds", 60)
                remaining = evidence.get("repeat_expires_at") or evidence.get("started_at", 0) + window
                remaining -= time.time()
                if not digest or remaining <= 0:
                    continue
                fields = {key: value for key, value in evidence.items()
                          if key in TxResult.__dataclass_fields__ and key not in ("listeners", "repeat_event")}
                result = TxResult(**fields)
                result.outcome, result.submitted, result.log_id = row["outcome"], True, row["id"]
                result.retry_pending = False
                result.listeners.append(self._db.finish_transmission)
                self._repeat_tracker.register_digest(digest, result, remaining)
            except (TypeError, ValueError, KeyError):
                logger.warning("Could not restore repeat tracking for transmission %s", row["id"])

    # ---- lifecycle ------------------------------------------------------
    def start(self) -> None:
        self._stopped = False
        self._worker_task = asyncio.create_task(self._worker(), name="tx-worker")
        self._connection_task = asyncio.create_task(self._maintain_connections(), name="radio-connection")
        self._clock_task = asyncio.create_task(self._maintain_clock(), name="companion-clock")

    async def stop(self) -> None:
        self._stopped = True
        self._queue_event.set()
        if self._clock_task:
            self._clock_task.cancel()
            try:
                await self._clock_task
            except asyncio.CancelledError:
                pass
        if self._connection_task:
            self._connection_task.cancel()
            try:
                await self._connection_task
            except asyncio.CancelledError:
                pass
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        for t in self._transports.values():
            if t.tx:
                await t.tx.close()

    async def reconfigure(self) -> None:
        """Rebuild transports from settings and connect the enabled ones.
        Called at startup and after a settings save."""
        async with self._lock:
            for t in self._transports.values():
                if t.tx:
                    try:
                        await t.tx.close()
                    except Exception:
                        pass
            self._transports = _build_transports(self._db)
            targets = [t for t in self._transports.values() if t.enabled and t.target]
            for t in targets:
                await self._ensure(t)

    async def _maintain_connections(self) -> None:
        while not self._stopped:
            await asyncio.sleep(15)
            try:
                async with self._lock:
                    for transport in self._transports.values():
                        if not transport.enabled or not transport.target:
                            continue
                        if transport.tx is not None and not transport.tx.connected:
                            transport.connected = False
                            await self._reconnect(transport)
                        elif not transport.connected or transport.tx is None:
                            await self._ensure(transport)
            except Exception:
                logger.exception("radio connection maintenance failed")

    async def _maintain_clock(self):
        while not self._stopped:
            await asyncio.sleep(1)
            if not self._clock.ready() or self._lock.locked() or self._active_notice is not None:
                continue
            try:
                async with self._lock:
                    if self._active_notice is None:
                        await self._clock.check(self._saved_radio())
            except RuntimeError:
                # Offline: connection callbacks will request a check on recovery.
                pass
            except Exception:
                logger.exception("companion clock maintenance failed")

    def clock_settings_changed(self):
        self._clock.request('clock settings changed')

    def clock_status(self):
        transport = self._transports['meshcore']
        return self._clock.status(bool(transport.tx and transport.connected and transport.tx.connected))

    async def check_companion_clock(self, force=False, target=None):
        if self._lock.locked() or self._active_notice is not None:
            raise RuntimeError("Companion busy; clock checks wait until the notice is complete")
        async with self._lock:
            return await self._clock.check(self._saved_radio(), force=force, target=target, read_only=not force and target is None)

    # ---- status / compat ------------------------------------------------
    @property
    def connected(self) -> bool:
        return any(t.connected and t.tx is not None and t.tx.connected
                   for t in self._transports.values() if t.enabled)

    @property
    def port(self) -> str | None:
        return self._transports["meshcore"].target or None

    @property
    def message_budget(self) -> int:
        t = self._transports["meshcore"]
        return t.tx.message_budget if isinstance(t.tx, MeshCoreTransmitter) and t.connected else MAX_PAYLOAD_BYTES

    @property
    def queue_depth(self) -> int:
        return sum(len(item.parts) - item.index if isinstance(item, QueuedNotice) else 1
                   for item in self._queue)

    @property
    def last_error(self) -> str:
        for t in self._transports.values():
            if t.enabled and t.error:
                return "%s: %s" % (t.label, t.error)
        return ""

    def status(self) -> list[dict]:
        return [
            {"name": t.name, "label": t.label, "enabled": t.enabled,
             "conn": t.conn, "connected": t.connected and t.tx is not None and t.tx.connected,
             "target": t.target, "channel": t.channel, "error": t.error,
             "clock": self.clock_status(),
             "transmission": t.tx.transmission_diagnostics() if hasattr(t.tx, "transmission_diagnostics") else {},
             "policy": vars(TransmissionPolicy.from_settings(self._db.all_settings())) if hasattr(self._db, "all_settings") else vars(TransmissionPolicy())}
            for t in self._transports.values()
        ]

    def _saved_radio(self) -> MeshCoreTransmitter:
        transport = self._transports["meshcore"]
        if not transport.enabled or not transport.target or not transport.connected or not transport.tx or not transport.tx.connected:
            raise RuntimeError("saved MeshCore radio is offline")
        return transport.tx

    async def get_device_settings(self) -> dict:
        async with self._lock:
            result = await self._saved_radio().read_settings()
            result["clock"] = self.clock_status()
            return result

    async def set_device_name(self, name: str) -> str:
        async with self._lock:
            return await self._saved_radio().set_device_name(name)

    async def set_tx_power(self, power: int) -> int:
        async with self._lock:
            return await self._saved_radio().set_tx_power(power)

    async def set_radio_parameters(self, freq: float, bw: float, sf: int, cr: int) -> dict:
        async with self._lock:
            return await self._saved_radio().set_radio_parameters(freq, bw, sf, cr)

    async def set_path_hash_bytes(self, size: int) -> int:
        async with self._lock:
            try:
                return await asyncio.wait_for(self._saved_radio().set_path_hash_bytes(size), 15)
            except TimeoutError as exc:
                raise RuntimeError("Path hash operation timed out; refresh before retrying") from exc

    async def execute_companion_command(self, command: str) -> str:
        import shlex
        words = shlex.split(command)
        if words and words[0].lstrip('.') in ('clock', 'time', 'st', 'sync_time'):
            from .companion_cli import parse
            parse(command)
            name = words[0].lstrip('.')
            target = int(words[1]) if name == 'time' else None
            if target is not None and not 0 <= target <= 0xffffffff:
                raise ValueError('Time must be a Unix timestamp between 0 and 4294967295')
            result = await self.check_companion_clock(force=(name in ('st', 'sync_time') or words[1:] == ['sync']), target=target)
            if result['status'] in ('failed', 'unsupported', 'inconclusive'):
                raise RuntimeError(result['error'])
            if target is not None and self._clock.policy()['enabled']:
                result['note'] = 'Automatic synchronization will later restore server time; disable it to retain an explicit time'
            if words[0].startswith('.'):
                return json.dumps(dict(result, time=result['companion_epoch']))
            return ('Current time: ' + result['companion_time'] + '\n' + result['status'] +
                    f"; drift {result['drift_seconds']:.2f}s ±{result['uncertainty_seconds']:.2f}s" +
                    ('\n' + result['note'] if result.get('note') else ''))
        if self._lock.locked() or self._active_notice is not None:
            raise RuntimeError("Companion busy; wait for the current transmission or settings operation")
        async with self._lock:
            radio = self._saved_radio()
            try:
                if command.strip().split(None, 1)[0].lstrip(".") in ("reboot", "cli"):
                    self._clock.request("manual companion command")
                output = await asyncio.wait_for(radio.execute_console(command), 15)
                if command.strip().split(None, 1)[0].lstrip(".") in ("cli", "set", "set_channel", "remove_channel"):
                    try:
                        await asyncio.wait_for(self._refresh_saved_channels(), 5)
                    except (RuntimeError, TimeoutError):
                        output += "\nCommand completed, but channel cache refresh failed. Refresh settings before using changed channels."
                if command.strip().split(None, 1)[0].lstrip(".") in ("reboot", "cli"):
                    self._clock.request("manual companion command")
                return radio.redact_console(output)
            except TimeoutError as exc:
                raise RuntimeError("Command timed out; it may have applied. Refresh settings before retrying") from exc

    async def _refresh_saved_channels(self) -> None:
        channels = await self._saved_radio().read_channels()
        self._db.set_setting("meshcore_channels", channels)
        transport = self._transports["meshcore"]
        self._db.set_setting("meshcore_channels_target",
                             {"conn": transport.conn, "target": transport.target})

    async def rename_device_channel(self, index: int, name: str, allow_key_change: bool = False) -> dict:
        async with self._lock:
            radio = self._saved_radio()
            renamed = await radio.rename_channel(index, name, True) if allow_key_change else await radio.rename_channel(index, name)
            try:
                await self._refresh_saved_channels()
            except Exception:
                renamed["refresh_error"] = "Channel saved and verified, but channel lists could not be refreshed. Refresh before selecting a channel."
            return renamed

    async def add_device_channel(self, name: str, secret_hex: str = "") -> dict:
        async with self._lock:
            created = await self._saved_radio().add_channel(name, secret_hex)
            try:
                await self._refresh_saved_channels()
            except Exception as exc:
                # Do not lose the one-time generated key after a verified write.
                created["refresh_error"] = str(exc)
            return created

    async def remove_device_channel(self, index: int) -> None:
        async with self._lock:
            if not 1 <= index < 256:
                raise ValueError("only non-default channel slots 1-255 can be removed")
            if index in (self._transports["meshcore"].channel,
                         self._transports["meshcore"].test_channel):
                raise ValueError("change the Live and Test channel selections before removing this channel")
            await self._saved_radio().remove_channel(index)
            await self._refresh_saved_channels()

    async def set_port(self, port: str) -> None:
        """Set the MeshCore serial port, then reconnect."""
        self._db.set_setting("meshcore_port", port or "")
        await self.reconfigure()

    # ---- connection -----------------------------------------------------
    async def _ensure(self, t: Transport) -> bool:
        if t.connected and t.tx is not None and t.tx.connected:
            return True
        if not t.enabled:
            return False
        if not t.target:
            t.error = "no connection configured"
            return False
        err = await self._open_once(t)
        if not err:
            self._reconnect_delay = 2.0
            self._db.add_event("INFO", "%s connected (%s)" % (t.label, t.target))
            logger.info("%s connected at %s", t.name, t.target)
            return True
        t.error = err
        self._db.add_error(t.name, err)
        logger.warning("%s connect failed: %s", t.name, err)
        return False

    async def _open_once(self, t: Transport) -> str:
        """Open t's link once. Returns '' on success or an error string. No DB
        logging so callers (startup vs. reconnect) can decide how to report."""
        try:
            tx = t.make()
            if isinstance(tx, MeshCoreTransmitter):
                tx.policy = self._policy()
                tx.repeat_tracker = self._repeat_tracker
            await tx.connect()
            t.tx, t.connected, t.error = tx, True, ""
            if isinstance(tx, MeshCoreTransmitter):
                self._clock.request('connection established')
                from meshcore import EventType
                async def clock_connection(event):
                    if t.tx is tx:
                        self._clock.request('SDK connection changed')
                for event_type in (EventType.CONNECTED, EventType.DISCONNECTED):
                    subscribe = getattr(tx._mc, "subscribe", None)
                    if callable(subscribe):
                        tx._clock_subscriptions.append(subscribe(event_type, clock_connection))
            return ""
        except Exception as exc:
            t.tx, t.connected = None, False
            return "connect failed: %s" % exc

    @staticmethod
    def _is_port_locked(err: str) -> bool:
        e = err.lower()
        return ("lock" in e or "busy" in e or "errno 11" in e
                or "errno 16" in e or "resource temporarily unavailable" in e)

    # ---- sending --------------------------------------------------------
    def enqueue_verification(self, text: str, on_result=None,
                             allow_new: bool = True) -> bool:
        """Keep one verification item at the end of the live warning queue."""
        pending = next((item for item in self._queue if item.verification), None)
        if pending is not None:
            self._queue.remove(pending)
            self._queue.append(pending)
            return True
        if self._verification_in_flight or not allow_new or len(self._queue) >= QUEUE_MAX + 1:
            return False
        self._queue.append(QueueItem(text=text, delay_after=BURST_GAP_SECONDS,
                                     on_result=on_result, verification=True, policy=self._policy()))
        self._queue_event.set()
        return True

    def enqueue_notice(self, parts: list[tuple[str, float]], on_result=None,
                       priority: int = 3, valid_if=None, delivery_context=None) -> bool:
        """Admit every part of one notice together, or defer the whole notice.

        A verification message may occupy the single reserved slot. Each pending
        notice occupies one slot, regardless of its number of parts. Storage is
        also bounded by QUEUE_BYTE_MAX; capacity refusal leaves history deferred.
        on_result(index, ok, error) runs once for each attempted part.
        """
        pending_bytes = sum(item.remaining_bytes if isinstance(item, QueuedNotice) else len(item.text.encode())
                            for item in self._queue if not item.verification)
        if (not parts or sum(not item.verification for item in self._queue) >= QUEUE_MAX
                or pending_bytes + sum(len(text.encode()) for text, _ in parts) > QUEUE_BYTE_MAX):
            return False
        pending = next((item for item in self._queue if item.verification), None)
        if pending is not None:
            self._queue.remove(pending)
        policy = self._policy()
        if delivery_context and hasattr(self._db, "delivery_policy"):
            policy = self._db.delivery_policy(delivery_context, policy)
        notice = QueuedNotice(tuple((text, max(0.0, float(delay))) for text, delay in parts),
                              on_result, priority, valid_if,
                              policy=policy, delivery_context=delivery_context)
        waiting = list(self._queue)
        protected = 0
        while (protected < len(waiting) and self._active_notice is not None
               and waiting[protected].notice_id is self._active_notice):
            protected += 1
        insertion = next((index for index in range(protected, len(waiting))
                          if waiting[index].priority > priority), len(waiting))
        waiting.insert(insertion, notice)
        if pending is not None:
            waiting.append(pending)
        self._queue.clear()
        self._queue.extend(waiting)
        self._queue_event.set()
        return True

    def _next_queued_part(self) -> QueueItem:
        notice = self._queue[0]
        if isinstance(notice, QueuedNotice):
            item = notice.next_part()
            if notice.index == len(notice.parts):
                self._queue.popleft()
            return item
        return self._queue.popleft()

    def _cancel_notice_remainder(self, notice_id, error):
        """Finish every pending callback after a failed/stale notice part."""
        if notice_id is None:
            return
        for notice in tuple(self._queue):
            if notice.notice_id is not notice_id or not isinstance(notice, QueuedNotice):
                continue
            self._queue.remove(notice)
            while notice.index < len(notice.parts):
                part = notice.next_part()
                if part.on_result is not None:
                    self._safe_result(part.on_result, TxResult("not_attempted", error), "Not attempted after notice stopped: " + error)

    def enqueue(self, text: str, channel: int | None = None, on_result=None,
                delay_after: float = BURST_GAP_SECONDS) -> bool:
        """Queue a single live-channel message without evicting earlier notices."""
        callback = (lambda index, ok, err: on_result(ok, err)) if on_result else None
        return self.enqueue_notice([(text, delay_after)], callback)

    def _safe_result(self, cb, ok: bool, err: str = "") -> None:
        try:
            r = cb(ok, err)
            if asyncio.iscoroutine(r):
                asyncio.create_task(r)
        except Exception:
            logger.exception("on_result callback error")

    async def _reconnect(self, t: Transport) -> bool:
        """Reset a transport's link, tolerating a zombie / locked serial port.
        A just-closed port often has not released its fd yet, so an instant reopen
        fails with '[Errno 11] could not exclusively lock port', and close() itself
        can hang on a wedged reader thread. So: close firmly with a timeout, then
        reopen with backoff, waiting out a still-locked port instead of giving up."""
        t.connected = False
        if t.tx is not None:
            try:
                await asyncio.wait_for(t.tx.close(), timeout=8)  # don't hang on a stuck close
            except Exception:
                pass
        t.tx = None
        last = "not reopened"
        for i, delay in enumerate((0.5, 1.5, 3.0, 5.0)):
            await asyncio.sleep(delay)          # give the OS time to release the port
            err = await self._open_once(t)
            if not err:
                self._db.add_event(
                    "INFO", "%s link reset%s (%s)" % (
                        t.label, (" after %d tries" % (i + 1)) if i else "", t.target))
                logger.info("%s reconnected at %s", t.name, t.target)
                return True
            last = err
            if self._is_port_locked(err):
                logger.warning("%s port still locked (try %d/4), waiting: %s",
                               t.name, i + 1, err)
            else:
                logger.warning("%s reopen failed (try %d/4): %s", t.name, i + 1, err)
        t.error = last
        self._db.add_error(t.name, "link reset failed: %s" % last)
        return False

    def _policy(self):
        settings = self._db.all_settings() if hasattr(self._db, "all_settings") else {}
        return TransmissionPolicy.from_settings(settings)

    async def _try_send(self, t, text, ch, policy=None, context=None, manual=False, valid_if=None):
        """Return evidence, not a boolean; never blindly repeat an ambiguous send."""
        policy = policy or self._policy()
        last = TxResult("rejected", "not connected before submission")
        uncertain = None
        retry_used = bool(getattr(self._db, "uncertainty_retry_used", lambda _: False)(context))
        rejection_attempts = 0
        total_attempts = 0
        while rejection_attempts < 3:
            if valid_if is not None and not valid_if():
                return (uncertain or TxResult("not_attempted", "Notice no longer current, selected or live")), "Notice no longer current, selected or live"
            if t.tx is None or not t.connected or not t.tx.connected:
                t.connected = False
                restored = await self._reconnect(t) if t.tx is not None else await self._ensure(t)
                if not restored:
                    rejection_attempts += 1
                    last = uncertain or TxResult("rejected", t.error or "not connected before submission")
                    continue
            log_id = None
            submission_result = None
            def submitted(result):
                nonlocal log_id, submission_result
                if uncertain is not None and uncertain.confirmed:
                    raise TxUnsent("already_confirmed", "Previous attempt confirmed before retry submission")
                if valid_if is not None and not valid_if():
                    raise TxUnsent("stale", "Notice no longer current, selected or live before submission")
                submission_result = result
                result.attempt = total_attempts
                begin = getattr(self._db, "begin_transmission", None)
                if begin:
                    log_id = begin(ch, text, t.name, manual, context, retry=uncertain is not None,
                                   policy=policy, evidence=result.as_record())
                    result.log_id = log_id
                    result.listeners.append(self._db.finish_transmission)
                if result.timestamp is not None and hasattr(self._db, "set_setting"):
                    self._db.set_setting("meshcore_last_tx_timestamp", result.timestamp)
            total_attempts += 1
            if isinstance(t.tx, MeshCoreTransmitter):
                t.tx.policy = policy
                t.tx.on_submission = submitted
                t.tx._last_timestamp = max(t.tx._last_timestamp, int(self._db.get_setting("meshcore_last_tx_timestamp", 0) or 0))
                if policy.repeat_detection and not t.tx._subscriptions:
                    t.tx._subscribe_repeats()
            else:
                submitted(TxResult("submitting", submitted=True))
            try:
                result = await t.tx.send_text(text, ch)
                last = result if isinstance(result, TxResult) else TxResult("local_confirmed", submitted=True, accepted=True)
            except TxUnsent as exc:
                if exc.category == "already_confirmed":
                    return uncertain, uncertain.detail
                last = TxResult("unconfirmed" if exc.category == "unverified" else "not_attempted" if exc.category == "stale" else "rejected", exc.detail,
                                submitted=exc.category == "unverified")
                if exc.category in ("too_large", "no_channel", "stale"):
                    rejection_attempts = 3
            except Exception as exc:
                # Once submission starts, a broken response is not proof of no send.
                last = submission_result if submission_result is not None else TxResult("unconfirmed", submitted=True)
                if last.submitted:
                    last.outcome = "unconfirmed"
                    last.detail = "command interrupted; message may have transmitted: %s" % (str(exc) or type(exc).__name__)
                else:
                    last.outcome = "not_attempted"
                    last.detail = "Submission preparation failed before sending: %s" % (str(exc) or type(exc).__name__)
            finally:
                if isinstance(t.tx, MeshCoreTransmitter):
                    t.tx.on_submission = None
            last.attempt = total_attempts
            if log_id is not None:
                last.log_id = log_id
            if log_id is not None:
                self._db.finish_transmission(last)
            if uncertain is not None and uncertain.confirmed:
                return uncertain, uncertain.detail
            if last.confirmed:
                t.error = ""
                return last, last.detail
            if last.outcome == "unconfirmed":
                uncertain = last
                t.error = ""  # uncertainty is shown as evidence, not a broken connection
                logger.warning("%s transmission unconfirmed (attempt %d): %s", t.name, total_attempts, last.detail)
                if not policy.retry_unconfirmed or retry_used:
                    return last, last.detail
                # Reserve before sleeping. A restart must not reset the retry budget.
                retry_used = True
                last.retry_pending = True
                if last.log_id is not None:
                    self._db.finish_transmission(last)
                reserve = getattr(self._db, "reserve_uncertainty_retry", None)
                if reserve:
                    reserve(context)
                deadline = time.monotonic() + policy.retry_delay_seconds
                while not last.confirmed and time.monotonic() < deadline:
                    if isinstance(t.tx, MeshCoreTransmitter) and last.counter_before is not None:
                        await t.tx.refresh_confirmation(last, min(0.75, max(0.001, deadline - time.monotonic())))
                    remaining = deadline - time.monotonic()
                    if last.confirmed or remaining <= 0:
                        break
                    try:
                        await asyncio.wait_for(last.repeat_event.wait(), min(0.3, remaining))
                    except asyncio.TimeoutError:
                        pass
                last.retry_pending = False
                if last.log_id is not None:
                    self._db.finish_transmission(last)
                # One final bounded read closes the race at the end of the retry delay.
                if isinstance(t.tx, MeshCoreTransmitter) and not last.confirmed:
                    await t.tx.refresh_confirmation(last, 0.75)
                if last.confirmed:
                    return last, last.detail
                if valid_if is not None and not valid_if():
                    return last, last.detail
                continue
            if last.outcome == "not_attempted":
                return uncertain or last, last.detail
            # An earlier ambiguous attempt may have succeeded, even if its retry is rejected.
            if uncertain is not None:
                return uncertain, uncertain.detail
            rejection_attempts += 1
            if last.rejection_category in ("no_channel", "unsupported", "invalid"):
                rejection_attempts = 3
            t.error = last.detail
            logger.warning("%s rejected (attempt %d): %s", t.name, total_attempts, last.detail)
            if rejection_attempts < 3:
                await asyncio.sleep(6 if "duty" in last.detail.lower() else 3 if "queue" in last.detail.lower() else 2)
                if rejection_attempts >= 2:
                    await self._reconnect(t)
        if not last:
            self._db.add_error(t.name, last.detail)
        return last, last.detail

    async def _send_all(self, text: str, manual: bool, on_test: bool | None = None) -> TxResult | bool:
        any_ok = False
        blen = len(text.encode())
        # `on_test` picks the channel (test vs live); `manual` only tags the log
        # (auto vs manual). Automated alerts AND composed manual sends both go on
        # each radio's LIVE channel; only the Troubleshoot test uses the test channel.
        use_test = manual if on_test is None else on_test
        async with self._lock:
            # Manual, test and queued sends share evidence and bounded retry rules.
            for t in self._transports.values():
                if not t.enabled:
                    continue
                ch = t.test_channel if use_test else t.channel
                ok, err = await self._try_send(t, text, ch, manual=manual)
                self._db.add_transmit_log(ch, blen, ok, text, manual,
                                          error=err, transport=t.name)
                if ok:
                    any_ok = ok
                    logger.info("transmission via %s on ch %d: %s", t.name, ch, getattr(ok, "outcome", "local_confirmed"))
        return any_ok

    async def send_manual(self, text: str) -> TxResult | bool:
        # A composed manual broadcast is a real message for people, so it goes on
        # each radio's LIVE channel (logged as a manual action).
        return await self._send_all(text, manual=True, on_test=False)

    async def send_test(self, text: str) -> TxResult | bool:
        # The Troubleshoot canned test goes on each radio's TEST channel.
        return await self._send_all(text, manual=True, on_test=True)

    async def load_channels(self, name: str, conn: str, port: str, host: str):
        """Read channels through the live link, or a temporary link for a new target."""
        async with self._lock:
            t = self._transports.get(name)
            if t is None or name != "meshcore":
                return None, "", "unknown radio"
            conn = conn or "serial"
            target = (host if conn == "tcp" else port) or ""
            if not target.strip():
                return None, "", "configure a USB port or network host to load channels"

            same_target = t.conn == conn and (
                t.target == target or (
                    conn == "serial" and bool(t.target) and
                    Path(t.target).resolve() == Path(target).resolve()
                )
            )
            if same_target:
                if not t.connected or t.tx is None or not t.tx.connected:
                    if not await self._ensure(t):
                        return None, "", t.error or "radio is offline"
                tx = t.tx
            else:
                tx = MeshCoreTransmitter(conn, port or "", host or "")
            try:
                if not same_target:
                    await tx.connect()
                channels = await tx.read_channels()
                try:
                    model = _fmt_model(await tx.read_info())
                except Exception:
                    model = ""
                return channels, model, ""
            except Exception as exc:
                return None, "", str(exc)
            finally:
                if not same_target:
                    try:
                        await tx.close()
                    except Exception:
                        pass

    async def send_to(self, name: str, text: str) -> tuple[bool, str]:
        """Key up a single named radio (bench testing). Returns (ok, error)."""
        blen = len(text.encode())
        async with self._lock:
            t = self._transports.get(name)
            if t is None:
                return False, "unknown radio"
            if not t.enabled:
                return False, "%s is disabled" % t.label
            ch = t.test_channel   # tests go on this radio's test channel
            ok, err = await self._try_send(t, text, ch, manual=True)
            self._db.add_transmit_log(ch, blen, ok, text, True,
                                      error=err, transport=t.name)
            if ok:
                logger.info("test transmitted via %s on ch %d", t.name, ch)
            return ok, err

    async def resend(self, name: str, text: str, channel: int) -> tuple[bool, str]:
        """Re-transmit an exact message on a specific radio and channel, logging a
        fresh entry. Backs the transmit-log Resend button."""
        blen = len(text.encode())
        async with self._lock:
            t = self._transports.get(name)
            if t is None:
                return False, "unknown radio"
            if not t.enabled:
                return False, "%s is disabled" % t.label
            ok, err = await self._try_send(t, text, channel, manual=True)
            self._db.add_transmit_log(channel, blen, ok, text, True,
                                      error=err, transport=t.name)
            if ok:
                logger.info("resent via %s on ch %d", t.name, channel)
            return ok, err

    async def _transmit_item(self, item: QueueItem) -> tuple[bool, str]:
        """Send one queued notice part on the live channel."""
        blen = len(item.text.encode())
        any_ok, last, last_result = False, "", None
        async with self._lock:
            if self._db.get_setting("dry_run", True):
                return TxResult("not_attempted", "Dry Run enabled before queued transmission"), "Dry Run enabled before queued transmission"
            if item.valid_if is not None and not item.valid_if():
                return TxResult("not_attempted", "Notice no longer current or selected before queued transmission"), "Notice no longer current or selected before queued transmission"
            for t in self._transports.values():
                if not t.enabled:
                    continue
                ch = t.channel
                guard = lambda: (not self._db.get_setting("dry_run", True)
                                 and (item.valid_if is None or item.valid_if()))
                ok, err = await self._try_send(t, item.text, ch, policy=item.policy,
                                               context=item.delivery_context, valid_if=guard)
                self._db.add_transmit_log(ch, blen, ok, item.text, False,
                                          error=err, transport=t.name)
                if ok:
                    any_ok = ok
                else:
                    last, last_result = err, ok
        return (any_ok if any_ok else last_result if last_result is not None else TxResult("not_attempted", last or "No enabled transport")), (any_ok.detail if isinstance(any_ok, TxResult) else last)

    async def _worker(self) -> None:
        while not self._stopped:
            if not self._queue:
                self._queue_event.clear()
                try:
                    await self._queue_event.wait()
                except asyncio.CancelledError:
                    return
                continue
            item = self._next_queued_part()
            if item.notice_id is not None:
                self._active_notice = item.notice_id
            if item.verification:
                self._verification_in_flight = True
            try:
                ok, err = await self._transmit_item(item)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # A failed iteration must complete its callback so history can retry.
                logger.exception("transmit worker iteration error")
                ok, err = False, str(exc)
            try:
                if item.on_result is not None:
                    self._safe_result(item.on_result, ok, err)
                if not ok:
                    self._cancel_notice_remainder(item.notice_id, err)
            finally:
                if item.verification:
                    self._verification_in_flight = False
                if (item.notice_id is not None and self._active_notice is item.notice_id
                        and not any(pending.notice_id is item.notice_id for pending in self._queue)):
                    self._active_notice = None
            if self._queue and item.delay_after > 0:
                try:
                    await asyncio.sleep(item.delay_after)
                except asyncio.CancelledError:
                    return
            if not ok:
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, 60.0)

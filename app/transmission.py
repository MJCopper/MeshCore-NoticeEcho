"""Transmission evidence, bounded policy and exact channel-packet matching."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from hashlib import sha256
import hmac
import math
import time

CONFIRMED = {"local_confirmed", "repeat_confirmed", "transmitted"}
COMPLETED = CONFIRMED | {"unconfirmed"}
LABELS = {
    "local_confirmed": "Locally confirmed", "repeat_confirmed": "Repeat confirmed",
    "unconfirmed": "Unconfirmed", "rejected": "Rejected", "not_attempted": "Not attempted",
    "submitting": "Submission in progress", "pending": "Pending", "transmitted": "Locally confirmed (legacy)",
    "failed": "Failed", "interrupted": "Interrupted",
}

@dataclass(frozen=True)
class TransmissionPolicy:
    repeat_detection: bool = True
    confirmation_seconds: float = 5.0
    retry_unconfirmed: bool = False
    retry_delay_seconds: float = 5.0
    late_repeat_seconds: float = 60.0

    @classmethod
    def from_settings(cls, settings):
        def number(key, default, low, high):
            try:
                value = float(settings.get(key, default))
                return min(high, max(low, value)) if math.isfinite(value) else default
            except (TypeError, ValueError):
                return default
        return cls(bool(settings.get("meshcore_repeat_detection", True)),
                   number("meshcore_confirmation_seconds", 5, 1, 30),
                   bool(settings.get("meshcore_retry_unconfirmed", False)),
                   number("meshcore_retry_delay_seconds", 5, 1, 30),
                   number("meshcore_late_repeat_seconds", 60, 10, 300))

@dataclass
class TxResult:
    outcome: str
    detail: str = ""
    submitted: bool = False
    accepted: bool = False
    rejection_category: str = ""
    retry_pending: bool = False
    attempt: int = 0
    started_at: float = field(default_factory=time.time)
    elapsed_seconds: float = 0.0
    counter_before: int | None = None
    counter_after: int | None = None
    counter_reset: bool = False
    packet_id: str = ""
    repeats: int = 0
    timestamp: int | None = None
    repeat_expires_at: float = 0.0
    log_id: int | None = None
    listeners: list = field(default_factory=list, repr=False)
    repeat_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    def __bool__(self):
        # True means the multipart queue may continue, not proven remote delivery.
        return self.outcome in COMPLETED

    @property
    def confirmed(self):
        return self.outcome in CONFIRMED

    @property
    def label(self):
        return LABELS.get(self.outcome, self.outcome)

    def as_record(self):
        return {key: value for key, value in vars(self).items()
                if key not in {"listeners", "repeat_event"}}

    def hear_repeat(self):
        self.repeats += 1
        self.outcome = "repeat_confirmed"
        self.repeat_event.set()
        for callback in tuple(self.listeners):
            callback(self)

    @classmethod
    def legacy(cls, value, detail=""):
        return value if isinstance(value, cls) else cls("local_confirmed" if value else "rejected", detail)


def channel_payload(secret: bytes, sender: str, text: str, timestamp: int) -> bytes:
    """Firmware GROUP_TEXT payload: channel hash, MAC and zero-padded AES128 ECB.

    Match full encrypted bytes, never the SDK's truncated 32-bit packet hash.
    See MeshCore BaseChatMesh::sendGroupMessage and Utils::encryptThenMAC.
    """
    from Crypto.Cipher import AES
    if len(secret) not in (16, 32):
        raise ValueError("Unsupported channel secret length")
    plain = timestamp.to_bytes(4, "little") + b"\x00" + f"{sender}: {text}".encode("utf-8")
    plain += b"\x00" * (-len(plain) % 16)
    encrypted = AES.new(secret[:16], AES.MODE_ECB).encrypt(plain)
    return sha256(secret).digest()[:1] + hmac.digest(secret, encrypted, "sha256")[:2] + encrypted


def received_channel_payload(data) -> bytes | None:
    """Read only complete, flood GROUP_TEXT packets with an actual repeater hop."""
    try:
        raw = data.get("payload")
        if isinstance(raw, str):
            raw = bytes.fromhex(raw)
        if isinstance(raw, bytes):
            if len(raw) < 3:
                return None
            header = raw[0]
            if header >> 6 or (header >> 2) & 15 != 5 or header & 3 not in (0, 1):
                return None
            offset = 5 if header & 3 == 0 else 1
            path_byte = raw[offset]
            count, width = path_byte & 63, (path_byte >> 6) + 1
            offset += 1 + count * width
            if count < 1 or offset >= len(raw):
                return None
            payload = raw[offset:]
        else:
            if (data.get("payload_type") != 5 or data.get("route_type") not in (0, 1)
                    or data.get("payload_ver", 0) != 0 or data.get("path_len", 0) < 1):
                return None
            payload = data.get("pkt_payload")
            if isinstance(payload, str):
                payload = bytes.fromhex(payload)
        if not isinstance(payload, bytes) or len(payload) < 19 or (len(payload) - 3) % 16:
            return None
        return payload
    except (ValueError, TypeError, IndexError):
        return None

class RepeatTracker:
    def __init__(self, limit=256):
        self.limit = limit
        self.pending = []
        self.last_event_at = None

    def prune(self):
        now = time.monotonic()
        self.pending[:] = [entry for entry in self.pending if entry[0] > now]

    def register(self, payload, result, seconds):
        self.prune()
        # Identical live packet identities are ambiguous; never guess an attempt.
        result.packet_id = sha256(payload).hexdigest()
        self.register_digest(result.packet_id, result, seconds)

    def register_digest(self, digest, result, seconds):
        self.prune()
        if seconds <= 0 or len(digest) != 64:
            return
        result.repeat_expires_at = time.time() + seconds
        self.pending.append((time.monotonic() + seconds, digest, result))
        self.pending[:] = self.pending[-self.limit:]

    def extend(self, result, seconds):
        self.pending[:] = [(time.monotonic() + seconds, digest, candidate) if candidate is result else (deadline, digest, candidate)
                           for deadline, digest, candidate in self.pending]
        result.repeat_expires_at = time.time() + seconds

    def discard(self, result):
        self.pending[:] = [entry for entry in self.pending if entry[2] is not result]

    async def receive(self, event):
        self.last_event_at = time.time()
        self.prune()
        payload = received_channel_payload(getattr(event, "payload", {}) or {})
        if payload is None:
            return
        digest = sha256(payload).hexdigest()
        matches = [result for _, expected, result in self.pending if expected == digest and result.submitted]
        if len(matches) == 1:
            matches[0].hear_repeat()

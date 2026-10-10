"""Shared notice admission and part-outcome recording for service pollers."""
from __future__ import annotations

import json

from .transmission import COMPLETED
from .brief import NoticeTooLong


class RecoveryBlocked(NoticeTooLong):
    """Keep partial delivery evidence when its recorded parts cannot fit."""

from .config import BURST_GAP_SECONDS, MULTIPART_GAP_SECONDS, QUEUE_MAX


def submit_notice(tx, parts: list[str], on_result, priority: int = 3, valid_if=None, delivery_context=None) -> bool:
    """Queue an entire notice in order. A full live queue defers every part."""
    entries = [(part, MULTIPART_GAP_SECONDS if index < len(parts) - 1 else BURST_GAP_SECONDS)
               for index, part in enumerate(parts)]
    batch = getattr(tx, "enqueue_notice", None)
    if batch is not None:
        if getattr(tx, "supports_delivery_context", False):
            return batch(entries, on_result=on_result, priority=priority, valid_if=valid_if,
                         delivery_context=delivery_context)
        if getattr(tx, "supports_notice_guards", False):
            return batch(entries, on_result=on_result, priority=priority, valid_if=valid_if)
        return batch(entries, on_result=on_result, priority=priority)
    # Older lightweight transmitter doubles do not expose batch admission.
    if len(parts) > QUEUE_MAX or getattr(tx, "queue_depth", 0) + len(parts) > QUEUE_MAX:
        return False
    for index, (part, delay) in enumerate(entries):
        if not tx.enqueue(part, on_result=lambda ok, err="", i=index: on_result(i, ok, err),
                          delay_after=delay):
            return False
    return True


def record_part(db, row_id: int, index: int, total: int, ok: bool, error: str = "") -> None:
    recorder = getattr(db, "record_delivery_part", None)
    if recorder is not None:
        recorder(row_id, index, total, ok, error)


def remaining_parts(row, text: str, parts: list[str]) -> list[int]:
    """Skip confirmed or potentially delivered parts on an ordinary recovery poll."""
    if row is None or row["transmitted_text"] != text:
        return list(range(len(parts)))
    try:
        raw = row["delivery_parts"]
        outcomes = json.loads(raw) if isinstance(raw, str) else (raw or [])
    except (KeyError, IndexError, TypeError, ValueError):
        outcomes = []
    if len(outcomes) != len(parts):
        return list(range(len(parts)))
    return [index for index, outcome in enumerate(outcomes)
            if outcome.get("status") not in COMPLETED]


def queue_refusal(parts: list[str]) -> tuple[str, str]:
    return "deferred", "Waiting for notice or byte capacity in the MeshCore send queue"


def permanently_unsendable(row, revision: str, text: str) -> bool:
    """Legacy oversized failures are eligible for streaming retry."""
    # Previously oversized notices can now stream; do not strand old failed rows.
    return False


def recovery_parts(row, revision, budget, *, force=False):
    """Pin an in-progress revision's recorded wording across formatter upgrades.

    An explicit resend starts fresh, except deferred work that has not been admitted.
    Oversized legacy failures can still use the current formatter if no part completed.
    """
    if row is None or row["revision_hash"] != revision or row["transmit_status"] not in ("failed", "interrupted", "deferred"):
        return None
    if force and row["transmit_status"] != "deferred":
        return None
    text = row["transmitted_text"] or ""
    if not text:
        return None
    parts = text.split(" || ")
    if all(part and len(part.encode("utf-8")) <= budget for part in parts):
        return parts
    try:
        outcomes = json.loads(row["delivery_parts"] or "[]")
    except (KeyError, IndexError, TypeError, ValueError):
        outcomes = []
    if any(outcome.get("status") in COMPLETED for outcome in outcomes):
        raise RecoveryBlocked("Formatting blocked: recorded partial notice exceeds current byte budget; original wording must be retained")
    return None

"""Shared, read-only notice lifecycle rules and provider snapshot adapters."""
from __future__ import annotations

from datetime import datetime
import json
import math
from typing import Literal

View = Literal["current", "upcoming", "past", "all"]
VIEWS = {"current": "Current", "upcoming": "Upcoming", "past": "Past", "all": "All saved"}
LABELS = {"current": "Current notice", "upcoming": "Upcoming notice", "cancelled": "Cancelled",
          "ended": "Ended", "expired": "Expired", "absent": "No longer in feed",
          "unknown": "Presence not yet confirmed"}
PAST = ("cancelled", "ended", "expired", "absent")

SCHEMA = """
CREATE TABLE IF NOT EXISTS source_catalogue (
    source TEXT NOT NULL, feed TEXT NOT NULL, item_id TEXT NOT NULL,
    payload TEXT NOT NULL, present INTEGER,
    cancelled INTEGER NOT NULL DEFAULT 0, ended INTEGER NOT NULL DEFAULT 0,
    starts_at REAL, expires_at REAL, first_seen TEXT, last_seen TEXT NOT NULL,
    missing_since TEXT, missing_polls INTEGER NOT NULL DEFAULT 0,
    snapshot_id TEXT NOT NULL DEFAULT '', imported INTEGER NOT NULL DEFAULT 0,
    priority INTEGER NOT NULL DEFAULT 0, sort_time REAL NOT NULL DEFAULT 0,
    PRIMARY KEY(source, feed, item_id)
);
CREATE INDEX IF NOT EXISTS idx_catalogue_source_feed ON source_catalogue(source, feed, present, last_seen);
CREATE TABLE IF NOT EXISTS source_snapshots (
    source TEXT NOT NULL, feed TEXT NOT NULL, last_success TEXT NOT NULL,
    PRIMARY KEY(source, feed)
);
"""


def epoch(value):
    """Only timezone-aware dates and finite numeric epochs establish boundaries."""
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        if isinstance(value, (int, float)):
            result = float(value)
        else:
            date = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if date.tzinfo is None:
                return None
            result = date.timestamp()
        return result if math.isfinite(result) else None
    except (ValueError, TypeError, OverflowError):
        return None


def lifecycle(present, cancelled, ended, starts_at, expires_at, now):
    """Also registered as a SQLite function, so counts and rows use one rule."""
    if cancelled:
        return "cancelled"
    if ended:
        return "ended"
    if expires_at is not None and expires_at <= now:
        return "expired"
    if present == 0:
        return "absent"
    if present is None:
        return "unknown"
    if starts_at is not None and starts_at > now:
        return "upcoming"
    return "current"


def adapt(source, row):
    """Extract lifecycle facts without interpreting delivery or selection state."""
    row = dict(row)
    data = json.loads(row.get("raw_data" if source == "bom" else "normalized_data") or "{}")
    if source == "bom":
        return dict(feed=row["region"], item_id=row["alert_id"],
                    cancelled=row.get("message_type") == "Cancel", ended=False,
                    starts_at=epoch(data.get("onset") or data.get("effective")),
                    expires_at=epoch(row.get("expires")), priority=0,
                    sort_time=epoch(row.get("issued")) or 0,
                    last_seen=row["fetched_at"])
    if source == "rfs":
        # RFS update/publication strings have no established timezone. Status
        # "Under control" is still a current incident, not an explicit ending.
        status = row.get("status", "").strip().casefold()
        return dict(feed="incidents", item_id=row["incident_id"],
                    cancelled=status in ("cancelled", "canceled"),
                    ended=status in ("out", "extinguished", "ended", "resolved", "closed"),
                    starts_at=None, expires_at=None,
                    priority={"Emergency Warning": 0, "Watch and Act": 1, "Advice": 2}.get(row.get("level"), 3),
                    sort_time=epoch(row.get("last_seen")) or 0, last_seen=row["last_seen"])
    return dict(feed=row["feed"], item_id=row["item_id"], cancelled=False,
                ended=data.get("ended") is True, starts_at=epoch(data.get("start")),
                expires_at=epoch(data.get("end")), priority=0,
                sort_time=epoch(data.get("updated")) or epoch(row.get("last_seen")) or 0,
                last_seen=row["last_seen"])

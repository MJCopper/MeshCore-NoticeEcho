"""Common current-list pagination and explicit snapshot freshness labels."""
from datetime import datetime, timezone


def freshness(stamp: str, minutes: int, enabled: bool = True) -> str:
    if not enabled:
        return "Monitoring disabled — saved data is not being refreshed"
    if not stamp:
        return "No successful fetch recorded"
    try:
        parsed = datetime.fromisoformat(stamp)
        if parsed.tzinfo is None:
            return "Fetch timezone is unknown"
        age = (datetime.now(timezone.utc) - parsed).total_seconds()
    except (TypeError, ValueError):
        return "Fetch timestamp unavailable"
    if age < -60:
        return "Fetch timestamp is in the future — check clock settings"
    if age > max(10, minutes * 2) * 60:
        return f"Stale snapshot — last successful fetch {int(age // 60)} minutes ago"
    return "Recently fetched — provider data freshness is not guaranteed"


def traffic_feed_status(settings, name, last_success):
    """Explain feed selection independently of RFS incident monitoring."""
    enabled = bool(settings.get("traffic_enabled", False))
    requested = enabled
    if not enabled:
        label = "Service disabled"
    elif not last_success:
        label = "Awaiting first poll"
    else:
        label = freshness(last_success, settings.get("traffic_poll_minutes", 5))
    return requested, label


def listing_status(db, source, pagination, settings, last_attempt, result):
    """Keep source health independent of an item's lifecycle classification."""
    enabled = bool(settings.get(source + "_enabled", source == "bom"))
    snapshots = pagination["snapshots"]
    last_success = max((row["last_success"] for row in snapshots), default="")
    if not last_success:
        last_success = settings.get(source + "_last_successful_poll", "")
    if not last_success and source == "bom":
        last_success = max((row["fetched_at"] for row in db.bom_snapshot_regions(["NSW"])), default="")
    return dict(enabled=enabled, last_attempt=last_attempt, last_success=last_success,
                has_snapshot=bool(last_success), result=result,
                fetch_problem=str(result).lower().startswith(("error", "partial")),
                freshness=freshness(last_success, settings.get(source + "_poll_minutes", 5), enabled))

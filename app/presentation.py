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
        label = freshness(last_success, settings.get("traffic_poll_minutes", 10))
    return requested, label

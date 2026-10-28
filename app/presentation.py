"""Common current-list pagination and explicit snapshot freshness labels."""
from datetime import datetime, timezone
import re


def rfs_notice_display(title, metadata=None):
    """Describe saved RFS records for the web without changing provider/TX data."""
    metadata = metadata if isinstance(metadata, dict) else {}

    def text(value):
        return str(value or '').strip()

    def meaningful(value):
        key = ' '.join(re.findall(r'[^\W_]+', value.casefold()))
        return bool(key) and key not in {
            'not applicable', 'n a', 'unrecognized', 'unrecognised', 'not supplied',
            'unknown', 'other', 'notice type not supplied',
        }

    def starts_with(value, prefix):
        words = re.findall(r'[^\W_]+', prefix)
        pattern = r'^' + r'[\W_]+'.join(re.escape(word) for word in words) + r'(?=$|[\W_])'
        return bool(words) and bool(re.match(pattern, value, re.I))

    level = text(metadata.get('level'))
    kind = text(metadata.get('kind'))
    kind = kind if meaningful(kind) else 'Incident'
    name = text(title) or text(metadata.get('location'))
    parts = []
    if meaningful(level) and not starts_with(kind, level) and not starts_with(name, level):
        parts.append(level)
    if not starts_with(name, kind):
        parts.append(kind)
    if name:
        parts.append(name)
    return dict(heading=' · '.join(parts), alert_level=level or 'Not supplied',
                status=text(metadata.get('status')) or 'Not supplied')


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

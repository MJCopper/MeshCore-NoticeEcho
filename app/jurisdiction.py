"""NSW jurisdiction evidence, independent of the selected council filter."""
from functools import lru_cache
import gzip
import json
import math
from pathlib import Path

STATE_NAMES = {
    "NSW": "NSW", "NEW SOUTH WALES": "NSW",
    "VIC": "VIC", "VICTORIA": "VIC", "QLD": "QLD", "QUEENSLAND": "QLD",
    "ACT": "ACT", "AUSTRALIAN CAPITAL TERRITORY": "ACT",
    "SA": "SA", "SOUTH AUSTRALIA": "SA", "WA": "WA", "WESTERN AUSTRALIA": "WA",
    "TAS": "TAS", "TASMANIA": "TAS", "NT": "NT", "NORTHERN TERRITORY": "NT",
}


def coordinates(value):
    try:
        lon, lat = float(value[0]), float(value[1])
        if math.isfinite(lon) and math.isfinite(lat) and -180 <= lon <= 180 and -90 <= lat <= 90:
            return lon, lat
    except (ValueError, TypeError, IndexError, KeyError):
        pass
    return None, None


@lru_cache(maxsize=1)
def nsw_boundaries():
    from .traffic.feed import prepare_councils
    path = Path(__file__).parent / "traffic" / "nsw_lga.geojson.gz"
    with gzip.open(path, "rt", encoding="utf-8") as source:
        features = json.load(source).get("features", [])
    if len(features) < 100:
        raise ValueError("NSW boundary snapshot is incomplete")
    return prepare_councils(features)


def point_jurisdiction(lon, lat):
    from .traffic.feed import council_at_prepared
    lon, lat = coordinates((lon, lat))
    if lon is None:
        return "unknown", "Coordinates are missing or invalid"
    try:
        prepared = nsw_boundaries()
    except (OSError, ValueError, KeyError, TypeError):
        return "unknown", "NSW boundary snapshot is unavailable"
    if council_at_prepared(lon, lat, prepared):
        return "NSW", "Point lies within the simplified NSW boundary snapshot"
    # Keep points near a simplified exterior boundary uncertain rather than
    # treating small snapshot differences as proof that the notice is interstate.
    tolerance = .003  # roughly 300 metres
    for left, bottom, right, top, _, rings in prepared:
        if not (left-tolerance <= lon <= right+tolerance and bottom-tolerance <= lat <= top+tolerance):
            continue
        for ring in rings:
            for a, b in zip(ring, ring[1:] + ring[:1]):
                dx, dy = b[0]-a[0], b[1]-a[1]
                length = dx*dx + dy*dy
                fraction = max(0., min(1., ((lon-a[0])*dx + (lat-a[1])*dy)/length)) if length else 0.
                if (lon-a[0]-fraction*dx)**2 + (lat-a[1]-fraction*dy)**2 <= tolerance*tolerance:
                    return "unknown", "Point is near a simplified NSW exterior boundary"
    return "outside", "Point lies outside the NSW boundary snapshot"


def resolve(item, council=""):
    from .rfs.councils import COUNCILS
    from .rfs.feed import council_key
    state = STATE_NAMES.get(str(getattr(item, "state", "") or "").strip().upper())
    if state and state != "NSW":
        return state, f"Provider supplied state {state}"
    point, reason = point_jurisdiction(getattr(item, "lon", None), getattr(item, "lat", None))
    if point == "outside":
        return point, reason
    if state == "NSW":
        return "NSW", "Provider supplied state NSW"
    if point == "NSW":
        return point, reason
    if council_key(council or getattr(item, "council", "")) in {council_key(x) for x in COUNCILS}:
        return "NSW", "Provider or boundary match identifies a NSW council"
    return "unknown", reason + "; no recognised provider state or NSW council"

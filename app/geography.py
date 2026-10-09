"""Universal geographic policy and service-specific evidence adapters."""
from dataclasses import asdict, dataclass, replace
from html import unescape
import re
import unicodedata

from .rfs.councils import COUNCILS
from .rfs.feed import council_key

KEY = "geographic_policy"


def normalise(value):
    text = unescape(re.sub(r"<[^>]*>", " ", str(value or "")))
    text = unicodedata.normalize("NFKC", text).casefold()
    return " ".join(re.findall(r"[^\W_]+", text, re.UNICODE))


def contains(text, term):
    return bool(normalise(term) and f" {normalise(term)} " in f" {normalise(text)} ")


def occurrences(text, phrase):
    """Token spans in normalized text, including overlapping occurrences."""
    tokens, needle = normalise(text).split(), normalise(phrase).split()
    return tuple((i, i + len(needle)) for i in range(len(tokens) - len(needle) + 1)
                 if needle and tokens[i:i + len(needle)] == needle)


def location_matches(fields, positive_terms, exclusions):
    matches, found, suppressed = [], [], []
    for field, value in fields:
        spans = [(phrase, start, end) for phrase in exclusions for start, end in occurrences(value, phrase)]
        found.extend((phrase, field, start, end) for phrase, start, end in spans)
        for term in positive_terms:
            for start, end in occurrences(value, term):
                blockers = [phrase for phrase, left, right in spans if left <= start and end <= right]
                if blockers:
                    suppressed.extend((term, field, phrase, start, end) for phrase in blockers)
                else:
                    matches.append((term, field))
    return tuple(dict.fromkeys(matches)), tuple(dict.fromkeys(found)), tuple(dict.fromkeys(suppressed))


def terms(values):
    values = values.splitlines() if isinstance(values, str) else values
    result, seen = [], set()
    for value in values:
        value = " ".join(str(value).strip().split())
        key = normalise(value)
        if not key:
            continue
        if len(value) > 120:
            raise ValueError("Each location term or district must be 120 characters or fewer")
        if key not in seen:
            seen.add(key)
            result.append(value)
    if len(result) > 400:
        raise ValueError("Use no more than 400 location terms")
    return result


def configuration(all_nsw=False, councils=(), location_terms=(), bom_districts=(), include_uncertain=False, location_exclusions=()):
    catalogue = {council_key(name): name for name in COUNCILS}
    selected = []
    for name in councils:
        canonical = catalogue.get(council_key(name))
        if not canonical:
            raise ValueError(f"Unknown council: {name}")
        if canonical not in selected:
            selected.append(canonical)
    combined = terms([*terms(location_terms), *terms(bom_districts)])
    return dict(version=2, active=True, all_nsw=bool(all_nsw), councils=sorted(selected),
                location_terms=combined, location_exclusions=terms(location_exclusions),
                include_uncertain=bool(include_uncertain))


def active(settings):
    return bool((settings.get(KEY) or {}).get("active"))


def needs_term_merge(settings):
    policy = settings.get(KEY) or {}
    return bool(policy.get("active") and (policy.get("version", 1) < 2 or policy.get("bom_districts")))


def proposal(settings):
    if active(settings):
        policy = settings[KEY]
        return configuration(policy["all_nsw"], policy["councils"], policy.get("location_terms", []),
                             policy.get("bom_districts", []), policy["include_uncertain"], policy.get("location_exclusions", []))
    # Include all saved service selections, including disabled services, and make
    # broadening explicit before activation rather than silently merging defaults.
    return configuration(any(settings.get(f"{s}_all_councils", s == "bom") for s in ("bom", "rfs", "traffic")),
                         list(dict.fromkeys(x for s in ("bom", "rfs", "traffic") for x in settings.get(f"{s}_councils", []))),
                         bom_districts=settings.get("bom_districts", []),
                         include_uncertain=settings.get("bom_include_unknown_councils", True))


@dataclass(frozen=True)
class Coverage:
    included: bool
    councils: tuple = ()
    districts: tuple = ()
    term_matches: tuple = ()
    method: str = ""
    reason: str = ""
    policy: dict | None = None
    jurisdiction: str = "unknown"
    jurisdiction_reason: str = ""

    exclusion_matches: tuple = ()
    suppressed_matches: tuple = ()

    def metadata(self):
        return asdict(self)


def evaluate(policy, service, councils=(), fields=(), district_fields=(), jurisdiction="NSW", uncertain=False, jurisdiction_reason=""):
    evidence = dict(policy=policy, jurisdiction=jurisdiction, jurisdiction_reason=jurisdiction_reason)
    if jurisdiction not in ("NSW", "unknown"):
        return Coverage(False, reason="Excluded: notice is outside NSW" + ("; " + jurisdiction_reason if jurisdiction_reason else ""), **evidence)
    shared_terms = policy.get("location_terms", [])
    districts_only = policy.get("bom_districts", []) if policy.get("version", 1) < 2 else []
    configured = bool(policy["all_nsw"] or policy["councils"] or shared_terms or
                      service == "bom" and districts_only)
    if not configured:
        return Coverage(False, reason="Excluded: no geographic coverage configured", **evidence)
    if jurisdiction == "unknown":
        if policy["include_uncertain"]:
            return Coverage(True, method="uncertain geography", reason="Included: uncertain NSW jurisdiction fallback", **evidence)
        return Coverage(False, reason="Excluded: NSW jurisdiction cannot be established", **evidence)
    if policy["all_nsw"] and jurisdiction == "NSW":
        return Coverage(True, method="all NSW", reason="Included: all NSW", **evidence)
    selected = {council_key(x) for x in policy["councils"]}
    matched_councils = tuple(x for x in councils if council_key(x) in selected)
    if service == "bom" and policy.get("version", 1) >= 2:
        fields = (*fields, *(("affected area", value) for value in district_fields))
    matches, excluded, suppressed = location_matches(fields, shared_terms, policy.get("location_exclusions", []))
    evidence.update(exclusion_matches=excluded, suppressed_matches=suppressed)
    ignored = "; ".join(dict.fromkeys(f'ignored “{term}” within “{phrase}” — {field}' for term, field, phrase, start, end in suppressed))
    districts = tuple(name for name in districts_only if service == "bom" and
                      any(contains(value, name) for value in district_fields))
    reasons = []
    if matched_councils:
        reasons.append("selected council — " + ", ".join(matched_councils))
    if districts:
        reasons.append("BOM forecast district — " + ", ".join(districts))
    if matches:
        reasons.extend(f'location term “{term}” — {field}' for term, field in matches)
    if reasons:
        return Coverage(True, matched_councils, districts, matches, "OR match", "Included: " + "; ".join(reasons) + ("; " + ignored if ignored else ""), **evidence)
    if uncertain and policy["include_uncertain"]:
        return Coverage(True, method="uncertain geography", reason="Included: uncertain geography fallback" + ("; " + ignored if ignored else ""), **evidence)
    return Coverage(False, reason=("Excluded: location terms matched only within excluded phrases; " + ignored if suppressed else "Excluded: no geographic coverage matched"), **evidence)


def incident_coverage(item, settings):
    if not active(settings):
        selected = COUNCILS if settings.get("rfs_all_councils") else settings.get("rfs_councils", [])
        included = council_key(item.council) in {council_key(x) for x in selected}
        return Coverage(included, reason="Included: selected council" if included else "Excluded: council selection")
    known = {council_key(x) for x in COUNCILS}
    # Generic incident causes are not location-bearing names.
    from .jurisdiction import resolve
    jurisdiction, jurisdiction_reason = resolve(item)
    generic = {normalise(item.kind), "fire", "bush fire", "grass fire", "structure fire", "other"}
    name = "" if normalise(item.name) in generic else item.name
    return evaluate(settings[KEY], "rfs", (item.council,) if council_key(item.council) in known else (),
                    (("incident location", item.location), ("incident name", name)),
                    uncertain=council_key(item.council) not in known,
                    jurisdiction=jurisdiction, jurisdiction_reason=jurisdiction_reason)


def traffic_coverage(item, council, settings):
    if not active(settings):
        selected = COUNCILS if settings.get("traffic_all_councils") else settings.get("traffic_councils", [])
        included = bool(council and council_key(council) in {council_key(x) for x in selected})
        return Coverage(included, reason="Included: selected council" if included else "Excluded: council selection")
    from .jurisdiction import resolve
    jurisdiction, jurisdiction_reason = resolve(item, council)
    return evaluate(settings[KEY], "traffic", (council,) if council else (),
                    (("road", item.road), ("suburb", item.suburb), ("road details", item.road_details)),
                    uncertain=not council, jurisdiction=jurisdiction, jurisdiction_reason=jurisdiction_reason)


def bom_coverage(alert, match, settings, enrichment=None):
    from .bom_geography import affected_district_texts, affected_active_clauses, match_geography
    if not active(settings):
        return match_geography(alert, match, settings, enrichment)
    texts, cancelled, active_sentences = affected_district_texts(alert, enrichment)
    explicit = affected_active_clauses(active_sentences)
    # Remove cancellation-only scopes from both term and district evidence.
    policy = settings[KEY]
    safe = []
    for value in texts:
        value = normalise(value)
        for scope in cancelled:
            for name in (*policy["location_terms"], *policy.get("bom_districts", [])):
                if contains(scope, name) and not any(contains(x, name) for x in explicit):
                    value = re.sub(r"(?<!\w)" + re.escape(normalise(name)) + r"(?!\w)", "", value)
        safe.append(value)
    return evaluate(policy, "bom", match.councils,
                    tuple(("affected area", x) for x in safe), tuple(safe),
                    jurisdiction=alert.raw.get("region", "NSW"), uncertain=match.status == "unknown")


def closure_coverage(coverage, db, service, notice_id, terminal, references=()):
    """Apply the same previous-transmission exception in delivery and diagnostics."""
    if not getattr(coverage, "policy", None) or not terminal or coverage.jurisdiction not in ("NSW", "unknown"):
        return coverage
    previous = db.latest_successful_broadcast(service, notice_id)
    if service == "bom" and not previous:
        for candidate in (notice_id, *references):
            state = db.get_state(candidate)
            if state and state["disposition"] in ("sent", "update", "cancelled"):
                previous = True
                break
    if not previous:
        return coverage
    reason = "Included: cancellation of previously transmitted warning" if service == "bom" else (
             "Included: closure of previously transmitted notice" if service == "rfs" else
             "Included: end of previously transmitted notice")
    return replace(coverage, included=True, method="previous transmission", reason=reason)


def incident_delivery_coverage(item, settings, db):
    return closure_coverage(incident_coverage(item, settings), db, "rfs", item.incident_id,
                            item.status.casefold().strip() in {"out", "extinguished", "resolved", "closed"})


def traffic_delivery_coverage(item, council, settings, db):
    return closure_coverage(traffic_coverage(item, council, settings), db, "traffic", item.item_id, item.ended)


def bom_delivery_coverage(alert, match, settings, db, enrichment=None):
    from .bom_geography import cancellation_only
    coverage = bom_coverage(alert, match, settings, enrichment)
    terminal = alert.message_type == "Cancel" or (not coverage.included and cancellation_only(alert) is not None)
    return closure_coverage(coverage, db, "bom", alert.alert_id, terminal, alert.references)


def saved_evidence(item, row):
    """A metadata-only provider change must also invalidate queued coverage."""
    import json
    data = json.loads(row["normalized_data"] or "{}")
    return replace(item, **{key: data[key] for key in ("state", "lon", "lat", "council")
                            if key in data and hasattr(item, key)})

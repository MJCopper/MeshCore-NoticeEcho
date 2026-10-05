"""BOM coverage: selected councils OR affected forecast districts."""
from dataclasses import dataclass
from html import unescape
import re

from .rfs.feed import council_key


@dataclass(frozen=True)
class GeographyMatch:
    included: bool
    councils: tuple[str, ...] = ()
    districts: tuple[str, ...] = ()
    method: str = ""
    reason: str = ""


def _clean(value):
    return " ".join(unescape(re.sub(r"<[^>]*>", " ", value or "")).split())


def _contains(text, name):
    # Full name boundaries prevent Hunter matching Hunterville, for example.
    words = re.escape(_clean(name)).replace(r"\ ", r"\s+")
    return bool(words and re.search(r"(?<!\w)" + words + r"(?!\w)", text, re.I))


def affected_district_texts(alert, enrichment=None):
    """Use affected-area fields/clauses, excluding cancellation-only mentions.

    Provider area fields and explicit affected-area clauses establish scope.
    Marine sections carry their own active/cancelled scope.
    """
    sections = getattr(alert, "warning_sections", ()) or ()
    if sections:
        return (tuple(_clean(s.areas) for s in sections
                      if s.phase != "CAN" and s.phenomenon.casefold() != "cancellation"), (), ())
    narrative = _clean(alert.warning_summary or alert.detail)
    sentences = re.split(r"(?<=[.!?])\s+", narrative)
    cancellation_scopes = []
    active_sentences = []
    for sentence in sentences:
        if re.search(r"\b(?:the|this) warning(?: for .+?)? (?:is|has been) cancelled\b", sentence, re.I):
            scope = re.search(r"(?:no longer occurring in|warning for) (.+?)(?:,? and the warning| (?:is|has been) cancelled|$)", sentence, re.I)
            if scope:
                cancellation_scopes.append(scope[1])
        else:
            active_sentences.append(sentence)
    result = [_clean(alert.area_desc), _clean(alert.specific_locations)]
    result.extend(_clean(x) for x in getattr(enrichment, "area_names", ()) or ())
    for kind, _, name in getattr(enrichment, "geocodes", ()) or ():
        if kind.casefold() not in {"lga", "aac:lga", "abs:lga", "state", "region", "aac:region"}:
            result.append(_clean(name))
    headline = _clean(alert.headline)
    # Headlines are used only for explicitly introduced affected areas.
    for sentence in active_sentences + [headline]:
        if re.search(r"\bcancel(?:led|lation)\b", sentence, re.I):
            continue
        for match in re.finditer(
            r"\b(?:locations which may be affected include|affected areas include|"
            r"forecast to affect|detected near|affecting|"
            r"(?:for|in|over) (?:people in )?parts of)\s+(.+?)(?:\.|$)",
            sentence, re.I,
        ):
            result.append(match[1])
    if not re.search(r"\bcancel(?:led|lation)\b", headline, re.I):
        match = re.search(r"\bwarning\s+for\s+(.+?)(?:\.|$)", headline, re.I)
        if match:
            result.append(match[1])
    # Return cancellation scopes separately so matching can subtract them from
    # broad provider fields while retaining explicit active mentions.
    return tuple(result), tuple(cancellation_scopes), tuple(active_sentences)


def match_geography(alert, council_match, settings, enrichment=None):
    if settings.get("bom_all_councils", True):
        return GeographyMatch(True, method="all NSW", reason="Included: all NSW council areas")
    selected = {council_key(x) for x in settings.get("bom_councils", []) if str(x).strip()}
    district_names = tuple(dict.fromkeys(_clean(x) for x in settings.get("bom_districts", []) if _clean(x)))
    if not selected and not district_names:
        return GeographyMatch(False, reason="No geographic coverage selected")
    councils = tuple(x for x in council_match.councils if council_key(x) in selected)
    scopes = affected_district_texts(alert, enrichment)
    active_texts, cancellation_scopes, active_sentences = scopes
    districts = []
    for name in district_names:
        if not any(_contains(text, name) for text in active_texts):
            continue
        if any(_contains(text, name) for text in cancellation_scopes):
            # A scope explicitly cancelled cannot be rescued by a broad area
            # field or background narrative. Require an active affected clause.
            explicit_active = affected_active_clauses(active_sentences)
            if not any(_contains(text, name) for text in explicit_active):
                continue
        districts.append(name)
    reasons = []
    if councils:
        reasons.append("council match: " + ", ".join(councils))
    if districts:
        reasons.append("forecast district match: " + ", ".join(districts))
    if reasons:
        return GeographyMatch(True, councils, tuple(districts), "council OR district",
                              "Included: " + "; ".join(reasons))
    if council_match.status == "unknown" and settings.get("bom_include_unknown_councils", True):
        return GeographyMatch(True, method="unknown geography",
                              reason="Included: council geography unknown; fallback enabled")
    return GeographyMatch(False, reason="Excluded: no selected council or forecast district matched")


def affected_active_clauses(sentences):
    result = []
    for sentence in sentences:
        for match in re.finditer(
            r"\b(?:locations which may be affected include|affected areas include|"
            r"forecast to affect|detected near|affecting|"
            r"(?:for|in|over) (?:people in )?parts of)\s+(.+?)(?:\.|$)",
            sentence, re.I,
        ):
            result.append(match[1])
    return result


def cancellation_only(alert):
    """Prepare only cancelled scopes from a mixed product, leaving source data intact."""
    from dataclasses import replace
    scopes = []
    for section in getattr(alert, "warning_sections", ()) or ():
        if section.phase == "CAN" or section.phenomenon.casefold() == "cancellation":
            scopes.append(_clean(section.areas))
    for sentence in re.split(r"(?<=[.!?])\s+", _clean(alert.warning_summary or alert.detail)):
        if not re.search(r"\b(?:the|this) warning(?: for .+?)? (?:is|has been) cancelled\b", sentence, re.I):
            continue
        scope = re.search(r"(?:no longer occurring in|warning for) (.+?)(?:,? and the warning| (?:is|has been) cancelled|$)", sentence, re.I)
        if scope:
            scopes.append(scope[1])
    scopes = list(dict.fromkeys(s for s in scopes if s))
    if not scopes:
        return None
    return replace(alert, message_type="Cancel", area_desc=", ".join(scopes),
                   specific_locations=", ".join(scopes), warning_summary="", detail="",
                   warning_sections=())

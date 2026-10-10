"""Versioned notice classification. Raw provider labels never become severity guesses."""
from __future__ import annotations
import copy
import re
from dataclasses import dataclass, asdict

VERSION = 2
REVIEWED = "2026-10-10"
UNKNOWN = "unrecognized"
MISSING = "not-supplied"
SOURCES = {"bom": "https://www.bom.gov.au/weather-services/severe-weather-knowledge-centre/",
           "rfs": "https://www.rfs.nsw.gov.au/fire-information/fires-near-me",
           "traffic": "https://www.livetraffic.com/"}
LABELS = {
 "bom": {"type": ["Severe Thunderstorm Warning", "Severe Weather Warning", "Flood Warning", "Flood Watch", "Fire Weather Warning", "Heatwave Warning", "Tropical Cyclone Warning", "Tropical Cyclone Advice", "Tsunami Warning", "Tsunami Watch", "Marine Wind Warning", "Hazardous Surf Warning", "Damaging Surf Warning", "Coastal Hazard Warning", "Frost Warning", "Warning to Sheep Graziers", "Road Weather Alert", "Bush Walkers Weather Alert"]},
 "rfs": {"level": ["Emergency Warning", "Watch and Act", "Advice", "Not Applicable", "Planned Burn"],
         "kind": ["Grass Fire", "MVA/Transport", "Bush Fire", "Other", "Fire Alarm", "Burn off", "Assist Other Agency", "Car Fire", "Structure Fire", "Vehicle/Equipment Fire", "Medical", "Flood/Storm/Tree Down", "Hazard Reduction", "Planned Event", "Truck Fire", "Fire Had Occurred", "HAZMAT", "Search/Rescue", "Rescue Road Crash", "Haystack fire"]},
 "traffic": {"category": ["SCHEDULED ROADWORK", "BREAKDOWN", "CRASH", "CHANGED TRAFFIC CONDITIONS", "HAZARD", "ADVERSE WEATHER", "SPECIAL EVENT", "TRAFFIC LIGHTS FLASHING YELLOW", "OPEN TO 4WD ONLY", "FLOODING", "TRAFFIC LIGHTS BLACKED OUT", "EMERGENCY ROADWORK", "SMOKE", "FERRY OUT OF SERVICE", "POLICE OPERATION", "HOLIDAY TRAFFIC", "GRASS FIRE", "HEAVY TRAFFIC", "BUSHFIRE", "BURST WATER MAIN", "BUILDING FIRE", "TRAFFIC LIGHTS"]}}

# Reviewed, explicit membership. Filtering never infers a group from label words.
GROUP_MEMBERS = {
 "bom": {"type": {
  "warnings": ("Warnings", LABELS["bom"]["type"][:3] + ["Fire Weather Warning", "Heatwave Warning", "Tropical Cyclone Warning", "Tsunami Warning", "Marine Wind Warning", "Hazardous Surf Warning", "Damaging Surf Warning", "Coastal Hazard Warning", "Frost Warning", "Warning to Sheep Graziers"]),
  "watches-advice": ("Watches and Advice", ["Flood Watch", "Tropical Cyclone Advice", "Tsunami Watch"]),
  "alerts": ("Alerts", ["Road Weather Alert", "Bush Walkers Weather Alert"])}},
 "rfs": {
  "level": {"alerts": ("Alert levels", ["Emergency Warning", "Watch and Act", "Advice"]), "other": ("Other levels", ["Not Applicable", "Planned Burn"])},
  "kind": {"fire": ("Fire", ["Grass Fire", "Bush Fire", "Fire Alarm", "Car Fire", "Structure Fire", "Vehicle/Equipment Fire", "Truck Fire", "Fire Had Occurred", "Haystack fire"]),
           "planned": ("Planned activity", ["Burn off", "Hazard Reduction", "Planned Event"]),
           "rescue-medical": ("Rescue and Medical", ["MVA/Transport", "Medical", "Search/Rescue", "Rescue Road Crash"]),
           "other": ("Other", ["Other", "Assist Other Agency", "Flood/Storm/Tree Down", "HAZMAT"])}},
 "traffic": {"category": {
  "fire": ("Fire", ["SMOKE", "GRASS FIRE", "BUSHFIRE", "BUILDING FIRE"]),
  "roadworks": ("Roadworks", ["SCHEDULED ROADWORK", "EMERGENCY ROADWORK"]),
  "traffic-lights": ("Traffic lights", ["TRAFFIC LIGHTS", "TRAFFIC LIGHTS FLASHING YELLOW", "TRAFFIC LIGHTS BLACKED OUT"]),
  "weather": ("Weather and Flooding", ["ADVERSE WEATHER", "FLOODING"]),
  "other": ("Other", ["BREAKDOWN", "CRASH", "CHANGED TRAFFIC CONDITIONS", "HAZARD", "SPECIAL EVENT", "OPEN TO 4WD ONLY", "FERRY OUT OF SERVICE", "POLICE OPERATION", "HOLIDAY TRAFFIC", "HEAVY TRAFFIC", "BURST WATER MAIN"])}}}

def group_definitions(service, dimension):
    return [{"id": key, "label": label, "description": "Reviewed provider classifications in this group."}
            for key, (label, _) in GROUP_MEMBERS[service][dimension].items()]

def group_for(service, dimension, entry_id):
    for group, (_, labels) in GROUP_MEMBERS[service][dimension].items():
        if entry_id in {identifier(label) for label in labels}:
            return group
    return None

def grouped_policy(service, policy):
    """Pure, repeatable migration: retain recognition snapshot and prior coverage."""
    result = copy.deepcopy(policy)
    if result.get("schema_version") == 2:
        return result
    snap = result["catalogue"]
    snap["groups"] = {dim: group_definitions(service, dim) for dim in snap["dimensions"]}
    for dim, entries in snap["dimensions"].items():
        old = result["dimensions"][dim]
        groups = {g["id"]: {"mode": old["mode"], "selected": []} for g in snap["groups"][dim]}
        for entry in entries:
            if entry["id"] in (UNKNOWN, MISSING):
                entry["group"] = "special"
                continue
            group = group_for(service, dim, entry["id"])
            if group is None:
                # Pin explicit historical catalogue families for entries no longer in code.
                label = entry.get("family") or "Historical catalogue"
                group = "historical-" + identifier(label)
                if group not in groups:
                    snap["groups"][dim].append({"id": group, "label": label, "description": "Saved historical catalogue group."})
                    groups[group] = {"mode": old["mode"], "selected": []}
            entry["group"] = group
            if entry["id"] in old["selected"]:
                groups[group]["selected"].append(entry["id"])
        result["dimensions"][dim] = {"groups": groups, "special": [UNKNOWN, MISSING] if old["mode"] == "all" else [x for x in old["selected"] if x in (UNKNOWN, MISSING)]}
    result["schema_version"] = 2
    return result

def adopt_catalogue(service, policy):
    old = grouped_policy(service, policy)
    result = {"schema_version": 2, "catalogue": catalogue(service), "dimensions": {}}
    for dim, definitions in result["catalogue"]["groups"].items():
        result["dimensions"][dim] = {"special": old["dimensions"][dim]["special"][:], "groups": {}}
        for group in definitions:
            previous = old["dimensions"][dim]["groups"].get(group["id"], {"mode": "selected", "selected": []})
            members = {e["id"] for e in result["catalogue"]["dimensions"][dim] if e["group"] == group["id"]}
            # Preserve individual IDs through moves, without selecting new entries.
            selected = [x for g in old["dimensions"][dim]["groups"].values() for x in g["selected"] if x in members]
            result["dimensions"][dim]["groups"][group["id"]] = {"mode": previous["mode"], "selected": sorted(set(selected))}
    return result

def canonical(value):
    text = re.sub(r"\s*[-‐‑‒–—−]\s*", " - ", str(value or ""))
    return re.sub(r"\s+", " ", text).strip().casefold()

def identifier(label):
    return re.sub(r"[^a-z0-9]+", "-", label.casefold()).strip("-")

def catalogue(service):
    return {"groups": {dim: group_definitions(service, dim) for dim in LABELS[service]}, "version": VERSION, "reviewed": REVIEWED, "source": SOURCES[service],
            "dimensions": {dim: [{"id": identifier(label), "label": label, "aliases": ["Detailed Severe Thunderstorm Warning", "Severe Thunderstorm Warning - Canberra"] if label == "Severe Thunderstorm Warning" else [],
              "group": group_for(service, dim, identifier(label)),
              "family": next(g["label"] for g in group_definitions(service, dim) if g["id"] == group_for(service, dim, identifier(label))),
              "description": "Provider classification; does not imply severity.", "source": SOURCES[service], "reviewed": REVIEWED,
              "patterns": {"detailed": True, "qualifiers": ["Canberra"]} if label == "Severe Thunderstorm Warning" else
                          {"prefix": True} if label in ("Flood Warning", "Flood Watch") else
                          {"for_suffix": True} if label in ("Tropical Cyclone Warning", "Tropical Cyclone Advice") else {}} for label in labels]
              + [{"id": UNKNOWN, "label": "Unrecognized", "aliases": [], "family": "Special", "group": "special", "description": "Nonempty provider value outside this catalogue."},
                 {"id": MISSING, "label": "Not supplied", "aliases": [], "family": "Special", "group": "special", "description": "Provider did not supply a classification."}]
             for dim, labels in LABELS[service].items()}}

def classify(service, dimension, raw, snapshot=None):
    snap = snapshot or catalogue(service)
    text = str(raw or "")
    value = canonical(text)
    result = {"raw": text, "id": MISSING if not value else UNKNOWN,
              "label": "Not supplied" if not value else "Unrecognized", "qualifier": "", "subtype": "", "group": "special"}
    for entry in sorted(snap["dimensions"][dimension], key=lambda e: len(e["label"]), reverse=True):
        if entry["id"] in (UNKNOWN, MISSING):
            continue
        names = [canonical(entry["label"]), *(canonical(a) for a in entry.get("aliases", []))]
        match = value in names
        qualifier = subtype = ""
        if service == "bom":
            base = canonical(entry["label"])
            patterns = entry.get("patterns", {})
            if patterns.get("detailed"):
                variant = value
                if variant.startswith("detailed "):
                    subtype = "Detailed"
                    variant = variant[len("detailed "):]
                if variant == base or variant in {base + " - " + canonical(q) for q in patterns.get("qualifiers", [])}:
                    match = True
                    if variant.startswith(base + " - "):
                        qualifier = re.split(r"\s*[-‐‑‒–—−]\s*", text, maxsplit=1)[-1].strip()
            elif patterns.get("prefix") and value.endswith(" " + base):
                match = True
                qualifier = text[:-len(entry["label"])].strip()
            elif patterns.get("for_suffix") and value.startswith(base + " for "):
                match = True
                qualifier = text[len(entry["label"]):].strip()
        if match:
            return result | {"id": entry["id"], "label": entry["label"], "qualifier": qualifier, "subtype": subtype, "group": entry.get("group")}
    return result

@dataclass(frozen=True)
class Selection:
    included: bool
    reason: str
    dimensions: dict
    catalogue: dict
    mode: str
    schema_version: int = 1
    def metadata(self):
        return asdict(self)

def proposal(service, settings):
    snap = catalogue(service)
    dimensions = {d: {"mode": "all", "selected": []} for d in snap["dimensions"]}
    if service == "rfs":
        dimensions["level"] = {"mode": "selected", "selected": [classify(service, "level", x, snap)["id"] for x in settings.get("rfs_levels", [])]}
    elif service == "bom":
        from .filters import FilterRules, should_include
        rules = FilterRules(list(settings.get("filter_include_exact", [])), list(settings.get("filter_include_suffix", [])), list(settings.get("filter_exclude_exact", [])))
        dimensions["type"] = {"mode": "selected", "selected": [e["id"] for e in snap["dimensions"]["type"] if e["id"] not in (UNKNOWN, MISSING) and should_include(e["label"], rules)]}
    return {"catalogue": snap, "dimensions": dimensions}

def validate(service, policy):
    if not isinstance(policy, dict) or not isinstance(policy.get("catalogue"), dict):
        raise ValueError("Invalid notice selection policy")
    snap = policy["catalogue"]
    if set(snap.get("dimensions", {})) != set(LABELS[service]) or set(policy.get("dimensions", {})) != set(LABELS[service]):
        raise ValueError("Invalid selection dimensions")
    for dim, selection in policy["dimensions"].items():
        ids = {e["id"] for e in snap["dimensions"][dim]}
        if len(ids) != len(snap["dimensions"][dim]) or not {UNKNOWN, MISSING} <= ids:
            raise ValueError("Invalid or duplicate catalogue IDs")
        if policy.get("schema_version") == 2:
            definitions = snap.get("groups", {}).get(dim, [])
            group_ids = {g["id"] for g in definitions}
            if len(group_ids) != len(definitions) or set(selection.get("groups", {})) != group_ids:
                raise ValueError("Invalid classification groups")
            if not isinstance(selection.get("special"), list) or not set(selection["special"]) <= {UNKNOWN, MISSING}:
                raise ValueError("Invalid Special choices")
            for entry in snap["dimensions"][dim]:
                if entry["id"] in (UNKNOWN, MISSING) and entry.get("group") != "special":
                    raise ValueError("Invalid Special group membership")
                if entry["id"] not in (UNKNOWN, MISSING) and entry.get("group") not in group_ids:
                    raise ValueError("Classification has invalid group membership")
            for group_id, choice in selection["groups"].items():
                members = {e["id"] for e in snap["dimensions"][dim] if e.get("group") == group_id and e["id"] not in (UNKNOWN, MISSING)}
                if choice.get("mode") not in ("all", "selected") or not isinstance(choice.get("selected"), list) or not set(choice["selected"]) <= members:
                    raise ValueError("Invalid group mode or classification membership")
        elif selection.get("mode") not in ("all", "selected") or not isinstance(selection.get("selected"), list) or not set(selection["selected"]) <= ids:
            raise ValueError("Invalid notice selection")
    return copy.deepcopy(policy)

def evaluate(service, values, settings):
    policy = settings.get(service + "_notice_selection")
    snap = policy["catalogue"] if policy else catalogue(service)
    classified = {dim: classify(service, dim, values.get(dim), snap) for dim in LABELS[service]}
    reasons = []
    if policy:
        for dim, item in classified.items():
            choice = policy["dimensions"][dim]
            if policy.get("schema_version") == 2:
                if item["id"] in (UNKNOWN, MISSING):
                    included = item["id"] in choice["special"]
                    item["group_mode"] = "selected"
                    group_label = "Special"
                else:
                    group_id = item.get("group")
                    group_choice = choice.get("groups", {}).get(group_id)
                    definitions = {g["id"]: g["label"] for g in snap.get("groups", {}).get(dim, [])}
                    if group_choice is None or group_id not in definitions:
                        reasons.append(f"{dim}: {item['label']} has invalid group membership")
                        continue
                    if group_choice.get("mode") not in ("all", "selected"):
                        reasons.append(f"{dim}: {item['label']} has invalid group mode")
                        continue
                    item["group_mode"] = group_choice["mode"]
                    group_label = definitions[group_id]
                    included = group_choice["mode"] == "all" or item["id"] in group_choice["selected"]
                item["group_label"] = group_label
                if not included:
                    reasons.append(f"{dim}: {item['label']} is not selected in {group_label}" + (f" ({item['raw']})" if item["id"] == UNKNOWN else ""))
            elif choice["mode"] == "selected" and item["id"] not in choice["selected"]:
                reasons.append(f"{dim}: {item['label']} is not selected" + (f" ({item['raw']})" if item["id"] == UNKNOWN else ""))
    elif service == "rfs":
        if values.get("level", "") not in settings.get("rfs_levels", []):
            reasons.append("alert level")
    elif service == "bom":
        from .filters import FilterRules, should_include
        rules = FilterRules(list(settings.get("filter_include_exact", [])), list(settings.get("filter_include_suffix", [])), list(settings.get("filter_exclude_exact", [])))
        if not should_include(values.get("type", ""), rules):
            reasons.append("legacy BOM product rules")
    if service == "traffic":
        feeds = settings.get("traffic_types", [])
        if values.get("feed") not in ("incident", "roadwork", "fire", "flood", "regional"):
            reasons.insert(0, "invalid provider feed")
        elif values.get("feed") not in feeds or ("ROADWORK" in str(values.get("category", "")).upper() and "roadwork" not in feeds):
            reasons.insert(0, "hazard type")
    return Selection(not reasons, "; ".join(reasons) or "Selected notice classifications", classified, snap, "catalogue" if policy else "legacy", policy.get("schema_version", 1) if policy else 1)

def values_for(service, item):
    if service == "bom":
        return {"type": item.event}
    if service == "rfs":
        return {"level": item.level, "kind": item.kind}
    return {"feed": item.feed, "category": getattr(item, "raw_category", None) if getattr(item, "raw_category", None) is not None else item.category}


def saved_values(db, service, current_only=False):
    """Retained notices, not history revisions. No writes, fetches, or radio work."""
    import json
    table = {"bom": "bom_current", "rfs": "rfs_incidents", "traffic": "traffic_items"}[service]
    with db._lock:
        rows = db._conn.execute("SELECT * FROM " + table).fetchall()
    result = []
    for record in rows:
        row = dict(record)
        if current_only and service != "bom" and row.get("missing_polls", 0) >= 2:
            continue
        data = json.loads(row.get("raw_data" if service == "bom" else "normalized_data") or "{}")
        if service == "bom":
            values = {"type": row.get("event", "")}
        elif service == "rfs":
            values = {"level": row.get("level", ""), "kind": data.get("kind", row.get("kind", ""))}
        else:
            values = {"feed": row.get("feed", ""), "category": data.get("raw_category") if data.get("raw_category") is not None else row.get("category", "")}
        result.append({"id": row.get("alert_id", row.get("incident_id", row.get("item_id"))),
                       "title": row.get("headline", row.get("name", row.get("title", ""))), "values": values,
                       "current": service == "bom" or row.get("missing_polls", 0) < 2,
                       "row": row, "data": data})
    return result

def inventory(db, service, snapshot=None):
    """Count distinct provider IDs per value across retained notices and recorded history."""
    import json
    snap = snapshot or catalogue(service)
    counts = {dim: {e["id"]: {"observed": set(), "current": set(), "examples": set()} for e in entries}
              for dim, entries in snap["dimensions"].items()}
    def add(identity, values, current=False):
        for dim in counts:
            item = classify(service, dim, values.get(dim), snap)
            bucket = counts[dim][item["id"]]
            bucket["observed"].add(identity)
            if current:
                bucket["current"].add(identity)
            if item["raw"]:
                bucket["examples"].add(item["raw"])
    for item in saved_values(db, service):
        add(item["id"], item["values"], item["current"])
    with db._lock:
        history = db._conn.execute("SELECT external_id,title,metadata FROM service_history WHERE source=?", (service,)).fetchall()
    for row in history:
        meta = json.loads(row["metadata"] or "{}")
        # Absence of old historical metadata is not evidence of a missing provider value.
        values = {"type": row["title"]} if service == "bom" else {d: meta[d] for d in counts if d in meta}
        recorded = meta.get("notice_selection", {}).get("dimensions", {})
        for dim in counts:
            if dim in recorded and "raw" in recorded[dim]:
                values[dim] = recorded[dim]["raw"]
        for dim in counts:
            if dim not in values:
                continue
            item = classify(service, dim, values[dim], snap)
            bucket = counts[dim][item["id"]]
            bucket["observed"].add(row["external_id"])
            if item["raw"]:
                bucket["examples"].add(item["raw"])
    return {dim: {key: {"observed": len(v["observed"]), "current": len(v["current"]),
                        "examples": sorted(v["examples"])} for key, v in buckets.items()} for dim, buckets in counts.items()}


def current_evidence(service, values, settings, coverage, active=True):
    selection = evaluate(service, values, settings)
    reasons = []
    closure = bool(coverage and coverage.method == "previous transmission")
    if not settings.get(service + "_enabled", service == "bom"):
        reasons.append("Service disabled")
    if not active and not closure:
        reasons.append("Notice is expired, future or absent")
    if not coverage or not coverage.included:
        reasons.append(coverage.reason if coverage else "Geographic evidence unavailable")
    if not selection.included and not closure:
        reasons.append(selection.reason)
    return {"included": not reasons, "reason": "; ".join(reasons) or
            ("Previously transmitted closure" if closure else "Classification and geographic coverage match; delivery deduplication still applies"),
            "selection": selection}


def saved_values_for(service, item, row):
    import json
    values = values_for(service, item)
    data = json.loads(row["normalized_data"] or "{}")
    if service == "traffic" and data.get("raw_category") is not None:
        values["category"] = data["raw_category"]
    return values

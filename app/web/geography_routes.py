"""Read-only geographic previews and explicit universal-policy activation."""
from dataclasses import fields
import json
from types import SimpleNamespace

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..geography import KEY, active, proposal, configuration, incident_delivery_coverage, traffic_delivery_coverage, bom_delivery_coverage, Coverage, needs_term_merge
from ..bom_area import CouncilMatch
from ..bom_enricher import WarningSection
from ..models import Alert
from ..rfs.feed import Incident
from ..rfs.councils import COUNCILS
from ..traffic.feed import TrafficItem
from ..traffic.schedule import ClosurePeriod
from .routes import render

router = APIRouter()


class ListingEvidence:
    """Batch delivery evidence for a page, rather than querying for every row."""
    def __init__(self, db, listing_rows):
        self.broadcasts, self.states = {}, {}
        with db._lock:
            for source, rows in listing_rows.items():
                identity = {"bom": "alert_id", "rfs": "incident_id", "traffic": "item_id"}[source]
                ids = {row[identity] for row in rows}
                if ids:
                    placeholders = ",".join("?" for _ in ids)
                    saved = db._conn.execute(f"SELECT * FROM service_history WHERE source=? AND external_id IN ({placeholders}) "
                                             "AND transmit_status IN ('success','repeat_confirmed','unconfirmed') ORDER BY id DESC",
                                             (source, *ids)).fetchall()
                    for row in saved:
                        self.broadcasts.setdefault((source, row["external_id"]), row)
                if source == "bom":
                    for row in rows:
                        ids.update(json.loads(row.get("raw_data") or "{}").get("references", []))
                    if ids:
                        placeholders = ",".join("?" for _ in ids)
                        self.states = {row["alert_id"]: row for row in db._conn.execute(
                            f"SELECT * FROM alert_state WHERE alert_id IN ({placeholders})", tuple(ids))}

    def latest_successful_broadcast(self, source, identity):
        return self.broadcasts.get((source, identity))

    def get_state(self, identity):
        return self.states.get(identity)


def saved_decisions(db, settings, sources=("bom", "rfs", "traffic"), *, listing_rows=None):
    result = []
    coverage_db = ListingEvidence(db, listing_rows) if listing_rows is not None else db
    bom_rows = (listing_rows.get("bom", []) if listing_rows is not None else db.bom_current_items(["NSW"], limit=100000)) if "bom" in sources else []
    for row in bom_rows:
        item = json.loads(row["raw_data"] or "{}")
        alert = Alert.from_bom(item)
        alert.specific_locations = row["specific_locations"]
        alert.warning_summary = row["warning_summary"]
        alert.warning_sections = tuple(WarningSection(**s) for s in json.loads(row["warning_sections"] or "[]"))
        match = CouncilMatch(row["council_match"], tuple(json.loads(row["matched_councils"] or "[]")))
        coverage = bom_delivery_coverage(alert, match, settings, coverage_db, SimpleNamespace(**item.get("_enrichment", {})))
        result.append(dict(source="bom", id=row["alert_id"], title=row["headline"], coverage=coverage))
    for service, table, cls, identity in (("rfs", "rfs_incidents", Incident, "incident_id"),
                                          ("traffic", "traffic_items", TrafficItem, "item_id")):
        if service not in sources:
            continue
        if listing_rows is not None:
            rows = listing_rows.get(service, [])
        else:
            with db._lock:
                rows = db._conn.execute(f"SELECT * FROM {table} WHERE missing_polls<2").fetchall()
        for row in rows:
            data = json.loads(row["normalized_data"] or "{}")
            data = {f.name: data[f.name] for f in fields(cls) if f.name in data}
            if service == "traffic":
                data["periods"] = tuple(ClosurePeriod(**p) for p in data.get("periods", []))
            try:
                item = cls(**data)
            except TypeError:
                result.append(dict(source=service, id=row[identity], title=row["name" if service == "rfs" else "title"],
                                   coverage=Coverage(False, reason="Unavailable: saved geographic evidence is incomplete")))
                continue
            coverage = incident_delivery_coverage(item, settings, coverage_db) if service == "rfs" else traffic_delivery_coverage(item, row["council"], settings, coverage_db)
            result.append(dict(source=service, id=row[identity], title=item.name if service == "rfs" else item.title,
                               coverage=coverage))
    return result


def page(request, policy=None, error="", preview=False):
    db = request.app.state.db
    settings = db.all_settings()
    pending_merge = needs_term_merge(settings)
    policy = policy or proposal(settings)
    comparisons = []
    summaries = []
    if preview or pending_merge or not active(settings):
        before = {(x["source"], x["id"]): x for x in saved_decisions(db, settings)}
        for item in saved_decisions(db, settings | {KEY: policy}):
            old = before[(item["source"], item["id"])]["coverage"]
            comparisons.append(item | {"previous": old, "changed": old.included != item["coverage"].included})
        for service in ("bom", "rfs", "traffic"):
            values = [x for x in comparisons if x["source"] == service]
            summaries.append(dict(service=service, enabled=settings.get(f"{service}_enabled", service == "bom"),
                                  notices=len(values), added=sum(not x["previous"].included and x["coverage"].included for x in values),
                                  removed=sum(x["previous"].included and not x["coverage"].included for x in values)))
    legacy = [dict(service=s, enabled=settings.get(f"{s}_enabled", s == "bom"),
                   all_nsw=settings.get(f"{s}_all_councils", s == "bom"), councils=settings.get(f"{s}_councils", []),
                   uncertain=settings.get("bom_include_unknown_councils", True) if s == "bom" else False)
              for s in ("bom", "rfs", "traffic")]
    response = render(request, "settings_geography.html", policy=policy, councils=COUNCILS,
                      active=active(settings), pending_merge=pending_merge, error=error, comparisons=comparisons, summaries=summaries, legacy=legacy,
                      preview=preview, saved=request.query_params.get("saved"))
    if error:
        response.status_code = 422
    return response


@router.get("/settings/geography", response_class=HTMLResponse)
async def geographic_settings(request: Request):
    return page(request)


@router.post("/settings/geography", response_class=HTMLResponse)
async def save_geographic_settings(request: Request):
    form = await request.form()
    try:
        policy = configuration(bool(form.get("all_nsw")), form.getlist("councils"),
                               form.get("location_terms", ""), form.get("bom_districts", ""),
                               bool(form.get("include_uncertain")), form.get("location_exclusions", ""))
    except ValueError as exc:
        submitted = dict(version=2, active=True, all_nsw=bool(form.get("all_nsw")), councils=form.getlist("councils"),
                         location_terms=str(form.get("location_terms", "")).splitlines() + str(form.get("bom_districts", "")).splitlines(),
                         location_exclusions=str(form.get("location_exclusions", "")).splitlines(),
                         include_uncertain=bool(form.get("include_uncertain")))
        return page(request, submitted, error=str(exc))
    action = form.get("action")
    if action == "preview":
        return page(request, policy, preview=True)
    if action != "save":
        return page(request, policy, error="Choose Preview coverage or Save and activate")
    db = request.app.state.db
    # A single setting write makes activation atomic; archive old settings only once.
    if not active(db.all_settings()):
        db.set_setting("geographic_legacy_settings", {k: v for k, v in db.all_settings().items()
                       if k in {f"{s}_{suffix}" for s in ("bom", "rfs", "traffic") for suffix in ("councils", "all_councils")} |
                       {"bom_districts", "bom_include_unknown_councils"}})
    if needs_term_merge(db.all_settings()) and not db.get_setting("geographic_policy_before_term_merge", None):
        db.set_setting("geographic_policy_before_term_merge", db.get_setting(KEY))
    db.set_setting(KEY, policy)
    db.add_event("INFO", "Universal geographic coverage saved and activated; confirmed notices are not automatically resent")
    for name in ("poller", "rfs_poller", "traffic_poller"):
        poller = getattr(request.app.state, name, None)
        if poller:
            poller.poke()
    return RedirectResponse("/settings/geography?saved=1", status_code=303)

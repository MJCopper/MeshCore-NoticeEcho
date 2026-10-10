"""Notice selection settings: preview is read-only; adoption is explicit."""
import copy
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from ..notice_selection import catalogue, proposal, validate, evaluate, saved_values, inventory, LABELS, current_evidence, grouped_policy, adopt_catalogue
from .routes import render
from .geography_routes import saved_decisions

router = APIRouter()

def page(request, service, policy=None, preview=False, error=""):
    db = request.app.state.db
    settings = db.all_settings()
    policy = policy or settings.get(service + "_notice_selection") or proposal(service, settings)
    policy = grouped_policy(service, policy)
    comparisons = []
    geo = {(x["source"], x["id"]): x["coverage"] for x in saved_decisions(db, settings, (service,))}
    for item in saved_values(db, service, current_only=True):
        before = evaluate(service, item["values"], settings)
        after = evaluate(service, item["values"], settings | {service + "_notice_selection": policy})
        coverage = geo.get((service, item["id"]))
        active = True
        if service == "traffic":
            import time
            data = item["data"]
            active = bool(item["row"].get("active") and not data.get("ended") and
                          (data.get("start") is None or data["start"] <= time.time()) and
                          (data.get("end") is None or data["end"] > time.time()))
        if service == "bom":
            from datetime import datetime, timezone
            try:
                expiry = datetime.fromisoformat(item["row"].get("expires") or "")
                active = not (expiry.tzinfo and expiry <= datetime.now(timezone.utc))
            except ValueError:
                pass
        old_overall = current_evidence(service, item["values"], settings, coverage, active)
        new_overall = current_evidence(service, item["values"], settings | {service + "_notice_selection": policy}, coverage, active)
        blockers = [] if new_overall["included"] else [new_overall["reason"]]
        closure = bool(coverage and coverage.method == "previous transmission")
        old_ok, new_ok = old_overall["included"], new_overall["included"]
        comparisons.append(item | {"before": before, "after": after, "old_ok": old_ok, "new_ok": new_ok,
                           "changed": old_ok != new_ok or before.included != after.included or before.dimensions != after.dimensions, "blockers": blockers, "closure": closure})
    catalogue_changes = []
    if preview:
        previous = grouped_policy(service, settings.get(service + "_notice_selection") or proposal(service, settings))
        for dim, entries in policy["catalogue"]["dimensions"].items():
            old_entries = {e["id"]: e for e in previous["catalogue"]["dimensions"][dim]}
            old_groups = {g["id"] for g in previous["catalogue"]["groups"][dim]}
            for group in policy["catalogue"]["groups"][dim]:
                if group["id"] not in old_groups:
                    catalogue_changes.append(f"{dim}: new group {group['label']} starts unselected")
            for entry in entries:
                old = old_entries.get(entry["id"])
                if old is None:
                    catalogue_changes.append(f"{dim}: new classification {entry['label']} in {entry.get('group')}")
                elif old.get("group") != entry.get("group"):
                    catalogue_changes.append(f"{dim}: {entry['label']} moves from {old.get('group')} to {entry.get('group')}")
            for entry_id in old_entries.keys() - {e["id"] for e in entries}:
                catalogue_changes.append(f"{dim}: {old_entries[entry_id]['label']} removed from catalogue")
    return render(request, f"settings_{service}.html", min_interval=5,
                  types=("incident", "roadwork", "fire", "flood", "regional"),
                  selected_types=set(settings.get("traffic_types", [])), service=service, policy=policy,
                  active_policy=settings.get(service + "_notice_selection"), latest=catalogue(service),
                  observations=inventory(db, service, policy["catalogue"]), comparisons=comparisons,
                  catalogue_changes=catalogue_changes, preview=preview, error=error, saved=request.query_params.get("saved"),
                  s=settings)

@router.get("/settings/notices/{service}", response_class=HTMLResponse)
async def settings_notices(request: Request, service: str):
    if service not in LABELS:
        raise HTTPException(404)
    return RedirectResponse(f"/settings/{service}", status_code=303)

@router.post("/settings/notices/{service}", response_class=HTMLResponse)
async def save_notices(request: Request, service: str):
    if service not in LABELS:
        raise HTTPException(404)
    db = request.app.state.db
    form = await request.form()
    settings = db.all_settings()
    policy = grouped_policy(service, settings.get(service + "_notice_selection") or proposal(service, settings))
    previous_groups = {dim: set(choice["groups"]) for dim, choice in policy["dimensions"].items()}
    if form.get("catalogue") == "latest":
        policy = adopt_catalogue(service, policy)
    try:
        if form.get("catalogue", "active") not in ("active", "latest"):
            raise ValueError("Choose active or latest catalogue")
        if form.get("action") not in ("preview", "save"):
            raise ValueError("Choose preview or save")
        legacy_form = any(dim + suffix in form for dim in policy["dimensions"] for suffix in ("_mode", "_selected"))
        if legacy_form:
            # Compatibility for previously bookmarked/open dimension-wide forms.
            allowed = {"action", "catalogue"} | {dim + suffix for dim in policy["dimensions"] for suffix in ("_mode", "_selected", "_choices_submitted")}
            if set(form.keys()) - allowed:
                raise ValueError("Do not mix group and dimension-wide controls")
            old = {"catalogue": policy["catalogue"], "dimensions": {}}
            for dim, choice in policy["dimensions"].items():
                mode = str(form.get(dim + "_mode", "selected"))
                selected = list(form.getlist(dim + "_selected"))
                if mode == "all" and not selected and form.get(dim + "_choices_submitted") != "1":
                    selected = [x for g in choice["groups"].values() for x in g["selected"]] + choice["special"]
                old["dimensions"][dim] = {"mode": mode, "selected": selected}
            validate(service, old)
            policy = grouped_policy(service, old)
        else:
            allowed = {"action", "catalogue"}
            for dim, choice in policy["dimensions"].items():
                allowed.update({dim + "_special", dim + "_special_submitted"})
                if form.get(dim + "_special_submitted") != "1":
                    raise ValueError("Submit Special choices for each classification dimension")
                choice["special"] = list(form.getlist(dim + "_special"))
                for group_id, group in choice["groups"].items():
                    prefix = dim + "_" + group_id
                    allowed.update({prefix + "_mode", prefix + "_selected", prefix + "_choices_submitted"})
                    if prefix + "_mode" not in form and group_id in previous_groups[dim]:
                        raise ValueError("Choose a mode for each classification group")
                    mode = str(form.get(prefix + "_mode", "selected"))
                    selected = list(form.getlist(prefix + "_selected"))
                    if mode == "all" and not selected and form.get(prefix + "_choices_submitted") != "1":
                        selected = group["selected"]
                    choice["groups"][group_id] = {"mode": mode, "selected": selected}
            if set(form.keys()) - allowed:
                raise ValueError("Unknown classification group or setting")
        validate(service, policy)
    except (ValueError, TypeError, KeyError) as exc:
        response = page(request, service, error=str(exc))
        response.status_code = 422
        return response
    if form["action"] == "preview":
        return page(request, service, policy, preview=True)
    db.set_setting(service + "_notice_selection", policy)
    db.add_event("INFO", f"{service.upper()} notice selection saved; completed notices are not automatically resent")
    poller = getattr(request.app.state, {"bom": "poller", "rfs": "rfs_poller", "traffic": "traffic_poller"}[service], None)
    if poller:
        poller.poke()
    return RedirectResponse(f"/settings/{service}?saved=1", status_code=303)


@router.get("/troubleshoot/notice-selection")
async def selection_diagnostics(request: Request):
    db = request.app.state.db
    settings = db.all_settings()
    return {service: {"selection": grouped_policy(service, settings.get(service + "_notice_selection") or proposal(service, settings)),
                      "applied": bool(settings.get(service + "_notice_selection")), "mode": "catalogue" if settings.get(service + "_notice_selection") else "legacy",
                      "inventory": inventory(db, service, (settings.get(service + "_notice_selection") or {}).get("catalogue")),
                      "settings_url": f"/settings/{service}"} for service in LABELS}

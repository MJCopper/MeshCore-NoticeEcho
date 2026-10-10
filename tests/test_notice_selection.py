"""Classification, migration, explicit snapshots and actual processing paths."""
from copy import deepcopy
from dataclasses import replace
import json
import time

import pytest
from fastapi.testclient import TestClient

from app.notice_selection import (LABELS, UNKNOWN, MISSING, catalogue, classify, identifier,
                                 proposal, evaluate, values_for, inventory, validate, current_evidence)
from app.filters import FilterRules, should_include
from app.db import Database
from app.geography import Coverage
from app.web.notice_routes import router
from test_troubleshooting import environment, seed, rows

CASES = [(s, d, label) for s, dimensions in LABELS.items() for d, labels in dimensions.items() for label in labels]

@pytest.mark.parametrize("service,dimension,label", CASES)
def test_every_catalogued_provider_value_has_a_stable_classification(service, dimension, label):
    for raw in (label, label.lower(), "  " + label.upper().replace(" ", "  ") + "  "):
        item = classify(service, dimension, raw)
        assert item["id"] == identifier(label)
        assert item["raw"] == raw

@pytest.mark.parametrize("service,dimension", [(s,d) for s,v in LABELS.items() for d in v])
@pytest.mark.parametrize("raw,expected", [(None,MISSING),("",MISSING),("  ",MISSING),("New Unknown Warning",UNKNOWN)])
def test_special_values_are_separate_in_every_dimension(service, dimension, raw, expected):
    assert classify(service, dimension, raw)["id"] == expected

@pytest.mark.parametrize("raw,qualifier,subtype", [
    ("Severe Thunderstorm Warning - Canberra", "Canberra", ""),
    ("Severe Thunderstorm Warning – Canberra", "Canberra", ""),
    ("Severe Thunderstorm Warning−Canberra", "Canberra", ""),
    ("Detailed Severe Thunderstorm Warning", "", "Detailed"),
    ("Detailed Severe Thunderstorm Warning — Canberra", "Canberra", "Detailed")])
def test_thunderstorm_family_retains_raw_qualifier_and_subtype(raw, qualifier, subtype):
    item = classify("bom", "type", raw)
    assert item == {"id":"severe-thunderstorm-warning","label":"Severe Thunderstorm Warning",
                    "raw":raw,"qualifier":qualifier,"subtype":subtype}

@pytest.mark.parametrize("raw", ["Severe Weather Warning - Damaging Winds", "Unknown Warning", "Some - Hazard Warning", "Severe Thunderstorm Warning - Damaging Winds"])
def test_no_blind_dash_or_warning_suffix_classification(raw):
    assert classify("bom", "type", raw)["id"] == UNKNOWN

def policy(service, settings=None):
    return proposal(service, settings or {})

@pytest.mark.parametrize("service,dimension", [(s,d) for s,v in LABELS.items() for d in v])
def test_all_includes_future_and_missing_selected_does_not(service, dimension):
    p = policy(service)
    for d in p["dimensions"]:
        p["dimensions"][d] = {"mode":"all","selected":[]}
    settings = {service + "_notice_selection":p, "traffic_types":["incident"]}
    values = {dimension:"Future provider value", "feed":"incident"}
    assert evaluate(service, values, settings).included
    p["dimensions"][dimension] = {"mode":"selected","selected":[UNKNOWN]}
    assert evaluate(service, values, settings).included
    values[dimension] = ""
    assert not evaluate(service, values, settings).included
    p["dimensions"][dimension]["selected"] = [MISSING]
    assert evaluate(service, values, settings).included

@pytest.mark.parametrize("level,kind,expected", [("Advice","Grass Fire",True),("Advice","Medical",False),("Not Applicable","Grass Fire",False),("","Grass Fire",False)])
def test_rfs_level_and_type_must_both_match(level, kind, expected):
    p=policy("rfs",{"rfs_levels":["Advice"]})
    p["dimensions"]["kind"]={"mode":"selected","selected":["grass-fire"]}
    assert evaluate("rfs",{"level":level,"kind":kind},{"rfs_notice_selection":p}).included == expected

@pytest.mark.parametrize("feed,category,expected", [("incident","CRASH",True),("fire","CRASH",False),("incident","BREAKDOWN",False),("regional","SCHEDULED ROADWORK",False),("invalid","CRASH",False)])
def test_traffic_feeds_and_category_and_roadwork_gate(feed,category,expected):
    p=policy("traffic")
    p["dimensions"]["category"]={"mode":"selected","selected":["crash","scheduled-roadwork"]}
    result=evaluate("traffic",{"feed":feed,"category":category},{"traffic_notice_selection":p,"traffic_types":["incident","regional"]})
    assert result.included == expected

def test_old_catalogue_unknown_selection_does_not_silently_reclassify():
    p=policy("traffic")
    p["dimensions"]["category"]={"mode":"selected","selected":[UNKNOWN]}
    settings={"traffic_notice_selection":p,"traffic_types":["incident"]}
    values={"feed":"incident","category":"NEW CATEGORY"}
    assert evaluate("traffic",values,settings).included
    newer=deepcopy(p)
    newer["catalogue"]["version"]=2
    newer["catalogue"]["dimensions"]["category"].append({"id":"new-category","label":"NEW CATEGORY","aliases":[]})
    assert not evaluate("traffic",values,settings|{"traffic_notice_selection":newer}).included
    assert evaluate("traffic",values,settings).included


def test_legacy_rules_preserve_exclusion_precedence_and_sheep_graziers():
    s={"filter_include_suffix":["Warning"],"filter_include_exact":["Road Weather Alert"],"filter_exclude_exact":["Severe Thunderstorm Warning"]}
    assert not evaluate("bom",{"type":"Severe Thunderstorm Warning"},s).included
    assert evaluate("bom",{"type":"Warning to Sheep Graziers"},s).included
    assert not evaluate("bom",{"type":"Severe Thunderstorm Warning - Canberra"},s).included
    p=proposal("bom",s)
    assert not evaluate("bom",{"type":"Severe Thunderstorm Warning - Canberra"},s|{"bom_notice_selection":p}).included
    assert "warning-to-sheep-graziers" in p["dimensions"]["type"]["selected"]


def test_migration_does_not_select_new_rfs_levels_and_new_dimensions_are_all():
    p=proposal("rfs",{"rfs_levels":["Advice"]})
    assert p["dimensions"]["level"]["selected"]==["advice"]
    assert p["dimensions"]["kind"]["mode"]=="all"
    assert proposal("traffic",{})["dimensions"]["category"]["mode"]=="all"


def test_validation_rejects_unknown_ids_modes_and_dimensions():
    for mutate in (lambda p:p["dimensions"]["category"].update(mode="bad"),
                   lambda p:p["dimensions"]["category"].update(selected=["invented"]),
                   lambda p:p["dimensions"].update(invented={})): 
        p=policy("traffic"); mutate(p)
        with pytest.raises(ValueError): validate("traffic",p)


def test_overall_eligibility_separates_geographic_match_from_rfs_level():
    result=current_evidence("rfs",{"level":"Not Applicable","kind":"Grass Fire"},
                            {"rfs_enabled":True,"rfs_levels":["Advice"]},Coverage(True,reason="Included: Tamworth"))
    assert not result["included"] and "alert level" in result["reason"]

@pytest.mark.parametrize("source",["bom","rfs","traffic"])
def test_settings_preview_is_read_only_and_explicit_save_pins_snapshot(environment,source):
    app=environment; app.include_router(router); seed(app,source)
    from app.web.routes import router as web_router
    from app.rfs.web import router as rfs_router
    from app.traffic.web import router as traffic_router
    for service_router in (web_router,rfs_router,traffic_router): app.include_router(service_router)
    db=app.state.db; before=db.all_settings(); history=rows(db,"SELECT * FROM service_history")
    client=TestClient(app)
    page=client.get("/settings/notices/"+source)
    assert page.status_code==200
    assert "Unrecognized" in page.text and "Not supplied" in page.text
    p=proposal(source,before)
    form={"action":"preview","catalogue":"latest"}
    for dim,choice in p["dimensions"].items():
        form[dim+"_mode"]="all"
    response=client.post("/settings/notices/"+source,data=form)
    assert response.status_code==200 and "Preview of saved current notices" in response.text
    assert db.all_settings()==before and rows(db,"SELECT * FROM service_history")==history and not app.state.tx.sent
    form["action"]="save"
    assert client.post("/settings/notices/"+source,data=form,follow_redirects=False).status_code==303
    assert db.get_setting(source+"_notice_selection")["catalogue"]["version"]==1
    assert not app.state.tx.sent
    assert client.get("/troubleshoot/notice-selection").status_code==200

@pytest.mark.asyncio
@pytest.mark.parametrize("source",["bom","rfs","traffic"])
async def test_replay_uses_shared_selection_for_unknown_and_missing(environment,source):
    app=environment; item=seed(app,source)
    p=policy(source,app.state.db.all_settings())
    dim=next(iter(p["dimensions"]))
    p["dimensions"][dim]={"mode":"selected","selected":[]}
    app.state.db.set_setting(source+"_notice_selection",p)
    preview=await app.state.troubleshooting.preview(source,"resend")
    assert not preview["errors"] and preview["notices"]==0
    assert not app.state.tx.sent
    for d in p["dimensions"]: p["dimensions"][d]={"mode":"all","selected":[]}
    app.state.db.set_setting(source+"_notice_selection",p)
    preview=await app.state.troubleshooting.preview(source,"resend")
    assert not preview["errors"] and preview["notices"]==1
    app.state.troubleshooting.start(preview["token"])
    await app.state.troubleshooting.task
    assert len(app.state.tx.sent)==1
    record=rows(app.state.db,"SELECT * FROM service_history ORDER BY id DESC LIMIT 1")[0]
    evidence=json.loads(record["metadata"])["notice_selection"]
    assert evidence["included"] and evidence["catalogue"]["version"]==1


def test_inventory_counts_ids_not_history_revisions_and_preserves_old_unknown_fields(environment):
    app=environment; item=seed(app,"rfs")
    for _ in range(3): app.state.db.rfs_add_history(item,"","","","filtered")
    inv=inventory(app.state.db,"rfs")
    assert inv["level"]["advice"]["observed"]==1
    assert inv["level"]["advice"]["current"]==1
    app.state.db.add_service_history("rfs","historical","Old","",metadata={})
    assert inventory(app.state.db,"rfs")["kind"][MISSING]["observed"]==0


def test_bom_filterrules_delegate_without_changing_legacy_callers():
    assert should_include("Strange Warning",FilterRules([], ["Warning"], []))
    p=proposal("bom",{"filter_include_suffix":["Warning"]})
    rules=FilterRules.from_settings({"bom_notice_selection":p})
    assert not should_include("Strange Warning",rules)
    assert should_include("Detailed Severe Thunderstorm Warning",rules)


def test_provider_severity_is_stored_only_when_supplied():
    from app.bom_enricher import parse_warning_api
    assert parse_warning_api({"warning":{"severity":"Severe","urgency":"Immediate","certainty":"Observed"}}).severity=="Severe"
    assert parse_warning_api({"warning":{}}).severity==""


def test_missing_traffic_category_is_distinct_from_legacy_display_and_revision():
    from app.traffic.feed import parse_feed
    payload={"type":"FeatureCollection","features":[{"id":1,"properties":{},"geometry":{}}],"lastPublished":time.time()*1000}
    item=parse_feed("incident",payload)[0]
    assert item.category=="INCIDENT" and item.raw_category==""
    assert classify("traffic","category",values_for("traffic",item)["category"])["id"]==MISSING
    assert item.revision==replace(item,raw_category="anything").revision


@pytest.mark.asyncio
@pytest.mark.parametrize("source",["bom","rfs","traffic"])
async def test_queued_notice_is_invalidated_by_current_classification_settings(environment,source):
    app=environment; seed(app,source); app.state.tx.pause=True
    preview=await app.state.troubleshooting.preview(source,"resend")
    app.state.troubleshooting.start(preview["token"])
    # Replay waits for callbacks; wait until the guarded notice is queued.
    import asyncio
    for _ in range(100):
        if app.state.tx.pending: break
        await asyncio.sleep(.01)
    assert app.state.tx.pending
    parts,callback,guard=app.state.tx.pending[0]
    assert guard()
    p=proposal(source,app.state.db.all_settings())
    for dim in p["dimensions"]: p["dimensions"][dim]={"mode":"selected","selected":[]}
    app.state.db.set_setting(source+"_notice_selection",p)
    assert not guard()
    for index in range(len(parts)): callback(index,False,"Settings changed")
    await app.state.troubleshooting.task

@pytest.mark.asyncio
async def test_bom_newly_selected_same_revision_gets_a_new_history_decision(environment):
    app=environment; item=seed(app,"bom"); db=app.state.db
    p=proposal("bom",db.all_settings()); p["dimensions"]["type"]={"mode":"selected","selected":[]}
    db.set_setting("bom_notice_selection",p)
    async def process():
        s=db.all_settings()
        return await app.state.poller._process(dict(item),FilterRules.from_settings(s),"Australia/Sydney",0,False,s)
    await process()
    assert not app.state.tx.sent
    p["dimensions"]["type"]["selected"]=["severe-thunderstorm-warning"]
    db.set_setting("bom_notice_selection",p)
    await process()
    assert len(app.state.tx.sent)==1
    # An exclusion and re-inclusion must not resend a completed delivery.
    p["dimensions"]["type"]["selected"]=[];db.set_setting("bom_notice_selection",p)
    await process()
    p["dimensions"]["type"]["selected"]=["severe-thunderstorm-warning"];db.set_setting("bom_notice_selection",p)
    await process()
    assert len(app.state.tx.sent)==1


def test_snapshot_pins_bom_qualifier_patterns():
    p=proposal("bom",{})
    p["dimensions"]["type"]={"mode":"selected","selected":[UNKNOWN]}
    s={"bom_notice_selection":p};values={"type":"Severe Thunderstorm Warning - Example District"}
    assert evaluate("bom",values,s).included
    updated=deepcopy(p)
    next(e for e in updated["catalogue"]["dimensions"]["type"] if e["id"]=="severe-thunderstorm-warning")["patterns"]["qualifiers"].append("Example District")
    assert not evaluate("bom",values,s|{"bom_notice_selection":updated}).included
    assert evaluate("bom",values,s).included


def test_ordinary_service_settings_keep_adopted_classification_policy(environment):
    from app.web.routes import router as web_router
    from app.rfs.web import router as rfs_router
    app=environment;app.include_router(web_router);app.include_router(rfs_router)
    client=TestClient(app)
    for source in ("bom","rfs"):
        p=proposal(source,app.state.db.all_settings());app.state.db.set_setting(source+"_notice_selection",p)
        form={"poll_interval":"5","rfs_poll_minutes":"5",source+"_enabled":"1"}
        response=client.post("/settings/"+source,data=form,follow_redirects=False)
        assert response.status_code==303
        assert app.state.db.get_setting(source+"_notice_selection")==p


def test_validation_error_never_saves_or_transmits(environment):
    app=environment;app.include_router(router);client=TestClient(app)
    before=app.state.db.all_settings()
    response=client.post("/settings/notices/traffic",data={"action":"save","category_mode":"selected","category_selected":"invented"})
    assert response.status_code==422
    assert app.state.db.all_settings()==before and not app.state.tx.sent


def test_brief_thunderstorm_deduplicates_location_and_preserves_hazard_variants():
    from app.brief import brief_bom_parts
    from app.models import Alert
    alert=Alert("one","Severe Thunderstorm Warning – Canberra","Storm","Canberra","","","Alert")
    parts=brief_bom_parts(alert,"Australia/Sydney","NEW",126)
    assert " || ".join(parts).count("Canberra")==1
    alert.event="Severe Thunderstorm Warning - Damaging Winds"
    assert "Damaging Winds" in " || ".join(brief_bom_parts(alert,"Australia/Sydney","NEW",126))

@pytest.mark.asyncio
async def test_previous_rfs_closure_bypasses_new_classification_selection(environment):
    app=environment;item=seed(app,"rfs");db=app.state.db
    # Establish a recorded successful delivery before the provider reports closure.
    db.rfs_add_history(item,"sent","success","")
    from app.geography import configuration, KEY
    db.set_setting(KEY,configuration(all_nsw=True))
    closed=replace(item,status="Out")
    p=proposal("rfs",db.all_settings())
    for dim in p["dimensions"]:p["dimensions"][dim]={"mode":"selected","selected":[]}
    db.set_setting("rfs_notice_selection",p)
    await app.state.rfs_poller.poll_once(replay_items=[closed])
    assert len(app.state.tx.sent)==1


def test_queue_evidence_uses_latest_raw_traffic_category(environment):
    from app.notice_selection import saved_values_for
    app=environment;item=seed(app,"traffic")
    saved=app.state.db.traffic_get_item(item.item_id)
    row=dict(saved);data=json.loads(row["normalized_data"]);data["raw_category"]="";row["normalized_data"]=json.dumps(data)
    assert saved_values_for("traffic",item,row)["category"]==""


@pytest.mark.asyncio
@pytest.mark.parametrize("source",["bom","rfs","traffic"])
@pytest.mark.parametrize("raw,special",[("",MISSING),("Future provider warning",UNKNOWN)])
async def test_normal_processing_transmits_explicitly_selected_special_classifications(environment,source,raw,special):
    app=environment;item=seed(app,source);db=app.state.db
    p=proposal(source,db.all_settings())
    for dim in p["dimensions"]:p["dimensions"][dim]={"mode":"all","selected":[]}
    dim=next(iter(p["dimensions"]));p["dimensions"][dim]={"mode":"selected","selected":[special]}
    db.set_setting(source+"_notice_selection",p)
    if source=="bom":
        item["event"]=raw;s=db.all_settings()
        await app.state.poller._process(dict(item),FilterRules.from_settings(s),"Australia/Sydney",0,False,s)
    else:
        item=replace(item,level=raw) if source=="rfs" else replace(item,category=raw or "INCIDENT",raw_category=raw)
        class Client:
            async def fetch(self):return [item]
            async def boundaries(self):return []
        poller=app.state.rfs_poller if source=="rfs" else app.state.traffic_poller
        poller.client=Client()
        await poller.poll_once()
    assert len(app.state.tx.sent)==1
    history=rows(db,"SELECT * FROM service_history ORDER BY id DESC LIMIT 1")[0]
    selection=json.loads(history["metadata"])["notice_selection"]
    assert selection["dimensions"][dim]["raw"]==raw
    assert selection["dimensions"][dim]["id"]==special
    observed=inventory(db,source)[dim]
    assert observed[special]["observed"] >= 1
    if special == MISSING:
        assert observed[UNKNOWN]["observed"] == 0

@pytest.mark.asyncio
@pytest.mark.parametrize('source',['bom','traffic'])
async def test_terminal_notices_keep_previous_transmission_exception_with_catalogue_selection(environment,source):
    from app.geography import configuration, KEY
    app=environment;item=seed(app,source);db=app.state.db
    db.set_setting(KEY,configuration(all_nsw=True))
    if source=='bom':
        s=db.all_settings()
        await app.state.poller._process(dict(item),FilterRules.from_settings(s),'Australia/Sydney',0,False,s)
        app.state.tx.sent.clear()
        item=dict(item,message_type='Cancel')
    else:
        db.add_service_history('traffic',item.item_id,'Previously sent','',transmit_status='success')
        item=replace(item,ended=True)
    p=proposal(source,db.all_settings())
    for dim in p['dimensions']:p['dimensions'][dim]={'mode':'selected','selected':[]}
    db.set_setting(source+'_notice_selection',p)
    if source=='bom':
        s=db.all_settings()
        await app.state.poller._process(item,FilterRules.from_settings(s),'Australia/Sydney',0,False,s)
    else:
        await app.state.traffic_poller.poll_once(replay_items=[item])
    assert len(app.state.tx.sent)==1


@pytest.mark.parametrize("source",["bom","rfs","traffic"])
def test_unified_layout_redirects_and_hides_migration_after_application(environment,source):
    from app.web.routes import router as web_router
    from app.rfs.web import router as rfs_router
    from app.traffic.web import router as traffic_router
    app=environment
    for service_router in (router,web_router,rfs_router,traffic_router):app.include_router(service_router)
    client=TestClient(app);db=app.state.db
    old=db.all_settings()
    response=client.get('/settings/notices/'+source,follow_redirects=False)
    assert response.status_code==303 and response.headers['location']=='/settings/'+source
    body=client.get('/settings/'+source).text
    assert body.index('<h3>Monitoring') < body.index('id="notice-selection"') < body.index('<h3>Polling')
    assert 'Existing rules are active' in body
    assert 'Unrecognized' in body and 'Not supplied' in body
    form={'preserve_notice_selection':'1','poll_interval':'5',source+'_poll_minutes':'5',source+'_enabled':'1'}
    if source=='traffic':form['traffic_types']=old['traffic_types']
    client.post('/settings/'+source,data=form,follow_redirects=False)
    for key in ('rfs_levels','filter_include_exact','filter_include_suffix','filter_exclude_exact'):
        assert db.get_setting(key)==old[key]
    form={'action':'save','catalogue':'latest'}
    for dim in proposal(source,db.all_settings())['dimensions']:form[dim+'_mode']='all'
    result=client.post('/settings/notices/'+source,data=form,follow_redirects=False)
    assert result.headers['location']=='/settings/'+source+'?saved=1'
    body=client.get(result.headers['location']).text
    assert 'Existing rules are active' not in body
    assert 'Legacy' not in body and 'legacy' not in body
    assert 'Restore legacy' not in body
    assert 'Save selection' in body
    assert 'Unrecognized' in body and 'Not supplied' in body
    assert not app.state.tx.sent

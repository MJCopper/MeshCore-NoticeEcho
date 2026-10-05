import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.bom_area import CouncilMatch
from app.bom_enricher import WarningSection
from app.bom_geography import match_geography
from app.models import Alert


def alert(area="Hunter", **changes):
    return replace(Alert("geo", "Flood Warning", "Flood Warning", area, "", "", "Alert"), **changes)


def settings(**changes):
    return dict(bom_all_councils=False, bom_councils=["Tamworth Regional"],
                bom_districts=["Hunter"], bom_include_unknown_councils=False, **changes)


@pytest.mark.parametrize("councils,area,included", [
    (("Tamworth Regional",),"Illawarra",True),
    (("Campbelltown",),"Hunter",True),
    (("Tamworth Regional",),"Hunter",True),
    (("Campbelltown",),"Illawarra",False),
])
def test_council_or_district_neither_can_veto_the_other(councils,area,included):
    result=match_geography(alert(area),CouncilMatch("matched",councils),settings())
    assert result.included is included
    if included:
        assert result.councils or result.districts


@pytest.mark.parametrize("changes,included", [
    ({"bom_all_councils":True,"bom_councils":[],"bom_districts":[]},True),
    ({"bom_councils":[],"bom_districts":[],"bom_include_unknown_councils":True},False),
    ({"bom_councils":[],"bom_districts":["Hunter"]},True),
    ({"bom_councils":["Tamworth Regional"],"bom_districts":[]},False),
    ({"bom_include_unknown_councils":True},True),
])
def test_empty_selections_all_nsw_and_unknown_fallback(changes,included):
    config=settings()
    config.update(changes)
    result=match_geography(alert(),CouncilMatch("unknown"),config)
    assert result.included is included


def test_unrelated_narrative_and_partial_place_names_do_not_match():
    for candidate in (
        alert("Hunterville"),
        alert("Illawarra",warning_summary="Background information about Hunter rainfall."),
        alert("Illawarra",headline="Previous Hunter warning discussed today"),
    ):
        assert not match_geography(candidate,CouncilMatch("unknown"),settings()).included


@pytest.mark.parametrize("field,text",[
    ("headline","Flood Warning for Hunter"),
    ("warning_summary","Heavy rainfall in parts of Hunter district."),
    ("warning_summary","Locations which may be affected include Hunter."),
    ("detail","<p>Flooding affecting Hunter district.</p>"),
])
def test_explicit_affected_area_wording_matches(field,text):
    result=match_geography(alert("NSW",**{field:text}),CouncilMatch("unknown"),settings())
    assert result.districts==("Hunter",)


def test_provider_district_names_match_without_narrative_search():
    result=match_geography(alert("NSW"),CouncilMatch("unknown"),settings(),
                          SimpleNamespace(area_names=("Hunter forecast district",),geocodes=()))
    assert result.districts==("Hunter",)


def test_cancelled_only_district_does_not_qualify_active_warning():
    candidate=alert("Hunter, Illawarra",warning_summary=(
        "Heavy rainfall in parts of Illawarra. "
        "Flooding is no longer occurring in the Hunter district and the warning for this district is CANCELLED."))
    assert not match_geography(candidate,CouncilMatch("unknown"),settings()).included


def test_marine_active_and_cancelled_scopes_are_matched_separately():
    candidate=alert("Hunter",warning_sections=(
        WarningSection("Cancellation","Hunter","CAN"),
        WarningSection("Strong Wind Warning","Illawarra","REN")))
    assert not match_geography(candidate,CouncilMatch("unknown"),settings()).included
    config=settings()
    config["bom_districts"]=["Illawarra"]
    assert match_geography(candidate,CouncilMatch("unknown"),config).included


@pytest.mark.asyncio
@pytest.mark.parametrize("area,councils,districts",[
    ("Tamworth Regional Council",["Tamworth Regional"],["Illawarra"]),
    ("Hunter",["Campbelltown"],["Hunter"]),
    ("Hunter",[],["Hunter"]),
])
async def test_or_matching_is_identical_in_polling_preview_and_resend(area,councils,districts):
    import asyncio
    from datetime import datetime, timedelta, timezone
    from fastapi import FastAPI
    from app.db import Database
    from app.poller import BomPoller
    from app.troubleshooting import Troubleshooting
    from app.filters import FilterRules
    db=Database(":memory:")
    for key,value in {
        "bom_enabled":True,"bom_all_councils":False,"bom_councils":councils,
        "bom_districts":districts,"bom_include_unknown_councils":False,
        "filter_include_suffix":["Warning"],"dry_run":True
    }.items():
        db.set_setting(key,value)
    radio=SimpleNamespace(message_budget=126)
    poller=BomPoller(db,radio)
    item=dict(id="geo",region="NSW",event="Flood Warning",headline="Flood Warning",
              area_desc=area,message_type="Alert",references=[],
              expires=(datetime.now(timezone.utc)+timedelta(days=1)).isoformat())
    await poller._process(item,FilterRules([],["Warning"],[]),"Australia/Sydney",0,True)
    original=db.latest_history("geo")["transmitted_text"]
    assert item["selection"]=="included"
    assert db.latest_history("geo")["transmit_status"]=="dry-run"
    db.replace_bom_current([item],{"NSW"},datetime.now(timezone.utc).isoformat())
    app=FastAPI()
    app.state.db,app.state.tx,app.state.poller=db,radio,poller
    service=Troubleshooting(app)
    for mode in ("reprocess","resend"):
        preview=await service.preview("bom",mode)
        assert preview["notices"]==1 and not preview["errors"]
        service.start(preview["token"])
        await service.task
        assert service.job["status"]=="completed"
    assert db.query_service_history(source="bom")[-1]["transmitted_text"]==original
    db.close()


@pytest.mark.asyncio
async def test_queued_warning_remains_valid_when_either_selection_matches():
    from datetime import datetime, timedelta, timezone
    from app.db import Database
    from app.poller import BomPoller
    from app.filters import FilterRules
    db=Database(":memory:")
    db.set_setting("bom_all_councils",False)
    db.set_setting("bom_councils",["Tamworth Regional"])
    db.set_setting("bom_districts",["Illawarra"])
    db.set_setting("bom_include_unknown_councils",False)
    db.set_setting("filter_include_suffix",["Warning"])
    class Radio:
        message_budget=126
        supports_notice_guards=True
        def enqueue_notice(self, parts, on_result=None, priority=1, valid_if=None):
            self.guard=valid_if
            return True
    radio=Radio()
    poller=BomPoller(db,radio)
    item=dict(id="queue",region="NSW",event="Flood Warning",headline="Flood Warning",
              area_desc="Tamworth Regional Council",message_type="Alert",references=[],
              expires=(datetime.now(timezone.utc)+timedelta(days=1)).isoformat())
    await poller._process(item,FilterRules([],["Warning"],[]),"Australia/Sydney",0,False)
    assert radio.guard()
    db.set_setting("bom_districts",["Tamworth Regional"])
    db.set_setting("bom_councils",["Campbelltown"])
    assert radio.guard()  # The district match keeps the queued notice eligible.
    db.set_setting("bom_districts",["Illawarra"])
    assert not radio.guard()
    db.close()


@pytest.mark.asyncio
async def test_adding_district_reconsiders_confirmed_warning_but_does_not_repeat():
    from app.db import Database
    from app.poller import BomPoller
    from app.filters import FilterRules
    db=Database(":memory:")
    candidate=alert("Hunter")
    db.set_setting("bom_all_councils",False)
    db.set_setting("bom_councils",["Tamworth Regional"])
    db.set_setting("bom_districts",["Illawarra"])
    db.set_setting("bom_include_unknown_councils",False)
    db.upsert_state(alert_id=candidate.alert_id,event=candidate.event,headline=candidate.headline,
        expires="",msg_hash=candidate.content_hash(),disposition="sent",sent_ts="2026-10-01T00:00:00Z")
    radio=SimpleNamespace(message_budget=126)
    poller=BomPoller(db,radio)
    item=dict(id=candidate.alert_id,region="NSW",event=candidate.event,headline=candidate.headline,
              area_desc=candidate.area_desc,message_type="Alert",references=[])
    await poller._process(item,FilterRules([],["Warning"],[]),"Australia/Sydney",0,True)
    assert db.latest_history("geo")["disposition"]=="filtered"
    db.set_setting("bom_districts",["Hunter"])
    await poller._process(item,FilterRules([],["Warning"],[]),"Australia/Sydney",0,True)
    row=db.latest_history("geo")
    assert row["transmit_status"]=="dry-run" and row["disposition"]=="update"
    assert json.loads(row["metadata"])["selected_districts"]==["Hunter"]
    assert "forecast district match" in row["detail"]
    count=len(db.query_service_history(source="bom"))
    await poller._process(item,FilterRules([],["Warning"],[]),"Australia/Sydney",0,True)
    assert len(db.query_service_history(source="bom"))==count
    db.close()


@pytest.mark.asyncio
async def test_mixed_warning_sends_only_cancellation_for_previously_sent_product():
    from app.db import Database
    from app.poller import BomPoller
    from app.filters import FilterRules
    db=Database(":memory:")
    config=settings()
    config["bom_councils"]=[]
    for key,value in config.items():
        db.set_setting(key,value)
    db.upsert_state(alert_id="mixed",event="Flood Warning",headline="",expires="",
                    msg_hash="previous",disposition="sent",sent_ts="2026-10-01T00:00:00Z")
    item=dict(id="mixed",region="NSW",event="Flood Warning",headline="Flood Warning",
              area_desc="Illawarra",message_type="Alert",references=[],
              detail="Flooding in parts of Illawarra. Flooding is no longer occurring in the Hunter district and the warning for this district is CANCELLED.")
    class Radio:
        message_budget=126
        supports_notice_guards=True
        def __init__(self):
            self.parts=[]
        def enqueue_notice(self, parts, on_result=None, priority=1, valid_if=None):
            self.parts.extend(p[0] for p in parts)
            for i in range(len(parts)):
                on_result(i,True,"")
            return True
    radio=Radio()
    poller=BomPoller(db,radio)
    await poller._process(item,FilterRules([],["Warning"],[]),"Australia/Sydney",0,False)
    text=" ".join(radio.parts)
    assert "CANCELLED" in text and "Hunter" in text
    assert "Illawarra" not in text
    assert "Cancellation-only" in item["selection_reason"]
    assert db.get_state("mixed")["disposition"]=="cancelled"
    count=len(radio.parts)
    await poller._process(item,FilterRules([],["Warning"],[]),"Australia/Sydney",0,False)
    assert len(radio.parts)==count
    db.close()


@pytest.mark.asyncio
async def test_legacy_confirmed_warning_does_not_repeat_after_or_upgrade():
    from app.db import Database
    from app.poller import BomPoller
    from app.filters import FilterRules
    db = Database(":memory:")
    candidate = alert("Tamworth Regional Council", headline="Flood Warning for Hunter")
    config = settings()
    for key, value in config.items():
        db.set_setting(key, value)
    db.upsert_state(alert_id="geo", event=candidate.event, headline=candidate.headline,
                    expires="", msg_hash=candidate.content_hash(), disposition="sent",
                    sent_ts="2026-10-01T00:00:00Z")
    db.add_history("geo", candidate.event, candidate.area_desc, "sent",
                   transmitted_text="Previously confirmed message", transmit_status="sent",
                   metadata={"selection": "included", "all_councils": False,
                             "selected_councils": ["Tamworth Regional"], "include_unknown": False})
    poller = BomPoller(db, SimpleNamespace(message_budget=126))
    item = dict(id="geo", region="NSW", event=candidate.event, headline=candidate.headline,
                area_desc=candidate.area_desc, message_type="Alert", references=[])
    await poller._process(item, FilterRules([], ["Warning"], []), "Australia/Sydney", 0, True)
    rows = db.query_service_history(source="bom")
    assert rows[0]["disposition"] == "duplicate"
    assert rows[0]["transmit_status"] is None
    assert rows[-1]["transmitted_text"] == "Previously confirmed message"
    await poller._process(item, FilterRules([], ["Warning"], []), "Australia/Sydney", 0, True)
    assert len(db.query_service_history(source="bom")) == len(rows)
    db.close()

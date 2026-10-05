"""Live Traffic NSW feed, council and sending behavior."""
import time
from dataclasses import replace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.db import Database
from app.traffic.feed import TrafficItem, council_at, council_at_prepared, prepare_councils, parse_feed
from app.traffic.poller import TrafficPoller
from app.traffic.web import router


POLYGONS = [{"type": "Feature", "properties": {"lganame": "Central Coast"},
             "geometry": {"type": "Polygon", "coordinates": [[[150, -34], [152, -34],
                                                             [152, -32], [150, -32], [150, -34]]]}}]


def item(item_id="incident:1", feed="incident", category="CRASH", **changes):
    event = TrafficItem(item_id, feed, category, "CRASH", "Pacific Highway", "Gosford", "",
                        "Northbound", "Road closed", "Avoid area", 151, -33, None, None,
                        False, time.time())
    return replace(event, **changes)


class Client:
    def __init__(self, items):
        self.items = items

    async def fetch(self):
        return self.items

    async def boundaries(self):
        return POLYGONS


class Tx:
    message_budget = 145
    queue_depth = 0

    def __init__(self):
        self.sent = []

    def enqueue(self, text, on_result=None, delay_after=None):
        self.sent.append((text, on_result))
        return True

    def enqueue_verification(self, text, on_result=None, allow_new=True):
        return True


def configure(db, dry_run=False):
    db.set_setting("traffic_enabled", True)
    db.set_setting("traffic_councils", ["Central Coast"])
    db.set_setting("traffic_types", ["incident", "regional", "flood"])
    db.set_setting("dry_run", dry_run)


def test_parse_status_and_point_in_council():
    payload = {"type": "FeatureCollection", "lastPublished": int(time.time() * 1000),
               "features": [{"id": 2, "geometry": {"coordinates": [151, -33]},
                             "properties": {"displayName": "CRASH", "mainCategory": "CRASH",
                                            "ended": True, "roads": [{"mainStreet": "Pacific Highway"}]}}]}
    parsed = parse_feed("incident", payload)[0]
    assert parsed.item_id == "incident:2"
    assert not parsed.active()
    assert council_at(151, -33, POLYGONS) == "Central Coast"
    assert not council_at(153, -33, POLYGONS)
    assert not item(start=time.time() + 3600).active()


def test_prepared_councils_match_raw_polygons_and_holes():
    polygons = [{"properties": {"lganame": "Test"}, "geometry": {
        "type": "Polygon", "coordinates": [
            [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]],
            [[4, 4], [6, 4], [6, 6], [4, 6], [4, 4]],
        ]}}]
    prepared = prepare_councils(polygons)
    for point in ((2, 2), (5, 5), (20, 20)):
        assert council_at_prepared(*point, prepared) == council_at(*point, polygons)


@pytest.mark.asyncio
async def test_first_live_poll_is_baseline_then_new_council_sends_current_item():
    db = Database(":memory:")
    configure(db)
    tx = Tx()
    client = Client([item()])
    poller = TrafficPoller(db, tx, client)
    await poller.poll_once()
    assert poller.last_successful_poll == poller.last_poll
    assert tx.sent == []
    assert db.latest_service_history("traffic", "incident:1")["disposition"] == "baseline"
    await poller.poll_once()
    assert tx.sent == []
    client.items = [item(), item("incident:2", lon=153, council="Cessnock")]
    await poller.poll_once()
    assert tx.sent == []
    assert db.latest_service_history("traffic", "incident:2")["disposition"] == "excluded-council"
    db.set_setting("traffic_councils", ["Central Coast", "Cessnock"])
    await poller.poll_once()
    assert tx.sent and "CRASH" in tx.sent[0][0] and "Pacific Highway" in tx.sent[0][0]
    for _, callback in tx.sent:
        callback(True, "")
    assert db.traffic_get_item("incident:2")["last_sent_hash"] == client.items[1].revision
    db.close()


@pytest.mark.asyncio
async def test_ended_future_and_roadwork_are_recorded_but_not_sent():
    db = Database(":memory:")
    configure(db)
    db.set_setting("traffic_baseline_done", True)
    tx = Tx()
    events = [item("incident:ended", ended=True),
              item("incident:future", start=time.time() + 3600),
              item("roadwork:1", feed="roadwork", category="SCHEDULED ROADWORK")]
    poller = TrafficPoller(db, tx, Client(events))
    await poller.poll_once()
    assert not tx.sent
    assert db.latest_service_history("traffic", "incident:ended")["disposition"] == "excluded-ended-or-not-yet-active"
    assert db.latest_service_history("traffic", "roadwork:1")["disposition"] == "excluded-hazard-type"
    db.set_setting("traffic_types", ["incident", "roadwork"])
    await poller.poll_once()
    assert "SCHEDULED ROADWORK" in tx.sent[0][0]
    assert tx.sent[-1][0].endswith("check livetraffic.com")
    db.close()


@pytest.mark.asyncio
async def test_dry_run_records_message_without_radio():
    db = Database(":memory:")
    configure(db, dry_run=True)
    tx = Tx()
    await TrafficPoller(db, tx, Client([item()])).poll_once()
    row = db.latest_service_history("traffic", "incident:1")
    assert row["transmit_status"] == "dry-run"
    assert "Pacific Highway" in row["transmitted_text"]
    assert not tx.sent
    db.close()


def test_traffic_settings_page_and_save():
    db = Database(":memory:")
    app = FastAPI()
    app.include_router(router)
    app.state.db = db
    app.state.tx = Tx()
    app.state.traffic_poller = type("Poller", (), {"poke": lambda self: None})()
    client = TestClient(app)
    page = client.get("/settings/traffic")
    assert page.status_code == 200
    assert 'name="traffic_poll_minutes" type="number" min="5"' in page.text
    response = client.post("/settings/traffic", data={"traffic_enabled": "1",
                       "traffic_councils": "Central Coast", "traffic_types": "incident"},
                       follow_redirects=False)
    assert response.status_code == 303
    assert db.get_setting("traffic_councils") == []
    client.post("/settings/traffic", data={"traffic_poll_minutes": "2"}, follow_redirects=False)
    assert db.get_setting("traffic_poll_minutes") == 5
    client.post("/settings/traffic", data={"traffic_poll_minutes": "17"}, follow_redirects=False)
    assert db.get_setting("traffic_poll_minutes") == 17
    db.set_setting("traffic_councils", ["Central Coast"])
    db.set_setting("traffic_all_councils", True)
    page = client.get("/settings/traffic").text
    assert 'href="/settings/geography"' in page
    assert 'name="traffic_councils"' not in page
    assert "Existing service coverage remains active" in page
    client.post("/settings/traffic", data={"traffic_enabled": "1", "traffic_all_councils": "1"},
                follow_redirects=False)
    assert db.get_setting("traffic_councils") == ["Central Coast"]
    db.close()


@pytest.mark.asyncio
async def test_dry_run_preview_preserves_first_live_baseline_then_new_item_sends():
    db = Database(":memory:")
    configure(db, dry_run=True)
    tx = Tx()
    poller = TrafficPoller(db, tx, Client([item()]))
    await poller.poll_once()
    assert not tx.sent
    db.set_setting("dry_run", False)
    await poller.poll_once()
    assert not tx.sent
    assert db.latest_service_history("traffic", "incident:1")["disposition"] == "baseline"
    poller.client.items.append(item("incident:2"))
    await poller.poll_once()
    first_count = len(tx.sent)
    assert first_count > 0
    assert db.latest_service_history("traffic", "incident:2")["transmit_status"] == "queued"
    assert db.traffic_recover_queued() == 1
    await poller.poll_once()
    assert len(tx.sent) == first_count * 2
    db.close()


@pytest.mark.asyncio
async def test_missing_item_is_recorded_without_claiming_road_reopened():
    db = Database(":memory:")
    configure(db)
    db.set_setting("traffic_baseline_done", True)
    tx = Tx()
    client = Client([item()])
    poller = TrafficPoller(db, tx, client)
    await poller.poll_once()
    for _, callback in tx.sent:
        callback(True, "")
    first_count = len(tx.sent)
    client.items = []
    await poller.poll_once()
    assert db.latest_service_history("traffic", "incident:1")["transmit_status"] == "success"
    await poller.poll_once()
    row = db.latest_service_history("traffic", "incident:1")
    assert row["disposition"] == "absent-from-feed"
    assert "unconfirmed" in row["detail"]
    assert len(tx.sent) == first_count
    db.close()


@pytest.mark.asyncio
async def test_traffic_revision_is_labelled_update_after_successful_broadcast():
    db = Database(":memory:")
    configure(db)
    db.set_setting("traffic_baseline_done", True)
    tx = Tx()
    client = Client([item()])
    poller = TrafficPoller(db, tx, client)
    await poller.poll_once()
    assert tx.sent and "Live Traffic NSW NEW" in tx.sent[0][0]
    for _, callback in tx.sent:
        callback(True, "")
    first_count = len(tx.sent)
    client.items = [item(impact="Two lanes closed", advice="Avoid the area")]
    await poller.poll_once()
    assert len(tx.sent) > first_count
    assert "Live Traffic NSW UPDATE" in tx.sent[first_count][0]
    db.close()


@pytest.mark.asyncio
async def test_fire_feed_is_requested_and_delivered_with_rfs_enabled(monkeypatch):
    from app.traffic.feed import TrafficClient
    db=Database(":memory:")
    configure(db)
    db.set_setting("traffic_types",["fire"])
    db.set_setting("rfs_enabled",True)
    db.set_setting("traffic_baseline_done",True)
    db.set_setting("traffic_baseline_feeds",["fire"])
    event=item("fire:1",feed="fire",category="FIRE",title="Fire",impact="Road closed")
    requested=[]
    async def fetch(self, feeds):
        requested.append(feeds)
        return [event]
    async def boundaries(self):
        return POLYGONS
    monkeypatch.setattr(TrafficClient,"fetch",fetch)
    monkeypatch.setattr(TrafficClient,"boundaries",boundaries)
    tx=Tx()
    tx.supports_notice_guards=True
    guards=[]
    def enqueue_notice(parts,on_result=None,priority=1,valid_if=None):
        guards.append(valid_if)
        tx.sent.append(parts)
        return True
    tx.enqueue_notice=enqueue_notice
    await TrafficPoller(db,tx,TrafficClient()).poll_once()
    assert requested==[{"fire"}]
    assert tx.sent and guards[0]()
    assert "Northbound road closed" in " ".join(p[0] for p in tx.sent[0])
    assert db.latest_service_history("traffic",event.item_id)["transmit_status"]=="queued"
    db.close()


@pytest.mark.parametrize("enabled,selected,expected",[
    (False,True,"Service disabled"),
    (True,False,"Feed not selected"),
    (True,True,"Selected; awaiting first poll"),
])
def test_fire_feed_diagnostics_with_rfs_enabled(enabled,selected,expected):
    from app.presentation import traffic_feed_status
    requested,label=traffic_feed_status({"traffic_enabled":enabled,
        "traffic_types":["fire"] if selected else [],"rfs_enabled":True},"fire","")
    assert requested is (enabled and selected)
    assert label==expected


@pytest.mark.parametrize("direction,impact,expected",[
    ("Northbound","Affected","Northbound affected"),
    ("Both directions","Affected","Both directions affected"),
    ("Northbound","Road closed","Northbound road closed"),
    ("","Affected","Affected"),
    ("Northbound","","Northbound"),
    ("Northbound","Northbound road closed","Northbound road closed"),
])
def test_direction_and_impact_are_one_phrase_after_locations(direction,impact,expected):
    from app.traffic.poller import format_item
    import re
    event=item(direction=direction,impact=impact,title="CRASH")
    parts=format_item(event,"",126)
    text=" ".join(re.sub(r"^\d+/\d+ ","",p) for p in parts)
    assert expected in text
    assert text.index("Pacific Highway") < text.index(expected)
    if direction and impact:
        assert direction+"; "+impact not in text
    assert len(parts)<=2
    assert all(len(p.encode())<=126 for p in parts)


def test_repeated_breakdown_prefix_is_removed_without_losing_vehicle_detail():
    from app.traffic.poller import format_item
    import re
    event=item(category="BREAKDOWN",title="BREAKDOWN B-double",direction="Northbound",impact="Affected")
    parts=format_item(event,"",126)
    text=" ".join(re.sub(r"^\d+/\d+ ","",p) for p in parts)
    assert text.count("BREAKDOWN")==1
    assert "B-double" in text and "Northbound affected" in text
    assert "Pacific Highway" in text and "Gosford" in text

"""Shared lifecycle, atomic observations, upgrade, retention and page contract."""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch
import json
import sqlite3
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.catalogue import adapt, epoch, lifecycle
from app.db import Database
from app.maintenance import DEFAULTS, clean
from app.rfs.feed import Incident
from app.rfs.web import router as rfs_router
from app.traffic.web import router as traffic_router
from app.web.routes import router as web_router
from test_traffic import item as traffic_item

NOW = datetime(2026, 10, 23, 12, tzinfo=timezone.utc).timestamp()
STAMP = datetime.fromtimestamp(NOW, timezone.utc).isoformat()
SOURCES = ('bom', 'rfs', 'traffic')


def snapshot(db, source, ids, *, feeds=None, changes=None):
    """Publish through the same repository boundaries used by real pollers."""
    changes = changes or {}
    if source == 'bom':
        db.replace_bom_current([
            dict(id=key, region='NSW', event='Flood Warning', headline=key, area_desc='Sydney',
                 effective='2026-10-01T00:00:00+00:00') | changes.get(key, {}) for key in ids
        ], {'NSW'} if feeds is None else feeds, STAMP)
    elif source == 'rfs':
        for key in ids:
            values = dict(incident_id=key, name=key, level='Advice', council='Sydney', location='',
                          status='Under control', kind='Bush Fire', updated='23 Oct 2026 12:00', source_url='')
            values.update(changes.get(key, {}))
            event = Incident(**values)
            db.rfs_save_incident(event, event.revision, catalogue_observation=False)
        db.rfs_missing_after_poll(set(ids))
    else:
        for key in ids:
            values = dict(changes.get(key, {}))
            feed = key.split(':')[0] if ':' in key else 'incident'
            event = traffic_item(key, feed=feed, title=key, updated=NOW, **values)
            db.traffic_save_item(event, 'Sydney', event.active(NOW), catalogue_observation=False)
        db.traffic_missing_after_poll(set(ids), {'incident'} if feeds is None else feeds)


def listing(db, source, view='current', now=NOW, page=1):
    return db.current_listing(source, page, view=view, now=now)


@pytest.fixture
def db():
    database = Database(':memory:')
    yield database
    database.close()


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('present,cancelled,ended,start,end,expected', [
    (1, 0, 0, None, None, 'current'),
    (1, 0, 0, NOW + 1, None, 'upcoming'),
    (1, 0, 0, NOW, None, 'current'),
    (1, 0, 0, None, NOW, 'expired'),
    (1, 0, 0, None, NOW + 1, 'current'),
    (1, 1, 0, None, None, 'cancelled'),
    (1, 0, 1, None, None, 'ended'),
    (0, 0, 0, NOW + 1, None, 'absent'),
    (0, 1, 0, None, None, 'cancelled'),
    (0, 0, 0, None, NOW, 'expired'),
    (None, 0, 0, None, None, 'unknown'),
])
def test_python_and_sql_share_exact_lifecycle(source, present, cancelled, ended, start, end, expected, db):
    assert lifecycle(present, cancelled, ended, start, end, NOW) == expected
    with db._lock:
        db._conn.execute('INSERT INTO source_catalogue(source,feed,item_id,payload,present,cancelled,ended,starts_at,expires_at,last_seen) VALUES (?,?,?,?,?,?,?,?,?,?)',
                         (source, 'test', 'one', '{}', present, cancelled, ended, start, end, STAMP))
        db._conn.commit()
    rows, page = listing(db, source, 'all')
    assert rows[0]['lifecycle'] == expected
    view = expected if expected in ('current', 'upcoming') else 'all' if expected == 'unknown' else 'past'
    assert listing(db, source, view)[1]['total'] == 1
    assert sum(page['counts'][key] for key in ('current', 'upcoming', 'past')) == (0 if expected == 'unknown' else 1)


@pytest.mark.parametrize('value', [None, '', 'invalid', '2026-10-23T12:00:00', float('nan'), float('inf'), True])
def test_untrusted_dates_do_not_create_boundaries(value):
    assert epoch(value) is None


def test_offsets_and_daylight_saving_boundaries():
    assert epoch('2026-10-23T23:00:00+11:00') == NOW
    before = epoch('2026-10-04T01:59:59+10:00')
    after = epoch('2026-10-04T03:00:00+11:00')
    assert after - before == 1
    assert lifecycle(1, 0, 0, after, None, before) == 'upcoming'
    assert lifecycle(1, 0, 0, after, None, after) == 'current'


@pytest.mark.parametrize('source', SOURCES)
def test_success_empty_disappearance_reappearance_and_no_catalogue_growth(db, source):
    snapshot(db, source, ['one', 'two'])
    assert listing(db, source)[1]['total'] == 2
    for _ in range(3):
        snapshot(db, source, ['one', 'two'])
    assert listing(db, source, 'all')[1]['total'] == 2
    snapshot(db, source, ['one'])
    assert listing(db, source)[1]['total'] == 1
    past = listing(db, source, 'past')[0]
    assert past[0]['lifecycle'] == 'absent'
    assert past[0]['missing_polls'] == 1
    snapshot(db, source, [])
    assert listing(db, source)[1]['total'] == 0
    assert listing(db, source, 'past')[1]['total'] == 2
    snapshot(db, source, ['two'])
    assert listing(db, source)[0][0]['lifecycle'] == 'current'
    assert listing(db, source)[0][0]['missing_polls'] == 0
    assert listing(db, source, 'all')[1]['total'] == 2


@pytest.mark.parametrize('source', SOURCES)
def test_snapshot_failure_rolls_back_catalogue(db, source):
    snapshot(db, source, ['one'])
    previous = listing(db, source, 'all')
    original = db._catalogue_upsert
    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise sqlite3.OperationalError('simulated write failure')
    with patch.object(db, '_catalogue_upsert', side_effect=fail):
        with pytest.raises(sqlite3.OperationalError):
            snapshot(db, source, ['two'])
    assert listing(db, source, 'all') == previous


def test_partial_traffic_snapshot_preserves_failed_feeds(db):
    snapshot(db, 'traffic', ['incident:one', 'flood:two'], feeds={'incident', 'flood'})
    snapshot(db, 'traffic', [], feeds={'incident'})
    current, page = listing(db, 'traffic')
    assert [row['item_id'] for row in current] == ['flood:two']
    assert page['counts']['past'] == 1
    before = listing(db, 'traffic', 'all')
    snapshot(db, 'traffic', [], feeds=set())
    assert listing(db, 'traffic', 'all') == before


@pytest.mark.parametrize('source', ('bom', 'traffic'))
def test_time_transitions_without_poll(db, source):
    changes = {'later': {'effective': datetime.fromtimestamp(NOW + 10, timezone.utc).isoformat(),
                         'expires': datetime.fromtimestamp(NOW + 20, timezone.utc).isoformat()}}
    if source == 'traffic':
        changes = {'later': {'start': NOW + 10, 'end': NOW + 20}}
    snapshot(db, source, ['later'], changes=changes)
    assert listing(db, source, 'upcoming')[1]['total'] == 1
    assert listing(db, source, now=NOW + 10)[1]['total'] == 1
    assert listing(db, source, 'past', now=NOW + 20)[0][0]['lifecycle'] == 'expired'


def test_rfs_provider_dates_do_not_imply_end_and_under_control_is_current(db):
    snapshot(db, 'rfs', ['controlled', 'out'], changes={'out': {'status': 'Extinguished'}})
    assert listing(db, 'rfs')[0][0]['name'] == 'controlled'
    assert listing(db, 'rfs', 'past')[0][0]['lifecycle'] == 'ended'


def test_bom_cancellation_is_retained_after_replacement(db):
    snapshot(db, 'bom', ['warning'])
    snapshot(db, 'bom', ['warning'], changes={'warning': {'message_type': 'Cancel'}})
    assert listing(db, 'bom')[1]['total'] == 0
    assert listing(db, 'bom', 'past')[0][0]['lifecycle'] == 'cancelled'
    snapshot(db, 'bom', [])
    assert listing(db, 'bom', 'past')[0][0]['lifecycle'] == 'cancelled'
    assert not db.bom_current_items(['NSW'])


def test_upgrade_is_repeatable_preserves_evidence_and_does_not_invent_history(tmp_path):
    path = str(tmp_path / 'upgrade.db')
    original = Database(path)
    for source in SOURCES:
        snapshot(original, source, ['one'])
    original.set_setting('custom-preserved', {'value': 42})
    original.add_service_history('traffic', 'one', 'One', '', transmit_status='deferred', revision_hash='protected')
    original._conn.execute('DROP TABLE source_catalogue')
    original._conn.execute('DROP TABLE source_snapshots')
    original._conn.execute("DELETE FROM history_migrations WHERE name='source_catalogue_v1'")
    original._conn.commit()
    history = [tuple(row) for row in original._conn.execute('SELECT * FROM service_history')]
    original.close()
    for _ in range(2):
        upgraded = Database(path)
        assert upgraded.get_setting('custom-preserved') == {'value': 42}
        assert [tuple(row) for row in upgraded._conn.execute('SELECT * FROM service_history')] == history
        for source in SOURCES:
            rows, page = listing(upgraded, source, 'all')
            assert len(rows) == 1 and rows[0]['lifecycle'] == 'unknown'
            assert rows[0]['catalogue_imported']
            assert not page['snapshots']
        assert upgraded._conn.execute('SELECT first_seen FROM source_catalogue').fetchone()[0] is None
        upgraded.close()
    upgraded = Database(path)
    for source in SOURCES:
        snapshot(upgraded, source, ['one'])
        assert listing(upgraded, source)[1]['total'] == 1
    upgraded.close()


@pytest.mark.parametrize('source', SOURCES)
def test_retention_protects_pending_and_recent_missing(db, source):
    snapshot(db, source, ['old', 'pending', 'recent', 'present'])
    snapshot(db, source, ['present'])
    snapshot(db, source, ['present'])
    db._conn.execute("UPDATE source_catalogue SET last_seen='2000-01-01T00:00:00+00:00' WHERE item_id IN ('old','pending')")
    db._conn.commit()
    db.add_service_history(source, 'pending', 'Pending', '', transmit_status='deferred')
    clean(db, DEFAULTS)
    rows, page = listing(db, source, 'all')
    ids = {row.get('alert_id') or row.get('incident_id') or row.get('item_id') for row in rows}
    assert ids == {'pending', 'recent', 'present'}


def client_for(db):
    app = FastAPI()
    for router in (web_router, rfs_router, traffic_router):
        app.include_router(router)
    app.state.db = db
    app.state.tx = SimpleNamespace(message_budget=126)
    app.state.poller = SimpleNamespace(status=SimpleNamespace(last_poll_result='error: test feed unavailable', last_poll_time=STAMP))
    app.state.rfs_poller = app.state.traffic_poller = SimpleNamespace(last_result='error: test feed unavailable', last_poll=STAMP)
    return TestClient(app)


@pytest.mark.parametrize('source', SOURCES)
def test_pages_share_views_filtered_counts_and_read_only_behaviour(db, source):
    snapshot(db, source, [f'item-{i:03}' for i in range(115)])
    snapshot(db, source, [f'item-{i:03}' for i in range(55)])
    db.set_setting(source + '_enabled', True)
    # Freeze lifecycle time only. No network client or radio exists in this app.
    original = db.current_listing
    with patch.object(db, 'current_listing', side_effect=lambda *a, **kw: original(*a, **kw, now=NOW)):
        client = client_for(db)
        before = db._conn.total_changes
        first = client.get('/' + source)
        assert first.status_code == 200
        assert 'Current (55)' in first.text and 'Past (60)' in first.text and 'All saved (115)' in first.text
        assert 'Showing 1–50 of 55 items' in first.text
        assert 'view=current&amp;page=2' in first.text
        assert 'test feed unavailable' in first.text
        past = client.get('/' + source + '?view=past&page=999')
        assert past.status_code == 200
        assert 'Showing 51–60 of 60 items' in past.text
        assert 'No longer in feed' in past.text
        assert 'view=past&amp;page=1' in past.text
        assert client.get('/' + source + '?view=invalid').status_code == 422
        assert client.get('/' + source + '?page=0').status_code == 422
        assert db._conn.total_changes == before
        db.set_setting(source + '_enabled', False)
        disabled = client.get('/' + source)
        assert 'Disabled — showing saved observations' in disabled.text
        assert 'Current (55)' in disabled.text


@pytest.mark.parametrize('source', SOURCES)
def test_empty_states_do_not_claim_safe_conditions(db, source):
    client = client_for(db)
    db.set_setting(source + '_enabled', True)
    assert 'No successful fetch has been recorded' in client.get('/' + source).text
    snapshot(db, source, [])
    assert 'No current items' in client.get('/' + source).text
    assert 'does not confirm conditions are safe' in client.get('/' + source).text
    db.set_setting(source + '_enabled', False)
    assert 'Monitoring is disabled' in client.get('/' + source).text


@pytest.mark.parametrize('source', SOURCES)
def test_pagination_is_stable_and_filters_before_counting(db, source):
    snapshot(db, source, [f'item-{i:04}' for i in range(700)])
    snapshot(db, source, [f'item-{i:04}' for i in range(511)])
    collected = []
    for page in range(1, 12):
        rows, pagination = listing(db, source, page=page)
        assert pagination['total'] == 511
        collected.extend(row.get('alert_id') or row.get('incident_id') or row.get('item_id') for row in rows)
    assert len(set(collected)) == len(collected) == 511
    assert listing(db, source, page=999)[1]['page'] == 11
    assert listing(db, source, 'past')[1]['total'] == 189


@pytest.mark.asyncio
@pytest.mark.parametrize('source', SOURCES)
async def test_real_failed_poll_does_not_change_catalogue(db, source, monkeypatch):
    from unittest.mock import AsyncMock
    from app.bom import BOMError
    from app.rfs.feed import RFSFeedError
    from app.traffic.feed import TrafficFeedError
    from app.poller import BomPoller
    from app.rfs.poller import RFSPoller
    from app.traffic.poller import TrafficPoller
    snapshot(db, source, ['one'])
    before = listing(db, source, 'all')
    db.set_setting(source + '_enabled', True)
    db.set_setting(source + '_all_councils', True)
    radio = SimpleNamespace(message_budget=126)
    if source == 'bom':
        monkeypatch.setattr('app.poller.BOMClient', lambda: SimpleNamespace(fetch_active=AsyncMock(side_effect=BOMError('failed feed'))))
        poller = BomPoller(db, radio)
    elif source == 'rfs':
        poller = RFSPoller(db, radio, SimpleNamespace(fetch=AsyncMock(side_effect=RFSFeedError('failed feed'))))
    else:
        poller = TrafficPoller(db, radio, SimpleNamespace(fetch=AsyncMock(side_effect=TrafficFeedError('failed feed'))))
    await poller.poll_once()
    assert listing(db, source, 'all') == before


@pytest.mark.asyncio
@pytest.mark.parametrize('source', ('rfs', 'traffic'))
async def test_first_missing_poll_hides_item_second_preserves_history_rule(db, source):
    from unittest.mock import AsyncMock
    from app.rfs.poller import RFSPoller
    from app.traffic.poller import TrafficPoller
    snapshot(db, source, ['one'])
    db.add_service_history(source, 'one', 'Previously sent', '', transmit_status='success')
    db.set_setting(source + '_enabled', True)
    db.set_setting(source + '_all_councils', True)
    if source == 'traffic':
        db.traffic_mark_sent('one', 'previous-revision')
    radio = SimpleNamespace(message_budget=126)
    client = SimpleNamespace(fetch=AsyncMock(return_value=[]))
    poller = RFSPoller(db, radio, client) if source == 'rfs' else TrafficPoller(db, radio, client)
    if source == 'traffic':
        poller._councils = []
    await poller.poll_once()
    assert listing(db, source)[1]['total'] == 0
    assert db.latest_service_history(source, 'one')['disposition'] != 'absent-from-feed'
    await poller.poll_once()
    assert db.latest_service_history(source, 'one')['disposition'] == 'absent-from-feed'


def test_scheduled_traffic_notice_remains_current_between_work_periods(db):
    from app.traffic.schedule import ClosurePeriod, window_state
    period = ClosurePeriod(from_day='Monday', to_day='Monday', start_time='01:00', finish_time='02:00', timezone='Australia/Sydney')
    snapshot(db, 'traffic', ['roadwork:one'], feeds={'roadwork'}, changes={'roadwork:one': {'periods': (period,)}})
    assert listing(db, 'traffic')[1]['total'] == 1
    assert window_state((period,), NOW)[0] != 'active'


@pytest.mark.parametrize('source', SOURCES)
def test_disabled_poll_never_changes_saved_snapshot(db, source):
    import asyncio
    from app.poller import BomPoller
    from app.rfs.poller import RFSPoller
    from app.traffic.poller import TrafficPoller
    snapshot(db, source, ['one'])
    before = listing(db, source, 'all')
    db.set_setting(source + '_enabled', False)
    poller = {'bom': BomPoller, 'rfs': RFSPoller, 'traffic': TrafficPoller}[source](db, SimpleNamespace(message_budget=126))
    asyncio.run(poller.poll_once())
    assert listing(db, source, 'all') == before


@pytest.mark.parametrize('source', SOURCES)
def test_page_queries_do_not_grow_with_displayed_row_count(db, source):
    snapshot(db, source, [f'item-{i}' for i in range(55)])
    client = client_for(db)
    selects = []
    def collect(sql):
        if sql.lstrip().upper().startswith('SELECT'):
            selects.append(sql)
    db._conn.set_trace_callback(collect)
    try:
        assert client.get('/' + source + '?page=1').status_code == 200
        first = len(selects)
        selects.clear()
        assert client.get('/' + source + '?page=2').status_code == 200
        assert len(selects) == first
        assert first < 25
    finally:
        db._conn.set_trace_callback(None)


def test_shared_asset_links_respect_root_path_without_named_asset_routes(db):
    from fastapi import Request
    from app.web.routes import render
    app = FastAPI()
    app.state.db, app.state.tx = db, SimpleNamespace(message_budget=126)
    @app.get('/test')
    def page(request: Request):
        return render(request, 'settings_home.html')
    page = TestClient(app, root_path='/noticeecho').get('/test')
    assert page.status_code == 200
    assert '/noticeecho/static/favicon.png' in page.text
    assert '/noticeecho/static/noticeecho-logo.png' in page.text


@pytest.mark.parametrize('source', SOURCES)
def test_successful_empty_snapshot_confirms_imported_absence(db, source):
    snapshot(db, source, ['one'])
    db._conn.execute('UPDATE source_catalogue SET present=NULL,imported=1')
    db._conn.commit()
    snapshot(db, source, [])
    row = listing(db, source, 'past')[0][0]
    assert row['lifecycle'] == 'absent'
    assert not row['catalogue_imported']


def test_rfs_orders_current_incidents_by_severity(db):
    snapshot(db, 'rfs', ['advice', 'watch', 'emergency'], changes={
        'watch': {'level': 'Watch and Act'}, 'emergency': {'level': 'Emergency Warning'}})
    assert [row['name'] for row in listing(db, 'rfs')[0]] == ['emergency', 'watch', 'advice']

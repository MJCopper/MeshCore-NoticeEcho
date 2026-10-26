"""Warning episodes are independent of provider URL revisions and delivery mode."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.bom_episodes import destination, full_cancellation, product_key, suffix
from app.bom_enricher import WarningSection
from app.db import Database
from app.filters import FilterRules
from app.models import Alert
from app.poller import BomPoller
from app.transmission import TxResult
from app.brief import brief_bom_parts
from app.maintenance import clean, DEFAULTS

PRODUCT = 'http://reg.bom.gov.au/products/IDN21033.shtml'
NOW = datetime(2026, 10, 26, 0, tzinfo=timezone.utc).timestamp()


def warning(**changes):
    value = dict(id=PRODUCT, region='NSW', event='Severe Thunderstorm Warning',
                 headline='Severe Thunderstorm Warning for Hunter', area_desc='Hunter',
                 effective='', expires='', message_type='Alert', references=[], detail='Damaging winds expected.')
    return value | changes


def alert(**changes):
    return Alert.from_bom(warning(**changes))


@pytest.fixture
def db():
    value = Database(':memory:')
    yield value
    value.close()


class Radio:
    message_budget = 126
    supports_notice_guards = True
    def __init__(self):
        self.notices = []
        self.admit = True
    def enqueue_notice(self, entries, on_result=None, priority=1, valid_if=None):
        if not self.admit:
            return False
        self.notices.append(dict(entries=entries, callback=on_result, valid=valid_if))
        return True
    def finish(self, outcome=True, error=''):
        notice = self.notices[-1]
        for index in range(len(notice['entries'])):
            notice['callback'](index, outcome, error)


async def process(db, radio, raw=None, *, dry=True, force=False, observe=True):
    db.set_setting('dry_run', dry)
    settings = db.all_settings()
    return await BomPoller(db, radio)._process(raw or warning(), FilterRules.from_settings(settings),
                                              'Australia/Sydney', int(settings.get('meshcore_channel', 0)), dry,
                                              settings=settings, force=force, observe=observe)


def latest(db):
    return db.latest_history(PRODUCT)


def metadata(db):
    return json.loads(latest(db)['metadata'])['bom_episode']


def test_product_aliases_are_conservative():
    assert product_key(PRODUCT) == product_key('https://www.bom.gov.au/products/IDN21033.shtml')
    assert product_key(PRODUCT) != product_key('https://other.example/products/IDN21033.shtml')
    assert product_key('http://reg.bom.gov.au/nsw/warnings/marinewind.shtml') == product_key('https://www.bom.gov.au/nsw/warnings/marinewind.shtml')


@pytest.mark.asyncio
async def test_changed_known_channel_key_is_a_new_audience_without_new_episode(db):
    radio = Radio()
    db.set_setting('meshcore_channels_target', {'conn': 'serial', 'target': ''})
    db.set_setting('meshcore_channels', [{'index': 0, 'name': 'Weather', 'identity': 'first-key-hash'}])
    await process(db, radio)
    original = metadata(db)
    db.set_setting('meshcore_channels', [{'index': 0, 'name': 'Renamed', 'identity': 'first-key-hash'}])
    assert not await process(db, radio)
    db.set_setting('meshcore_channels', [{'index': 0, 'name': 'Renamed', 'identity': 'second-key-hash'}])
    assert await process(db, radio)
    assert metadata(db)['action'] == 'NEW'
    assert metadata(db)['reference'] == original['reference']
    assert metadata(db)['destination'] != original['destination']
    settings = db.all_settings()
    settings['meshcore_port'] = '/dev/other-radio'
    with_stale_cache = destination(settings, 0)
    settings['meshcore_channels'] = []
    assert destination(settings, 0) == with_stale_cache


@pytest.mark.parametrize('number,expected', [(1,'A'),(26,'Z'),(27,'AA'),(52,'AZ'),(53,'BA'),(703,'AAA')])
def test_suffix_rollover(number, expected):
    assert suffix(number) == expected


def test_lifecycle_new_update_partial_full_cancel_and_new_episode(db):
    store = db.bom_episodes
    first = store.observe(alert(), now=NOW)
    assert first['reference'] == '2331-A'
    changed = store.observe(alert(headline='Updated warning'), now=NOW+1)
    assert changed['id'] == first['id']
    partial = alert()
    partial.warning_sections = (WarningSection('Severe Thunderstorm Warning', 'Hunter', 'NEW'), WarningSection('Cancellation', 'Illawarra', 'CAN'))
    assert not full_cancellation(partial)
    assert store.observe(partial, now=NOW+2)['state'] == 'open'
    closed = store.observe(alert(message_type='Cancel'), now=NOW+3)
    assert closed['reference'] == first['reference'] and closed['closure_reason'] == 'cancelled'
    assert store.observe(alert(message_type='Cancel'), now=NOW+4)['id'] == first['id']
    second = store.observe(alert(), now=NOW+5)
    assert second['reference'] == '2331-B'


def test_expiry_extension_missing_and_duplicate_snapshot_rules(db):
    store = db.bom_episodes
    expires = lambda t: datetime.fromtimestamp(t, timezone.utc).isoformat()
    first = store.observe(alert(expires=expires(NOW+10)), now=NOW)
    assert store.observe(alert(expires=expires(NOW+20)), now=NOW+5)['id'] == first['id']
    assert store.observe(alert(expires=expires(NOW+40)), now=NOW+20)['reference'] == '2331-B'
    store.snapshot([], 'missing-1', now=NOW+21)
    store.snapshot([], 'missing-1', now=NOW+22)
    assert store.lookup(alert())['state'] == 'open'
    assert store.observe(alert(), now=NOW+23)['reference'] == '2331-B'
    store.snapshot([], 'missing-2', now=NOW+24)
    store.snapshot([], 'missing-3', now=NOW+25)
    assert store.lookup(alert())['closure_reason'] == 'no longer observed'
    assert store.observe(alert(), now=NOW+26)['reference'] == '2331-C'


@pytest.mark.parametrize('expiry', ['', 'not a date', '2026-10-25T00:00:00'])
def test_unknown_expiry_does_not_close_episode(db, expiry):
    one = db.bom_episodes.observe(alert(expires=expiry), now=NOW)
    two = db.bom_episodes.observe(alert(expires=expiry), now=NOW+86400)
    assert one['id'] == two['id']


def test_unmatched_and_ambiguous_cancellations_do_not_allocate_or_close(db):
    store = db.bom_episodes
    assert store.observe(alert(message_type='Cancel'), now=NOW) is None
    assert db._conn.execute('SELECT COUNT(*) FROM bom_products').fetchone()[0] == 0
    first = store.observe(alert(), now=NOW)
    other = PRODUCT.replace('IDN21033', 'IDN21034')
    store.observe(alert(id=other), now=NOW)
    ambiguous = alert(id='unmatched-cancel', message_type='Cancel', references=[PRODUCT, other])
    assert store.observe(ambiguous, now=NOW+1) is None
    matched = alert(id='referenced-cancel', message_type='Cancel', references=[PRODUCT])
    assert store.observe(matched, now=NOW+1)['id'] == first['id']
    assert store.lookup(alert(id=other))['state'] == 'open'


def test_hash_collision_extends_product_prefix(db):
    class Digest:
        def __init__(self, value): self.value = value
        def hexdigest(self): return '2331' + ('A' if self.value == PRODUCT.encode() else 'B') + '0'*59
    with patch('app.bom_episodes.hashlib.sha256', side_effect=lambda value: Digest(value)):
        first = db.bom_episodes.observe(alert(), now=NOW)
        second = db.bom_episodes.observe(alert(id='another-product'), now=NOW)
    assert first['reference'] == '2331-A'
    assert second['reference'] == '2331B-A'


def test_concurrent_allocation_never_duplicates_episode(tmp_path):
    path = str(tmp_path/'concurrent.db')
    Database(path).close()
    def allocate(_):
        db = Database(path)
        try: return db.bom_episodes.observe(alert(), now=NOW)['reference']
        finally: db.close()
    with ThreadPoolExecutor(max_workers=4) as pool:
        refs = list(pool.map(allocate, range(8)))
    assert refs == ['2331-A']*8


@pytest.mark.asyncio
async def test_dry_run_new_update_cancel_new_and_no_false_live_delivery(db):
    radio = Radio()
    assert await process(db, radio)
    assert latest(db)['disposition'] == 'sent'
    assert metadata(db)['action'] == 'NEW'
    assert '#2331-A' in latest(db)['transmitted_text']
    count = db._conn.execute('SELECT COUNT(*) FROM service_history').fetchone()[0]
    assert not await process(db, radio)
    assert db._conn.execute('SELECT COUNT(*) FROM service_history').fetchone()[0] == count
    assert await process(db, radio, warning(headline='Changed storm'))
    assert metadata(db)['action'] == 'UPDATE'
    assert await process(db, radio, warning(message_type='Cancel'))
    assert metadata(db)['action'] == 'CANCELLED' and metadata(db)['reference'] == '2331-A'
    assert await process(db, radio)
    assert metadata(db)['action'] == 'NEW' and metadata(db)['reference'] == '2331-B'
    assert not radio.notices
    assert db.get_state(PRODUCT) is None
    assert await process(db, radio, dry=False)
    assert metadata(db)['mode'] == 'live' and metadata(db)['action'] == 'NEW'
    assert len(radio.notices) == 1


@pytest.mark.asyncio
async def test_live_introduction_cancellation_and_changed_destination(db):
    radio = Radio()
    assert await process(db, radio, dry=False)
    assert not await process(db, radio, dry=False)
    assert not await process(db, radio, dry=False, force=True)  # already queued
    radio.finish()
    assert not await process(db, radio, dry=False)
    assert await process(db, radio, warning(headline='Changed'), dry=False)
    assert metadata(db)['action'] == 'UPDATE'
    radio.finish()
    assert await process(db, radio, warning(message_type='Cancel'), dry=False)
    assert metadata(db)['action'] == 'CANCELLED'
    radio.finish()
    assert not await process(db, radio, warning(message_type='Cancel'), dry=False)
    assert await process(db, radio, dry=False)
    assert metadata(db)['reference'] == '2331-B' and metadata(db)['action'] == 'NEW'
    old_guard = radio.notices[-1]['valid']
    db.set_setting('meshcore_channel', 2)
    assert not old_guard()
    assert await process(db, radio, dry=False)
    assert metadata(db)['action'] == 'NEW'
    assert metadata(db)['reference'] == '2331-B'


@pytest.mark.asyncio
async def test_deferral_pre_submission_failure_ambiguity_and_resend(db):
    radio = Radio();radio.admit = False
    assert not await process(db, radio, dry=False)
    assert latest(db)['transmit_status'] == 'deferred'
    assert not db.bom_episodes.announcement(db.bom_episodes.lookup(alert()), 'live', destination(db.all_settings(),0), latest(db)['revision_hash'])[1]
    original_text = latest(db)['transmitted_text']
    radio.admit = True
    assert await process(db, radio, dry=False)
    assert latest(db)['transmitted_text'] == original_text
    radio.finish(False, 'Not attempted: unavailable before submission')
    assert await process(db, radio, warning(headline='New revision'), dry=False)
    assert metadata(db)['action'] == 'NEW'
    radio.finish(TxResult('unconfirmed', submitted=True))
    assert await process(db, radio, warning(headline='Another revision'), dry=False)
    assert metadata(db)['action'] == 'UPDATE'
    radio.finish()
    assert await process(db, radio, warning(headline='Another revision'), dry=False, force=True)
    assert metadata(db)['action'] == 'RESEND'
    assert 'BOM RESEND #2331-A' in latest(db)['transmitted_text']
    radio.finish()
    assert not await process(db, radio, warning(headline='Another revision'), dry=False)


@pytest.mark.asyncio
async def test_closed_episode_invalidates_pending_active_notice(db):
    radio = Radio()
    await process(db, radio, dry=False)
    guard = radio.notices[-1]['valid']
    assert guard()
    db.bom_episodes.observe(alert(message_type='Cancel'))
    assert not guard()


@pytest.mark.asyncio
async def test_never_introduced_cancellation_is_inspectable_without_broadcast(db):
    radio = Radio()
    await process(db, radio)
    assert not await process(db, radio, warning(message_type='Cancel'), dry=False)
    assert not radio.notices
    assert latest(db)['disposition'] == 'filtered'


def test_single_and_multipart_reference_respects_byte_budget():
    for budget in (126, 140, 195):
        value = alert(event='Flood Warning', detail='Flooding.', area_desc='Hunter')
        parts = brief_bom_parts(value, 'Australia/Sydney', 'CANCELLED', budget, episode_reference='2331-AA')
        assert '#2331-AA' in parts[0]
        assert all(len(part.encode()) <= budget for part in parts)
    value = alert(detail='Damaging winds, heavy rain and flash flooding.', area_desc='Armidale, Tamworth, Walcha, Ebor, Woolgoolga, Wooli')
    parts = brief_bom_parts(value,'Australia/Sydney','UPDATE',126,episode_reference='2331-A')
    assert len(parts) >= 2 and parts[0].startswith('1/')
    assert sum('#2331-A' in part for part in parts) == 1


@pytest.mark.asyncio
async def test_restart_keeps_reference_and_partial_recovery_text(tmp_path):
    path = str(tmp_path/'restart.db');db = Database(path);radio = Radio()
    raw = warning(area_desc='Armidale, Tamworth, Walcha, Ebor, Woolgoolga, Wooli', detail='Damaging winds and flash flooding expected.')
    await process(db,radio,raw,dry=False)
    first = radio.notices[-1]
    assert len(first['entries']) >= 2
    first['callback'](0,True,'')
    first['callback'](1,False,'Not attempted: link unavailable')
    text = latest(db)['transmitted_text'];reference = metadata(db)['reference']
    db.close();db=Database(path);radio=Radio()
    try:
        assert await process(db,radio,raw,dry=False)
        assert latest(db)['transmitted_text'] == text
        assert metadata(db)['reference'] == reference
        assert len(radio.notices[-1]['entries']) == len(first['entries'])-1
    finally: db.close()


@pytest.mark.asyncio
async def test_upgrade_anchors_do_not_rewrite_or_resend_old_messages(tmp_path):
    path=str(tmp_path/'upgrade.db');db=Database(path)
    raw=warning()
    db.replace_bom_current([raw],{'NSW'},datetime.now(timezone.utc).isoformat())
    revision=Alert.from_bom(raw).revision_hash()
    # Use the exact normal pipeline's settings-dependent revision hash.
    await process(db,Radio(),raw)
    old=latest(db);old_text='1/2 BOM UPDATE #2331 old recorded text || 2/2 retained text'
    db._conn.execute('UPDATE service_history SET transmitted_text=? WHERE id=?',(old_text,old['id']))
    db._conn.execute('DROP TRIGGER bom_announcement_delivery')
    for table in ('bom_announcements','bom_episodes','bom_product_aliases','bom_products'):
        db._conn.execute('DROP TABLE '+table)
    db._conn.execute("DELETE FROM history_migrations WHERE name='bom_episodes_v1'")
    db._conn.commit();before=[tuple(row) for row in db._conn.execute('SELECT * FROM service_history')];db.close()
    db=Database(path)
    try:
        assert [tuple(row) for row in db._conn.execute('SELECT * FROM service_history')] == before
        assert not await process(db,Radio(),raw)
        assert latest(db)['transmitted_text'] == old_text
        assert await process(db,Radio(),warning(headline='Changed after upgrade'))
        assert metadata(db)['action'] == 'NEW'
    finally:db.close()


def test_announcement_and_history_reservation_are_atomic_and_concurrent(db):
    from app.bom_episodes import AnnouncementExists
    episode = db.bom_episodes.observe(alert())
    context = dict(episode=episode, mode='live', audience='destination', revision='one', action='NEW')
    def reserve(index):
        try:
            return db.add_history(PRODUCT,'Storm','Hunter','sent',f'Prepared {index}',transmit_status='queued',
                                  bom_announcement=context)
        except AnnouncementExists:
            return None
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(reserve, range(8)))
    assert sum(result is not None for result in results) == 1
    assert db._conn.execute('SELECT COUNT(*) FROM service_history').fetchone()[0] == 1
    assert db._conn.execute('SELECT COUNT(*) FROM bom_announcements').fetchone()[0] == 1
    # Explicit resends also cannot duplicate an in-flight reservation.
    with pytest.raises(AnnouncementExists):
        db.add_history(PRODUCT,'Storm','Hunter','update','Duplicate resend',transmit_status='queued',
                       bom_announcement=context | {'force':True})
    assert db._conn.execute('SELECT COUNT(*) FROM service_history').fetchone()[0] == 1


def test_submission_rejection_and_late_repeat_update_introduction_evidence(db):
    episode = db.bom_episodes.observe(alert())
    row = db.add_history(PRODUCT,'Storm','Hunter','sent','Prepared',transmit_status='queued')
    store = db.bom_episodes
    store.register(episode,'live','target','rev','NEW',row)
    assert not store.announcement(episode,'live','target','rev')[1]
    def parts(value):
        db._conn.execute('UPDATE service_history SET delivery_parts=? WHERE id=?',(json.dumps(value),row))
        db._conn.commit()
    parts([{'status':'submitting'}])
    assert store.announcement(episode,'live','target','rev')[1]
    parts([{'status':'rejected'}])
    assert not store.announcement(episode,'live','target','rev')[1]
    db.record_delivery_part(row,0,1,TxResult('unconfirmed',submitted=True))
    assert store.announcement(episode,'live','target','rev')[1]
    db.record_delivery_part(row,0,1,TxResult('repeat_confirmed',submitted=True))
    assert store.announcement(episode,'live','target','rev')[1]
    assert db._conn.execute('SELECT COUNT(*) FROM bom_announcements').fetchone()[0] == 1


@pytest.mark.asyncio
async def test_previews_preserve_production_identity_and_use_target_mode(db):
    from fastapi import FastAPI
    from app.troubleshooting import Troubleshooting
    radio = Radio()
    raw = warning()
    await process(db,radio,raw,dry=False)
    radio.finish()
    db.replace_bom_current([raw],{'NSW'},datetime.now(timezone.utc).isoformat())
    app=FastAPI();app.state.db=db;app.state.tx=radio;app.state.poller=BomPoller(db,radio)
    service=Troubleshooting(app)
    tables=('bom_products','bom_product_aliases','bom_episodes','bom_announcements','service_history','bom_current')
    before={table:[tuple(row) for row in db._conn.execute('SELECT * FROM '+table)] for table in tables}
    episode_before=before['bom_episodes']
    preview=await service.preview('bom','reprocess')
    assert preview['notices']==1 and not preview['errors']
    assert 'BOM UPDATE #2331-A' in preview['details'][0]['text']
    assert {table:[tuple(row) for row in db._conn.execute('SELECT * FROM '+table)] for table in tables}==before
    service.start(preview['token']);await asyncio.wait_for(service.task, 3)
    assert len(radio.notices)==1  # ordinary reprocess preserves completed suppression
    resend=await service.preview('bom','resend')
    assert 'BOM RESEND #2331-A' in resend['details'][0]['text']
    service.start(resend['token'])
    for _ in range(100):
        if len(radio.notices) == 2: break
        await asyncio.sleep(.01)
    radio.finish()
    await asyncio.wait_for(service.task, 3)
    assert len(radio.notices)==2
    assert [tuple(row) for row in db._conn.execute('SELECT * FROM bom_episodes')]==episode_before
    assert metadata(db)['mode']=='live'
    radio.finish()


@pytest.mark.asyncio
async def test_episode_change_invalidates_reviewed_preview(db):
    from fastapi import FastAPI
    from app.troubleshooting import Troubleshooting
    radio=Radio();raw=warning()
    db.replace_bom_current([raw],{'NSW'},datetime.now(timezone.utc).isoformat())
    app=FastAPI();app.state.db=db;app.state.tx=radio;app.state.poller=BomPoller(db,radio)
    service=Troubleshooting(app)
    preview=await service.preview('bom','resend')
    db.bom_episodes.observe(alert(message_type='Cancel'))
    with pytest.raises(ValueError,match='changed'):
        service.start(preview['token'])


@pytest.mark.asyncio
async def test_unknown_cancellation_in_other_mode_never_consumes_live_introduction(db):
    radio=Radio()
    await process(db,radio)
    assert not await process(db,radio,warning(message_type='Cancel'),dry=False)
    assert not radio.notices
    await process(db,radio,dry=False)
    assert metadata(db)['action']=='NEW'
    assert metadata(db)['reference']=='2331-B'


@pytest.mark.asyncio
async def test_scoped_cancellation_does_not_close_global_episode(db):
    radio=Radio()
    db.set_setting('bom_all_councils',False)
    db.set_setting('bom_councils',[])
    db.set_setting('bom_districts',['Hunter'])
    db.set_setting('bom_include_unknown_councils',False)
    raw=warning(event='Flood Warning',area_desc='Hunter',headline='Flood Warning')
    await process(db,radio,raw)
    raw=warning(event='Flood Warning',area_desc='Illawarra',headline='Flood Warning',
                detail='Flooding in Illawarra. Flooding is no longer occurring in the Hunter district and the warning for this district is CANCELLED.')
    await process(db,radio,raw)
    assert metadata(db)['action']=='CANCELLED'
    assert metadata(db)['scope']=='selected areas'
    assert db.bom_episodes.lookup(Alert.from_bom(raw))['state']=='open'
    assert metadata(db)['reference']=='2331-A'
    assert await process(db, radio, raw, force=True)
    assert metadata(db)['action'] == 'RESEND'
    assert metadata(db)['scope'] == 'selected areas'
    assert 'BOM RESEND #2331-A' in latest(db)['transmitted_text']
    assert 'CANCELLED' in latest(db)['transmitted_text']
    assert not await process(db, radio, raw)
    raw=warning(event='Flood Warning',area_desc='Hunter',headline='Flood Warning',detail='Renewed flooding in Hunter.')
    await process(db,radio,raw)
    assert metadata(db)['action']=='UPDATE'
    assert metadata(db)['reference']=='2331-A'


@pytest.mark.asyncio
@pytest.mark.parametrize('dry', [True, False])
async def test_explicit_cancellation_resend_preserves_cancelled_phase(db, dry):
    radio = Radio()
    await process(db, radio, dry=dry)
    if not dry:
        radio.finish()
    raw = warning(message_type='Cancel')
    await process(db, radio, raw, dry=dry)
    if not dry:
        radio.finish()
    assert await process(db, radio, raw, dry=dry, force=True)
    assert metadata(db)['action'] == 'RESEND'
    assert metadata(db)['scope'] == 'full warning'
    assert 'BOM RESEND #2331-A' in latest(db)['transmitted_text']
    assert 'CANCELLED' in latest(db)['transmitted_text']
    assert db.bom_episodes.lookup(alert())['state'] == 'closed'
    if not dry:
        radio.finish()
    assert not await process(db, radio, raw, dry=dry)


@pytest.mark.asyncio
async def test_settings_and_target_mode_changes_do_not_split_episode(db):
    radio=Radio()
    await process(db,radio)
    reference=metadata(db)['reference']
    db.set_setting('display_timezone','Australia/Perth')
    assert not await process(db,radio)
    db.set_setting('meshcore_host','another-host:4000')
    db.set_setting('meshcore_conn','tcp')
    await process(db,radio)
    assert metadata(db)['action']=='NEW' and metadata(db)['reference']==reference
    await process(db,radio,dry=False)
    assert metadata(db)['action']=='NEW' and metadata(db)['reference']==reference
    assert db._conn.execute('SELECT COUNT(*) FROM bom_episodes').fetchone()[0]==1


def test_retention_keeps_reference_counters_and_pending_announcements(db):
    store=db.bom_episodes;episode=store.observe(alert())
    for revision,status in [('done','success'),('pending','deferred')]:
        history=db.add_history(PRODUCT,'Storm','Hunter','sent','Recorded',transmit_status=status)
        store.register(episode,'live','target',revision,'NEW',history)
    store.observe(alert(message_type='Cancel'))
    db._conn.execute("UPDATE bom_announcements SET created='2000-01-01T00:00:00+00:00'")
    db._conn.commit()
    clean(db,DEFAULTS)
    assert [row['state'] for row in db._conn.execute('SELECT * FROM bom_announcements')]==['deferred']
    assert store.observe(alert())['reference']=='2331-B'
    assert db._conn.execute('SELECT COUNT(*) FROM bom_products').fetchone()[0]==1


@pytest.mark.asyncio
async def test_unicode_and_smaller_recovery_budget_never_mix_recorded_parts(db):
    radio=Radio()
    raw=warning(area_desc='Armidale, Tamworth, Walcha, Ebor, Woolgoolga, Wooli',detail='Damaging winds and flash flooding expected.')
    await process(db,radio,raw,dry=False)
    text=latest(db)['transmitted_text']
    radio.notices[-1]['callback'](0,True,'')
    for index in range(1,len(radio.notices[-1]['entries'])):
        radio.notices[-1]['callback'](index,False,'Not attempted: link unavailable')
    radio.message_budget=60
    before=len(radio.notices)
    assert not await process(db,radio,raw,dry=False)
    assert len(radio.notices)==before and latest(db)['transmitted_text']==text
    radio.message_budget=126
    assert await process(db,radio,raw,dry=False)
    value=alert(detail='Heavy rain expected.',area_desc='Grafton, Coffs Harbour, Café Creek')
    parts=brief_bom_parts(value,'Australia/Sydney','NEW',126,episode_reference='2331-AA')
    assert all(len(part.encode('utf-8'))<=126 for part in parts)
    assert 'Café Creek' in ' '.join(parts)


@pytest.mark.asyncio
async def test_partial_explicit_resend_is_recovered_without_new_resend_or_new_text(db):
    radio=Radio()
    raw=warning(area_desc='Armidale, Tamworth, Walcha, Ebor, Woolgoolga, Wooli',detail='Damaging winds and flash flooding expected.')
    await process(db,radio,raw,dry=False);radio.finish()
    await process(db,radio,raw,dry=False,force=True)
    resend_id=latest(db)['id'];text=latest(db)['transmitted_text']
    notice=radio.notices[-1]
    assert len(notice['entries'])>=2
    notice['callback'](0,True,'')
    for index in range(1,len(notice['entries'])):
        notice['callback'](index,False,'Not attempted: link unavailable')
    assert await process(db,radio,raw,dry=False)
    assert latest(db)['id']==resend_id and latest(db)['transmitted_text']==text
    assert len(radio.notices[-1]['entries'])==len(notice['entries'])-1
    assert 'BOM RESEND #2331-A' in text
    radio.finish()
    assert not await process(db,radio,raw,dry=False)


@pytest.mark.asyncio
async def test_history_and_transmit_log_render_episode_actions(db):
    from test_source_catalogue import client_for
    radio=Radio()
    await process(db,radio)
    await process(db,radio,warning(headline='Changed warning'))
    await process(db,radio,warning(message_type='Cancel'))
    client=client_for(db)
    page=client.get('/history?source=bom')
    assert page.status_code==200
    for action in ('NEW','UPDATE','CANCELLED'):
        assert action+' #2331-A' in page.text
    for text in latest(db)['transmitted_text'].split(' || '):
        db.add_transmit_log(0,len(text.encode()),True,text)
    log=client.get('/transmit-log')
    assert log.status_code==200 and 'BOM CANCELLED #2331-A' in log.text


def test_full_cancelled_sections_close_episode_and_replay_lookup_does_not_mutate(db):
    first=db.bom_episodes.observe(alert(),now=NOW)
    cancelled=alert()
    cancelled.warning_sections=(WarningSection('Cancellation','Hunter','CAN'),WarningSection('Cancellation','Illawarra','CAN'))
    closed=db.bom_episodes.observe(cancelled,now=NOW+1)
    assert closed['id']==first['id'] and closed['state']=='closed'
    before=db._conn.total_changes
    assert db.bom_episodes.lookup(cancelled)['id']==first['id']
    assert db._conn.total_changes==before
    assert db.bom_episodes.observe(alert(),now=NOW+2)['reference']=='2331-B'


def test_episode_bootstrap_failure_rolls_back_without_consuming_suffix(tmp_path):
    path=str(tmp_path/'interrupted-upgrade.db');db=Database(path)
    db.replace_bom_current([warning()],{'NSW'},datetime.now(timezone.utc).isoformat())
    db._conn.execute("DELETE FROM history_migrations WHERE name='bom_episodes_v1'")
    for table in ('bom_announcements','bom_episodes','bom_product_aliases','bom_products'):
        db._conn.execute('DELETE FROM '+table)
    db._conn.commit()
    original=db.bom_episodes._resolve
    def fail(*args,**kwargs):
        original(*args,**kwargs)
        raise sqlite3.OperationalError('simulated upgrade failure')
    with patch.object(db.bom_episodes,'_resolve',side_effect=fail):
        with pytest.raises(sqlite3.OperationalError):db.bom_episodes.bootstrap()
    assert db._conn.execute('SELECT COUNT(*) FROM bom_products').fetchone()[0]==0
    assert not db._conn.execute("SELECT 1 FROM history_migrations WHERE name='bom_episodes_v1'").fetchone()
    db.bom_episodes.bootstrap()
    assert db.bom_episodes.lookup(alert())['reference']=='2331-A'
    db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('dry',[True,False])
async def test_issue_time_only_refresh_does_not_duplicate_material_content(db, dry):
    radio=Radio()
    raw=warning(effective='2026-10-26T00:00:00+00:00')
    await process(db,radio,raw,dry=dry)
    if not dry:radio.finish()
    total=db._conn.execute('SELECT COUNT(*) FROM bom_announcements').fetchone()[0]
    assert not await process(db,radio,raw | {'effective':'2026-10-26T01:00:00+00:00'},dry=dry)
    assert db._conn.execute('SELECT COUNT(*) FROM bom_announcements').fetchone()[0]==total
    await process(db,radio,raw | {'headline':'Changed hazard','effective':'2026-10-26T02:00:00+00:00'},dry=dry)
    assert metadata(db)['action']=='UPDATE'
    if not dry:radio.finish()
    # A real reversion of hazard content is a new revision, not an issue-only refresh.
    assert await process(db,radio,raw | {'effective':'2026-10-26T03:00:00+00:00'},dry=dry)
    assert metadata(db)['action']=='UPDATE'


@pytest.mark.asyncio
async def test_issue_only_refresh_recovers_original_partial_parts(db):
    radio=Radio()
    raw=warning(effective='2026-10-26T00:00:00+00:00',area_desc='Armidale, Tamworth, Walcha, Ebor, Woolgoolga, Wooli',detail='Damaging winds and flash flooding expected.')
    await process(db,radio,raw,dry=False)
    row_id=latest(db)['id'];text=latest(db)['transmitted_text'];notice=radio.notices[-1]
    notice['callback'](0,True,'')
    for index in range(1,len(notice['entries'])):
        notice['callback'](index,False,'Not attempted: link unavailable')
    assert await process(db,radio,raw | {'effective':'2026-10-26T01:00:00+00:00'},dry=False)
    assert latest(db)['id']==row_id and latest(db)['transmitted_text']==text
    assert len(radio.notices[-1]['entries'])==len(notice['entries'])-1


def test_concurrent_issue_only_revisions_cannot_both_reserve(db):
    from app.bom_episodes import AnnouncementExists
    episode=db.bom_episodes.observe(alert())
    def reserve(index):
        try:
            return db.add_history(PRODUCT,'Storm','Hunter','sent','Prepared',transmit_status='queued',
                                  bom_announcement=dict(episode=episode,mode='live',audience='target',revision=str(index),material_hash='same content',action='NEW'))
        except AnnouncementExists:return None
    with ThreadPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(reserve,range(8)))
    assert sum(value is not None for value in results)==1


@pytest.mark.asyncio
async def test_upgrade_does_not_assign_current_content_to_an_unrelated_legacy_revision(db):
    db.replace_bom_current([warning(headline='New content not announced')], {'NSW'}, datetime.now(timezone.utc).isoformat())
    db.add_history(PRODUCT, 'Storm', 'Hunter', 'sent', 'Old message',
                   transmit_status='dry-run', revision_hash='different-legacy-revision:coverage')
    db._conn.execute('DELETE FROM bom_announcements')
    db._conn.execute("DELETE FROM history_migrations WHERE name='bom_episodes_v1'")
    db._conn.commit()
    db.bom_episodes.bootstrap()
    assert db._conn.execute('SELECT material_hash FROM bom_announcements').fetchone()[0] == ''
    assert await process(db, Radio(), warning(headline='New content not announced'))
    assert metadata(db)['action'] == 'NEW'

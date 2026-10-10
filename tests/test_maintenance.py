"""Retention preserves delivery evidence; maintenance is independent and bounded."""
import asyncio
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
from unittest.mock import patch

import httpx
import pytest
from app.db import Database
from app.maintenance import Maintenance,DEFAULTS,backup,clean,storage,verify
from app.rfs.feed import Incident
from test_traffic import item
from test_troubleshooting import environment,rows,seed

OLD="2020-01-01T00:00:00+00:00"

@pytest.fixture
def db(tmp_path):
    db=Database(str(tmp_path/'maintenance.db'))
    yield db
    db.close()


def history(db,source='rfs',identity='one',status=None,old=True):
    i=db.add_service_history(source,identity,'Notice',transmit_status=status,revision_hash='revision',transmitted_text='Text' if status else '')
    if old:
        with db._lock:db._conn.execute('UPDATE service_history SET ts=? WHERE id=?',(OLD,i));db._conn.commit()
    return i


def sql(db,query,args=()):
    with db._lock:
        result=db._conn.execute(query,args).fetchall();db._conn.commit();return result


@pytest.mark.parametrize('source',['bom','rfs','traffic'])
def test_retention_preserves_latest_decision_broadcast_and_closure_anchor(db,source):
    obsolete=history(db,source,status=None)
    done=history(db,source,status='success')
    excluded=history(db,source,status=None)
    preview=history(db,source,status='dry-run')
    latest=history(db,source,status=None)
    clean(db,DEFAULTS)
    ids={r['id'] for r in sql(db,'SELECT * FROM service_history')}
    assert obsolete not in ids and excluded not in ids
    assert {done,preview,latest}<=ids
    assert db.latest_successful_broadcast(source,'one')['id']==done
    assert db.latest_service_history(source,'one')['id']==latest


@pytest.mark.parametrize('status',['queued','deferred','failed','interrupted'])
def test_pending_delivery_and_linked_attempts_never_expire(db,status):
    pending=history(db,status=status)
    history(db,status=None)
    attempt=db.begin_transmission(0,'Text','meshcore',False,context=(pending,0,1))
    sql(db,'UPDATE transmit_log SET ts=?,outcome=? WHERE id=?',(OLD,'rejected',attempt))
    clean(db,DEFAULTS)
    assert sql(db,'SELECT * FROM service_history WHERE id=?',(pending,))
    assert sql(db,'SELECT * FROM transmit_log WHERE id=?',(attempt,))


def test_unlinked_submitting_retry_pending_and_repeat_tracking_logs_are_protected(db):
    for outcome,evidence in [('submitting',{}),('unconfirmed',{'retry_pending':True}),('local_confirmed',{'repeat_expires_at':datetime.now(timezone.utc).timestamp()+3600}),('rejected',{})]:
        i=db.begin_transmission(0,'Text','meshcore',False)
        sql(db,'UPDATE transmit_log SET ts=?,outcome=?,evidence=? WHERE id=?',(OLD,outcome,json.dumps(evidence),i))
    clean(db,DEFAULTS)
    assert [r['outcome'] for r in sql(db,'SELECT outcome FROM transmit_log ORDER BY id')]==['submitting','unconfirmed','local_confirmed']


def test_unprotected_old_events_errors_and_history_are_deleted(db):
    for _ in range(3):
        history(db)
        db.add_event('INFO','Old');db.add_error('rfs','Old')
    sql(db,'UPDATE events SET ts=?',(OLD,));sql(db,'UPDATE errors SET ts=?',(OLD,))
    db.add_event('INFO','Recent');db.add_error('rfs','Recent')
    counts=clean(db,DEFAULTS)
    assert counts['service_history']==2
    assert counts['events']==3 and counts['errors']==3
    assert len(db.recent_events())==1 and len(db.recent_errors())==1


def test_recent_and_future_timestamp_records_remain(db):
    old=history(db);recent=history(db,old=False)
    future=history(db,old=False)
    sql(db,'UPDATE service_history SET ts=? WHERE id=?',('2099-01-01T00:00:00+00:00',future))
    clean(db,DEFAULTS)
    assert {r['id'] for r in sql(db,'SELECT id FROM service_history')}=={recent,future}


def test_only_confirmed_stale_current_rows_are_removed(db):
    for name,missing in [('gone',2),('unconfirmed',1),('pending',3),('fresh',2)]:
        incident=Incident(name,name,'Advice','Council','Road','Under control','Grass Fire','','')
        db.rfs_save_incident(incident,incident.revision)
        sql(db,'UPDATE rfs_incidents SET last_seen=?,missing_polls=? WHERE incident_id=?',(OLD if name!='fresh' else datetime.now(timezone.utc).isoformat(),missing,name))
    history(db,identity='pending',status='interrupted')
    clean(db,DEFAULTS)
    assert {r['incident_id'] for r in sql(db,'SELECT incident_id FROM rfs_incidents')}=={'unconfirmed','pending','fresh'}


def test_stale_traffic_pending_related_feed_is_protected(db):
    for identity in ('incident:1','incident:2'):
        event=item(item_id=identity)
        db.traffic_save_item(event,'Council',True)
        sql(db,'UPDATE traffic_items SET missing_polls=2,last_seen=? WHERE item_id=?',(OLD,identity))
    history(db,'traffic','regional:1','deferred')
    clean(db,DEFAULTS)
    assert db.traffic_get_item('incident:1') and db.traffic_get_item('incident:2') is None


def test_bom_expiry_unknown_expiry_and_delivery_state_protection(db):
    inputs=[{'id':name,'region':'NSW','event':'Warning','expires':expiry} for name,expiry in [('expired',OLD),('missing',''),('invalid','unknown'),('pending',OLD),('current','2099-01-01T00:00:00+00:00')]]
    db.replace_bom_current(inputs,{'NSW'},OLD)
    history(db,'bom','pending','queued')
    for name in ('expired','missing','pending'):
        db.upsert_state(name,'Warning','',OLD,'hash','sent',None)
    clean(db,DEFAULTS)
    assert {r['alert_id'] for r in sql(db,'SELECT alert_id FROM bom_current')}=={'missing','invalid','pending','current'}
    assert db.get_state('expired') is None
    assert db.get_state('missing') and db.get_state('pending')


def test_backup_verified_restore_and_rotation(db):
    db.set_setting('marker','saved')
    history(db,status='failed')
    snapshots=[backup(db,2) for _ in range(4)]
    paths=list(Path(db.path+'.backups').glob('*.db'))
    assert len(paths)==2 and Path(snapshots[-1]['path']) in paths
    target=Path(snapshots[-1]['path']);assert verify(target)['result']=='ok'
    with sqlite3.connect(target) as restored:
        assert json.loads(restored.execute("SELECT value FROM settings WHERE key='marker'").fetchone()[0])=='saved'
        assert restored.execute('SELECT transmit_status FROM service_history').fetchone()[0]=='failed'
    assert not list(target.parent.glob('*.partial'))


def test_bad_new_backup_does_not_rotate_old_good_backups(db):
    old=backup(db,1)
    with patch('app.maintenance.verify',side_effect=RuntimeError('bad snapshot')):
        with pytest.raises(RuntimeError,match='bad snapshot'):backup(db,1)
    assert Path(old['path']).exists()
    assert len(list(Path(db.path+'.backups').glob('*.db')))==1
    assert not list(Path(db.path+'.backups').glob('*.partial'))


@pytest.mark.asyncio
async def test_backup_failure_stops_retention_and_reports_error(db):
    history(db);history(db)
    manager=Maintenance(db)
    with patch('app.maintenance.backup',side_effect=OSError('disk full')):
        manager.trigger('maintenance');await manager.task
    assert manager.last['status']=='failed' and 'disk full' in manager.last['error']
    assert len(sql(db,'SELECT * FROM service_history'))==2
    assert db.recent_errors()[0]['source']=='database'


@pytest.mark.asyncio
async def test_scheduler_runs_without_any_enabled_services_and_stops_cleanly(db):
    for source in ('bom','rfs','traffic'):db.set_setting(source+'_enabled',False)
    manager=Maintenance(db);manager.start()
    for _ in range(100):
        if manager.task:break
        await asyncio.sleep(.01)
    await manager.close()
    assert manager.last['status']=='success' and manager.last_backup['status']=='verified'
    assert db.get_setting('maintenance_last_run')['status']=='success'
    resumed=Maintenance(db);resumed.start();await asyncio.sleep(.02);await resumed.close()
    assert resumed.task is None


@pytest.mark.asyncio
async def test_disabled_schedule_still_allows_explicit_check(db):
    db.set_setting('maintenance_enabled',False)
    manager=Maintenance(db);manager.start();await asyncio.sleep(.02)
    assert manager.task is None
    manager.trigger('deep-check');await manager.task
    assert manager.last_check['deep'] is True
    assert not db.get_setting('maintenance_last_run')
    await manager.close()


@pytest.mark.asyncio
async def test_worker_does_not_block_event_loop_and_prevents_overlap(db):
    manager=Maintenance(db);entered=threading.Event();release=threading.Event()
    original=manager._work
    def slow(action):
        entered.set();release.wait(5);return original(action)
    with patch.object(manager,'_work',slow):
        manager.trigger('check')
        for _ in range(100):
            if entered.is_set():break
            await asyncio.sleep(.005)
        assert entered.is_set() and manager.running
        with pytest.raises(RuntimeError):manager.trigger('backup')
        shutdown=asyncio.create_task(manager.close());await asyncio.sleep(.01)
        assert not shutdown.done()
        release.set();await shutdown
    assert not manager.running and manager.last['status']=='success'


@pytest.mark.asyncio
async def test_api_validation_atomic_settings_and_status_no_transmits(environment):
    app=environment
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        before=app.state.db.all_settings()
        assert (await client.post('/troubleshoot/database/settings',json=DEFAULTS|{'maintenance_event_days':0})).status_code==422
        assert app.state.db.all_settings()==before
        assert (await client.post('/troubleshoot/database/settings',json=DEFAULTS|{'extra':True})).status_code==422
        assert (await client.post('/troubleshoot/database/settings',json=DEFAULTS|{'maintenance_enabled':False,'maintenance_event_days':14})).status_code==200
        status=(await client.get('/troubleshoot/database')).json()
        assert status['settings']['maintenance_event_days']==14
        assert 'table_counts' in status['storage'] and 'protected_history' in status['storage']
        result=await client.post('/troubleshoot/database/action',json={'action':'check'})
        assert result.status_code==200
        assert (await client.post('/troubleshoot/database/action',json={'action':'backup'})).status_code==409
        await app.state.maintenance.task
        assert app.state.maintenance.last['status']=='success'
        assert not app.state.tx.sent
        assert (await client.post('/troubleshoot/database/action',json={'action':'vacuum'})).status_code==422


def test_legacy_tables_remain_untouched(db):
    sql(db,"INSERT INTO history(ts,alert_id) VALUES(?,?)",(OLD,'legacy'))
    clean(db,DEFAULTS)
    assert sql(db,'SELECT * FROM history')[0]['alert_id']=='legacy'


def test_batch_limit_and_idempotence(db):
    with db._lock:
        db._conn.executemany('INSERT INTO events(ts,level,message) VALUES(?,?,?)',[(OLD,'INFO','Old')]*5100);db._conn.commit()
    assert clean(db,DEFAULTS)['events']==5000
    assert clean(db,DEFAULTS)['events']==100
    assert clean(db,DEFAULTS)['events']==0


def test_offline_compaction_requires_confirmation_and_verified_backup(tmp_path):
    db=Database(str(tmp_path/'compact.db'))
    path=db.path
    db.add_event('INFO','Preserve me')
    refused=subprocess.run([sys.executable,'-m','app.maintenance','compact','--database',path],capture_output=True,text=True)
    assert refused.returncode==2 and 'Stop NoticeEcho' in refused.stderr
    db.close()
    result=subprocess.run([sys.executable,'-m','app.maintenance','compact','--database',path,'--confirm-stopped'],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    parsed=json.loads(result.stdout)
    assert parsed['backup']['status']=='verified' and parsed['check']['result']=='ok'
    with sqlite3.connect(path) as conn:assert conn.execute('SELECT message FROM events').fetchone()[0]=='Preserve me'
    # Fixture can close its connection again safely through Database.close.


def test_storage_reports_sizes_and_reusable_pages(db):
    info=storage(db)
    assert info['database_bytes']>0 and info['logical_bytes']>0
    assert info['free_disk_bytes']>0
    assert set(info['table_counts'])>= {'settings','service_history','transmit_log'}
    assert 0<=info['reusable_bytes']<=info['logical_bytes']


@pytest.mark.asyncio
async def test_cancelled_runner_waits_for_its_worker_before_close(db):
    manager=Maintenance(db);entered=threading.Event();release=threading.Event()
    original=manager._work
    def slow(action):
        entered.set();release.wait(5);return original(action)
    with patch.object(manager,'_work',slow):
        manager.trigger('check')
        while not entered.is_set():await asyncio.sleep(.005)
        manager.task.cancel();await asyncio.sleep(.01)
        assert not manager.task.done()
        release.set();await manager.close()
    assert manager.last['status']=='success'


def test_insufficient_backup_space_preserves_existing_snapshot(db):
    from types import SimpleNamespace
    good=backup(db,1)
    with patch('app.maintenance.shutil.disk_usage',return_value=SimpleNamespace(free=0)):
        with pytest.raises(RuntimeError,match='Insufficient disk'):backup(db,1)
    assert Path(good['path']).exists()


@pytest.mark.asyncio
async def test_readonly_recording_failure_is_reported_in_memory_without_blocking(db):
    manager=Maintenance(db)
    with patch.object(db,'add_event',side_effect=sqlite3.OperationalError('database is readonly')):
        manager.trigger('check');await manager.task
    assert not manager.running and manager.last['status']=='failed'
    assert 'unable to persist' in manager.last['error']


def test_unlinked_active_log_does_not_disable_history_retention(db):
    old=history(db);history(db)
    db.begin_transmission(0,'Manual','meshcore',True)
    clean(db,DEFAULTS)
    assert not sql(db,'SELECT id FROM service_history WHERE id=?',(old,))


def test_indirect_attempt_id_reference_preserves_legacy_transmit_log(db):
    attempt=db.begin_transmission(0,'Old','meshcore',False)
    sql(db,'UPDATE transmit_log SET ts=?,outcome=? WHERE id=?',(OLD,'rejected',attempt))
    record=history(db,status='interrupted')
    sql(db,'UPDATE service_history SET delivery_parts=? WHERE id=?',(json.dumps([{'status':'failed','attempt_ids':[attempt]}]),record))
    clean(db,DEFAULTS)
    assert sql(db,'SELECT id FROM transmit_log WHERE id=?',(attempt,))


@pytest.mark.asyncio
async def test_maintenance_reporting_does_not_invalidate_resend_preview(environment):
    app=environment;seed(app,'rfs')
    preview=await app.state.troubleshooting.preview('rfs','resend')
    manager=Maintenance(app.state.db)
    manager.trigger('check');await manager.task
    job=app.state.troubleshooting.start(preview['token']);await app.state.troubleshooting.task
    assert job['status']=='completed' and len(app.state.tx.sent)==1


def test_direct_history_prune_protects_delivery_anchors(db):
    obsolete=history(db);done=history(db,status='success');pending=history(db,status='interrupted');latest=history(db)
    assert db.prune_history()==1
    assert {r['id'] for r in sql(db,'SELECT id FROM service_history')}=={done,pending,latest}


@pytest.mark.asyncio
async def test_failed_schedule_uses_hourly_retry_even_if_persistence_fails(db):
    manager=Maintenance(db)
    with patch.object(db,'add_event',side_effect=sqlite3.OperationalError('readonly')):
        manager.trigger('maintenance');await manager.task
    assert manager.last_run['status']=='failed'
    manager.start();await asyncio.sleep(.02)
    original=manager.task
    await manager.close()
    assert manager.task is original


@pytest.mark.asyncio
async def test_shutdown_during_scheduler_settings_read_does_not_start_work(db):
    manager=Maintenance(db);entered=threading.Event();release=threading.Event()
    original=manager.settings
    def blocked():
        entered.set();release.wait(5);return original()
    with patch.object(manager,'settings',blocked):
        manager.start()
        while not entered.is_set():await asyncio.sleep(.005)
        shutdown=asyncio.create_task(manager.close());await asyncio.sleep(.01)
        release.set();await shutdown
    assert manager.task is None

"""Real file-backed WAL contention, task recovery and delivery preservation."""
import asyncio
from contextlib import contextmanager
import json
import sqlite3
import time
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.db import Database
from app.poller import BomPoller
from app.rfs.poller import RFSPoller
from app.traffic.poller import TrafficPoller
from app.rfs.feed import Incident
from app import maintenance, polling_runtime
from app.transmission import TxResult
from app.web.routes import router
from app.troubleshooting import diagnostics


@pytest.fixture
def db(tmp_path):
    value=Database(str(tmp_path/'locks.db'))
    value._conn.execute('PRAGMA busy_timeout=20')
    yield value
    value.close()


async def until(predicate, seconds=3):
    deadline=time.monotonic()+seconds
    while not predicate():
        assert time.monotonic()<deadline, 'Timed out waiting for worker state'
        await asyncio.sleep(.005)


def hold(db):
    conn=sqlite3.connect(db.path)
    conn.execute('BEGIN IMMEDIATE')
    return conn


@pytest.mark.asyncio
@pytest.mark.parametrize('source,cls',[('bom',BomPoller),('rfs',RFSPoller),('traffic',TrafficPoller)])
async def test_external_lock_does_not_kill_polling_and_recovers(db,monkeypatch,source,cls):
    monkeypatch.setattr(polling_runtime,'RETRY_DELAYS',(.01,.02,.03,.04))
    poller=cls(db,SimpleNamespace());success=[]
    async def poll():
        db.set_setting('poll_probe',source);success.append(True)
    poller.poll_once=poll
    other=hold(db)
    poller.start()
    try:
        await until(lambda:poller.worker.failures>=2)
        state=poller.worker.snapshot()
        assert state['alive'] and not state['ready'] and state['last_error']=='database is locked'
        assert not db._conn.in_transaction
        assert db.runtime_status()['lock_failures']>=2
        assert db.recent_errors()==[]  # No second failing write in error handling.
        other.rollback()
        await until(lambda:bool(success))
        assert poller.worker.snapshot()['ready'] and poller.worker.last_recovery
        assert not poller.worker.last_error
        assert db.runtime_status()['last_recovery']
    finally:
        other.close();await poller.stop()
    assert not poller.worker.snapshot()['alive']


@pytest.mark.asyncio
async def test_original_error_survives_when_error_logging_itself_is_locked(db,monkeypatch,caplog):
    monkeypatch.setattr(polling_runtime,'RETRY_DELAYS',(.01,))
    poller=RFSPoller(db,SimpleNamespace())
    poller.worker.recovered_queue=True
    async def failed():raise ValueError('original feed failure')
    poller.poll_once=failed;other=hold(db);poller.start()
    try:
        await until(lambda:poller.worker.failures>=2)
        assert 'original feed failure' in poller.worker.last_error
        assert 'Could not persist rfs error' in caplog.text
        assert poller.worker.snapshot()['alive']
    finally:other.rollback();other.close();await poller.stop()


def test_mid_write_abort_rolls_back_every_statement_and_releases_writer(db):
    db._conn.execute("CREATE TEMP TRIGGER fail_update BEFORE UPDATE OF normalized_data ON rfs_incidents BEGIN SELECT RAISE(ABORT,'injected failure'); END")
    with pytest.raises(sqlite3.IntegrityError,match='injected failure'):
        db.rfs_save_incident(Incident('one','Name','Advice','Council','Road','Active','Fire','',''),'revision')
    assert not db._conn.in_transaction and db.rfs_get_incident('one') is None
    other=sqlite3.connect(db.path,timeout=.1)
    try:
        other.execute("INSERT INTO events(ts,level,message) VALUES ('now','INFO','writer recovered')");other.commit()
    finally:other.close()


def test_base_exception_rolls_back_transaction(db):
    with pytest.raises(asyncio.CancelledError):
        with db.transaction('interruption test') as conn:
            conn.execute("INSERT INTO events(ts,level,message) VALUES ('now','INFO','never committed')")
            raise asyncio.CancelledError()
    assert not db._conn.in_transaction and not db.recent_events()


@pytest.mark.asyncio
async def test_supervisor_restarts_once_without_repeating_startup_recovery(db,monkeypatch):
    monkeypatch.setattr(polling_runtime,'RETRY_DELAYS',(.01,))
    poller=RFSPoller(db,SimpleNamespace());calls=[];recover=[]
    monkeypatch.setattr(db,'rfs_recover_queued',lambda:recover.append(True))
    original=poller._run
    async def unexpected():
        calls.append(True)
        if len(calls)==1:
            poller.worker.recovered_queue=True
            raise RuntimeError('unexpected loop failure')
        await original()
    poller._run=unexpected
    async def poll():pass
    poller.poll_once=poll
    poller.start();first=poller.task;poller.start()
    try:
        assert poller.task is first
        await until(lambda:len(calls)==2 and poller.worker.state=='waiting')
        assert poller.worker.restarts==1 and not recover
    finally:await poller.stop()
    await asyncio.sleep(.03)
    assert len(calls)==2


@pytest.mark.asyncio
async def test_readiness_reports_dead_and_backing_off_workers_without_database_reads(db,monkeypatch):
    app=FastAPI();app.include_router(router);app.state.db=db;app.state.tx=SimpleNamespace()
    workers=[]
    for attr,cls in [('poller',BomPoller),('rfs_poller',RFSPoller),('traffic_poller',TrafficPoller)]:
        poller=cls(db,SimpleNamespace());setattr(app.state,attr,poller)
        async def poll():pass
        poller.poll_once=poll;poller.start();workers.append(poller)
    await until(lambda:all(p.worker.state=='waiting' for p in workers))
    try:
        monkeypatch.setattr(db,'all_settings',lambda:(_ for _ in ()).throw(sqlite3.OperationalError('database is locked')))
        client=TestClient(app)
        assert client.get('/readyz').status_code==200
        workers[1].worker.state='backoff'
        assert client.get('/readyz').status_code==503
        assert client.get('/healthz').status_code==200
        result=diagnostics(app)
        assert result['database_unavailable'] and len(result['workers'])==3
    finally:
        for poller in workers:await poller.stop()


def test_accepted_delivery_bookkeeping_recovers_without_another_submission(db):
    row=db.add_service_history('rfs','one','Warning',transmit_status='queued',transmitted_text='Original message')
    result=TxResult('accepted');result.log_id=db.begin_transmission(0,'Original message','meshcore',False,(row,0,1))
    other=hold(db)
    try:
        db.finish_transmission(result)
        db.record_delivery_part(row,0,1,result)
        db.update_service_history(row,'success')
        assert db.runtime_status()['deferred_delivery_writes']==3
        with pytest.raises(sqlite3.OperationalError):
            db.begin_transmission(0,'must not be submitted','meshcore',False)
        assert len(db.query_transmit_log())==1
        other.rollback();db.flush_deferred()
        saved=db.latest_service_history('rfs','one')
        assert saved['transmit_status']=='accepted'
        assert json.loads(saved['delivery_parts'])[0]['status']=='accepted'
        assert db.runtime_status()['deferred_delivery_writes']==0
        assert len(db.query_transmit_log())==1
    finally:other.close()


def test_multipart_recovery_skips_accepted_part_after_locked_callbacks(db):
    from app.delivery import remaining_parts
    parts=['Original first part','Original second part'];text=' || '.join(parts)
    row=db.add_service_history('rfs','multipart','Warning',transmit_status='queued',transmitted_text=text)
    first=TxResult('accepted');first.log_id=db.begin_transmission(0,parts[0],'meshcore',False,(row,0,2))
    other=hold(db)
    try:
        db.finish_transmission(first)
        db.record_delivery_part(row,1,2,TxResult('not_attempted','Database unavailable before submission'))
        db.update_service_history(row,'failed')
        other.rollback();db.flush_deferred()
        saved=db.latest_service_history('rfs','multipart')
        assert saved['transmit_status']=='failed'
        assert remaining_parts(saved,text,parts)==[1]
        assert saved['transmitted_text']==text
    finally:other.close()


@pytest.mark.asyncio
async def test_database_lock_after_companion_acceptance_does_not_resend(db):
    from dataclasses import replace
    from test_transmission_reliability import make_radio, manager, FAST
    radio,commands=make_radio();tx,transport=manager(db,radio)
    row=db.add_service_history('rfs','native','Warning',transmit_status='queued',transmitted_text='Original message')
    blockers=[]
    commands.after_send=lambda:blockers.append(hold(db))
    try:
        result,_=await tx._try_send(transport,'Original message',0,policy=replace(FAST,retry_unconfirmed=True),context=(row,0,1))
        assert result.outcome=='accepted' and len(commands.calls)==1
        assert db.runtime_status()['deferred_delivery_writes']
        db.update_service_history(row,'success')
        blockers[0].rollback();db.flush_deferred()
        assert db.latest_service_history('rfs','native')['transmit_status']=='accepted'
        assert len(commands.calls)==1 and len(db.query_transmit_log())==1
    finally:
        for conn in blockers:conn.close()


def test_restart_preserves_submission_intent_if_bookkeeping_could_not_be_saved(tmp_path):
    path=str(tmp_path/'crash.db');db=Database(path);db._conn.execute('PRAGMA busy_timeout=20')
    row=db.add_service_history('rfs','one','Warning',transmit_status='queued',transmitted_text='Original message')
    result=TxResult('accepted');result.log_id=db.begin_transmission(0,'Original message','meshcore',False,(row,0,1))
    other=hold(db);db.finish_transmission(result)
    other.rollback();other.close();db._conn.close()  # Simulates abrupt loss of memory-only evidence.
    db=Database(path)
    try:
        saved=db.latest_service_history('rfs','one')
        assert saved['transmit_status']=='unconfirmed'
        assert json.loads(saved['delivery_parts'])[0]['status']=='unconfirmed'
    finally:db.close()


def test_cleanup_defers_locked_batches_and_clears_transactions(db):
    db.add_event('INFO','old')
    with db.transaction('age events') as conn:conn.execute("UPDATE events SET ts='2020-01-01'")
    other=hold(db)
    try:
        result=maintenance.clean(db,maintenance.DEFAULTS)
        assert result['events']==0 and db.maintenance_activity['deferred']
        assert not db._conn.in_transaction
    finally:other.rollback();other.close()
    assert maintenance.clean(db,maintenance.DEFAULTS)['events']==1


def test_cleanup_interrupt_budget_preserves_rows_and_releases_lock(db,monkeypatch):
    db.add_event('INFO','old')
    with db.transaction('age events') as conn:conn.execute("UPDATE events SET ts='2020-01-01'")
    original=maintenance.connection
    @contextmanager
    def slow_connection(db,**kwargs):
        with original(db,**kwargs) as conn:
            if kwargs.get('write'):
                conn.set_progress_handler(lambda:1,1)
            yield conn
    monkeypatch.setattr(maintenance,'connection',slow_connection)
    maintenance.clean(db,maintenance.DEFAULTS)
    assert db.recent_events() and db.maintenance_activity['deferred']
    assert not db._conn.in_transaction
    db.set_setting('writer_after_interrupt',True)


def test_scan_cursor_advances_past_protected_rows(db,monkeypatch):
    for i in range(8):db.add_service_history('rfs',str(i),'Warning',transmit_status='success')
    # One protected anchor per identity, followed by obsolete records for another identity.
    for i in range(3):db.add_service_history('rfs','tail','Warning',transmit_status=None)
    with db.transaction('age history') as conn:conn.execute("UPDATE service_history SET ts='2020-01-01'")
    monkeypatch.setattr(maintenance,'BATCH_SIZE',2)
    monkeypatch.setattr(maintenance,'BATCHES',2)
    for _ in range(3):maintenance.clean(db,maintenance.DEFAULTS)
    assert len(db.query_service_history(limit=100))==9


def test_backup_has_deadline_and_cleans_partial_files(db,monkeypatch):
    monkeypatch.setattr(maintenance,'BACKUP_SECONDS',0)
    with pytest.raises(TimeoutError,match='time budget'):maintenance.backup(db,7)
    assert not list((__import__('pathlib').Path(db.path+'.backups')).iterdir())
    monkeypatch.setattr(maintenance,'BACKUP_SECONDS',60)
    assert maintenance.backup(db,7)['status']=='verified'
    assert all(p.suffix=='.db' for p in __import__('pathlib').Path(db.path+'.backups').iterdir())


@pytest.mark.asyncio
async def test_accelerated_daily_cycles_preserve_delivery_and_recover(db,monkeypatch):
    monkeypatch.setattr(polling_runtime,'RETRY_DELAYS',(.001,))
    poller=RFSPoller(db,SimpleNamespace());polls=[]
    async def poll():db.set_setting('soak_poll',len(polls));polls.append(True)
    poller.poll_once=poll;poller.start()
    await until(lambda:bool(polls))
    anchor=db.add_service_history('rfs','anchor','Warning',transmit_status='accepted',transmitted_text='Original')
    try:
        for cycle in range(12):
            other=hold(db);before=poller.worker.failures
            poller.poke();await until(lambda:poller.worker.failures>before)
            other.rollback();other.close()
            await until(lambda:poller.worker.state=='waiting')
            task=asyncio.create_task(asyncio.to_thread(maintenance.clean,db,maintenance.DEFAULTS))
            poller.poke()
            row=db.add_service_history('rfs','cycle-'+str(cycle),'Warning',transmit_status='queued',transmitted_text='Original')
            evidence=TxResult('accepted');evidence.log_id=db.begin_transmission(0,'Original','meshcore',False,(row,0,1))
            db.finish_transmission(evidence);db.update_service_history(row,'success')
            await task
            assert db.latest_service_history('rfs','cycle-'+str(cycle))['transmit_status']=='accepted'
            assert db.latest_successful_broadcast('rfs','anchor')['id']==anchor
            assert poller.worker.snapshot()['alive']
        assert len(polls)>=13 and not db._conn.in_transaction
    finally:await poller.stop()

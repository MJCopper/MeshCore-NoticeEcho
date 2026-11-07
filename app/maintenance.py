"""Independent, bounded SQLite retention and verified snapshot backups."""
from __future__ import annotations

from .transmission import NOTICE_COMPLETED, sql_states
from .database_runtime import BUSY_MILLISECONDS, locked

import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import sqlite3
import time
import uuid
from contextlib import nullcontext, closing
import logging

logger = logging.getLogger('wx_echo.database')
QUERY_SECONDS = 0.25
CLEAN_SECONDS = 10.0
BACKUP_SECONDS = 60.0
BATCH_SIZE = 50
BATCHES = 100

DEFAULTS = {"maintenance_enabled": True, "maintenance_history_days": 90,
            "maintenance_log_days": 90, "maintenance_event_days": 30,
            "maintenance_error_days": 30, "maintenance_stale_days": 30,
            "maintenance_backup_keep": 7}
TABLES = ("settings", "service_history", "transmit_log", "errors", "events",
          "bom_current", "alert_state", "rfs_incidents", "traffic_items", "history", "rfs_history",
          "source_catalogue", "source_snapshots", "bom_products", "bom_product_aliases", "bom_episodes", "bom_announcements")
PENDING = "('queued','deferred','failed','interrupted')"
# Keep identity anchors even after stale current records leave the snapshot.
PROTECTED = f"""COALESCE(transmit_status,'') IN {PENDING}
 OR NOT EXISTS (SELECT 1 FROM service_history n WHERE n.source=service_history.source AND n.external_id=service_history.external_id AND n.id>service_history.id)
 OR (transmit_status IS NOT NULL AND NOT EXISTS (SELECT 1 FROM service_history n WHERE n.source=service_history.source AND n.external_id=service_history.external_id AND n.id>service_history.id AND n.transmit_status IS NOT NULL))
 OR (COALESCE(transmit_status,'') IN {sql_states(NOTICE_COMPLETED)} AND NOT EXISTS (SELECT 1 FROM service_history n WHERE n.source=service_history.source AND n.external_id=service_history.external_id AND n.id>service_history.id AND n.transmit_status IN {sql_states(NOTICE_COMPLETED)}))
 OR EXISTS (SELECT 1 FROM transmit_log t WHERE t.service_history_id=service_history.id AND (t.outcome='submitting' OR json_extract(t.evidence,'$.retry_pending')=1 OR COALESCE(json_extract(t.evidence,'$.repeat_expires_at'),0)>CAST(strftime('%s','now') AS INTEGER)))"""


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connection(db, *, write=False, operation='maintenance read', budget=None):
    gate = db._lock if write or db.path == ':memory:' else nullcontext()
    evidence = getattr(db, 'contention', None)
    with gate, evidence.operation(operation) if write and evidence else nullcontext():
        conn = db._conn if db.path == ':memory:' else sqlite3.connect(Path(db.path).resolve().as_uri()+"?mode=rw", uri=True, timeout=0.25)
        try:
            conn.execute(f"PRAGMA busy_timeout={min(BUSY_MILLISECONDS, 250)}")
            conn.execute("PRAGMA foreign_keys=ON")
            if budget is not None:
                deadline = time.monotonic()+budget
                conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            yield conn
            if write:
                conn.commit()
        except BaseException:
            if write:
                conn.set_progress_handler(None, 0)
                conn.rollback()
            raise
        finally:
            conn.set_progress_handler(None, 0)
            if db.path != ':memory:':
                conn.close()
            else:
                conn.execute(f'PRAGMA busy_timeout={BUSY_MILLISECONDS}')


def storage(db):
    path = Path(db.path)
    with connection(db) as conn:
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        pages = conn.execute("PRAGMA page_count").fetchone()[0]
        free = conn.execute("PRAGMA freelist_count").fetchone()[0]
        counts = {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in TABLES}
        protected = conn.execute(f"SELECT COUNT(*) FROM service_history WHERE {PROTECTED}").fetchone()[0]
    return {"database_bytes": path.stat().st_size if path.is_file() else 0,
            "wal_bytes": Path(str(path)+"-wal").stat().st_size if Path(str(path)+"-wal").is_file() else 0,
            "logical_bytes": pages*page_size, "reusable_bytes": free*page_size,
            "table_counts": counts, "protected_history": protected,
            "free_disk_bytes": shutil.disk_usage(path.parent).free if db.path != ":memory:" else None}


def verify(path, deep=False):
    with closing(sqlite3.connect(path.as_uri()+"?mode=ro", uri=True)) as conn:
        result = [row[0] for row in conn.execute("PRAGMA integrity_check" if deep else "PRAGMA quick_check")]
        foreign = conn.execute("PRAGMA foreign_key_check").fetchall()
        if result != ["ok"] or foreign:
            raise RuntimeError("Database check failed: " + "; ".join(result) + ("; foreign key violations" if foreign else ""))
        return {"result": "ok", "deep": deep, "checked_at": stamp()}


def backup(db, keep):
    if db.path == ":memory:":
        return {"status": "unavailable", "reason": "In-memory database"}
    source = Path(db.path).resolve()
    directory = source.parent / (source.name+".backups")
    directory.mkdir(mode=0o700, exist_ok=True)
    info = storage(db)
    if info["free_disk_bytes"] < info["logical_bytes"]*2 + 1024*1024:
        raise RuntimeError("Insufficient disk space for a verified database backup")
    target = directory / ("noticeecho-"+datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")+"-"+uuid.uuid4().hex[:8]+".db")
    temporary = target.with_suffix(".partial")
    started = time.monotonic()
    def progress(status, remaining, total):
        db.maintenance_activity = dict(stage='backup', remaining_pages=remaining, total_pages=total,
                                       elapsed_seconds=round(time.monotonic()-started, 3))
        if time.monotonic()-started >= BACKUP_SECONDS:
            raise TimeoutError('Database backup exceeded its time budget; retry later')
    try:
        with connection(db) as src, closing(sqlite3.connect(temporary)) as dest:
            src.backup(dest, pages=256, sleep=0.01, progress=progress)
        temporary.chmod(0o600)
        check = verify(temporary)
        temporary.replace(target)
        # Rotate only after the new snapshot has been verified and published.
        previous = [target] + sorted((p for p in directory.glob("noticeecho-*.db") if p != target), key=lambda p: (p.stat().st_mtime_ns,p.name), reverse=True)
        for old in previous[max(1,keep):]:
            if old != target:
                old.unlink()
        return {"status": "verified", "path": str(target), "bytes": target.stat().st_size, "check": check}
    finally:
        temporary.unlink(missing_ok=True)
        for suffix in ('-wal', '-shm'):
            Path(str(temporary)+suffix).unlink(missing_ok=True)


def clean(db, settings):
    now = datetime.now(timezone.utc)
    cutoff = lambda days: (now-timedelta(days=days)).isoformat(timespec="seconds")
    stale = cutoff(settings["maintenance_stale_days"])
    queries = {
        "bom_announcements": (f"created<? AND state NOT IN {PENDING} AND state!='submitting' "
                              "AND EXISTS (SELECT 1 FROM bom_episodes e WHERE e.id=bom_announcements.episode_id AND e.state='closed') "
                              f"AND NOT EXISTS (SELECT 1 FROM service_history h WHERE h.id=bom_announcements.history_id AND h.transmit_status IN {PENDING})",
                              (cutoff(settings["maintenance_history_days"]),)),
        "source_catalogue": (f"present=0 AND missing_polls>=2 AND last_seen<? AND NOT EXISTS "
                             f"(SELECT 1 FROM service_history h WHERE h.source=source_catalogue.source "
                             f"AND (h.external_id=source_catalogue.item_id OR (h.source='traffic' AND "
                             f"substr(h.external_id,instr(h.external_id,':')+1)=substr(source_catalogue.item_id,instr(source_catalogue.item_id,':')+1))) "
                             f"AND h.transmit_status IN {PENDING})", (stale,)),
        "rfs_incidents": (f"missing_polls>=2 AND last_seen<? AND NOT EXISTS (SELECT 1 FROM service_history h WHERE h.source='rfs' AND h.external_id=rfs_incidents.incident_id AND h.transmit_status IN {PENDING})", (stale,)),
        "traffic_items": (f"missing_polls>=2 AND last_seen<? AND NOT EXISTS (SELECT 1 FROM service_history h WHERE h.source='traffic' AND substr(h.external_id,instr(h.external_id,':')+1)=substr(traffic_items.item_id,instr(traffic_items.item_id,':')+1) AND h.transmit_status IN {PENDING})", (stale,)),
        "bom_current": (f"julianday(expires)<julianday(?) AND fetched_at<? AND NOT EXISTS (SELECT 1 FROM service_history h WHERE h.source='bom' AND h.external_id=bom_current.alert_id AND h.transmit_status IN {PENDING})", (stale,stale)),
        "alert_state": (f"julianday(expires)<julianday(?) AND NOT EXISTS (SELECT 1 FROM bom_current b WHERE b.alert_id=alert_state.alert_id) AND NOT EXISTS (SELECT 1 FROM service_history h WHERE h.source='bom' AND h.external_id=alert_state.alert_id AND h.transmit_status IN {PENDING})", (cutoff(2),)),
        "service_history": (f"ts<? AND NOT ({PROTECTED})", (cutoff(settings["maintenance_history_days"]),)),
        "transmit_log": ("ts<? AND outcome!='submitting' AND COALESCE(json_extract(evidence,'$.retry_pending'),0)!=1 AND COALESCE(json_extract(evidence,'$.repeat_expires_at'),0)<? AND NOT EXISTS (SELECT 1 FROM service_history h WHERE h.id=transmit_log.service_history_id) AND NOT EXISTS (SELECT 1 FROM service_history h,json_each(h.delivery_parts) p WHERE json_extract(p.value,'$.log_id')=transmit_log.id OR EXISTS (SELECT 1 FROM json_each(p.value,'$.attempt_ids') a WHERE a.value=transmit_log.id))", (cutoff(settings["maintenance_log_days"]),now.timestamp())),
        "events": ("ts<?", (cutoff(settings["maintenance_event_days"]),)),
        "errors": ("ts<?", (cutoff(settings["maintenance_error_days"]),)),
    }
    # Cheap retention predicates keep recent rows out of the scan budget. Full
    # protection is rechecked inside each write transaction below.
    scans = {
        'bom_announcements': ('created<?', (cutoff(settings['maintenance_history_days']),)),
        'source_catalogue': ('present=0 AND missing_polls>=2 AND last_seen<?', (stale,)),
        'rfs_incidents': ('missing_polls>=2 AND last_seen<?', (stale,)),
        'traffic_items': ('missing_polls>=2 AND last_seen<?', (stale,)),
        'bom_current': ('fetched_at<?', (stale,)),
        'alert_state': ('julianday(expires)<julianday(?)', (cutoff(2),)),
        'service_history': ('ts<?', (cutoff(settings['maintenance_history_days']),)),
        'transmit_log': ('ts<?', (cutoff(settings['maintenance_log_days']),)),
        'events': ('ts<?', (cutoff(settings['maintenance_event_days']),)),
        'errors': ('ts<?', (cutoff(settings['maintenance_error_days']),)),
    }
    deleted = {}
    started = time.monotonic()
    cursors = dict(getattr(db, 'get_setting', lambda key, default: default)('maintenance_scan_cursors', {}))
    deferred = []
    db.maintenance_activity = dict(stage='cleanup', deferred=deferred)
    # Scan bounded rowid ranges and resume next run, including past protected rows.
    for table,(where,args) in queries.items():
        deleted[table] = 0
        for _ in range(BATCHES):
            if time.monotonic()-started >= CLEAN_SECONDS:
                deferred.append(dict(table=table, reason='Run time budget reached'))
                break
            batch_started = time.monotonic()
            db.maintenance_activity = dict(stage='cleanup', table=table, deferred=list(deferred),
                                           started_at=time.time())
            try:
                with connection(db, budget=QUERY_SECONDS) as conn:
                    scan_where, scan_args = scans[table]
                    candidates = [r[0] for r in conn.execute(f'SELECT rowid FROM {table} WHERE rowid>? AND ({scan_where}) ORDER BY rowid LIMIT ?',
                                                             (cursors.get(table, 0), *scan_args, BATCH_SIZE))]
                if not candidates:
                    cursors[table] = 0
                    break
                with connection(db, write=True, operation='cleanup '+table, budget=QUERY_SECONDS) as conn:
                    marks = ','.join('?' for _ in candidates)
                    count = conn.execute(f'DELETE FROM {table} WHERE rowid IN ({marks}) AND ({where})', (*candidates, *args)).rowcount
                cursors[table] = candidates[-1]
            except sqlite3.OperationalError as exc:
                if not locked(exc) and getattr(exc, 'sqlite_errorcode', 0) != sqlite3.SQLITE_INTERRUPT:
                    raise
                deferred.append(dict(table=table, reason='Busy' if locked(exc) else 'Query time budget reached',
                                     code=getattr(exc, 'sqlite_errorname', None)))
                logger.warning('Cleanup deferred: %s', deferred[-1])
                break
            deleted[table] += count
            db.maintenance_activity.update(batch_seconds=round(time.monotonic()-batch_started, 4))
            if len(candidates) < BATCH_SIZE:
                cursors[table] = 0
                break
            time.sleep(0.005)
        else:
            deferred.append(dict(table=table, reason='Batch limit reached; scan will resume'))
    db.maintenance_activity = dict(stage='cleanup finished', deferred=deferred, cursors=cursors,
                                   elapsed_seconds=round(time.monotonic()-started, 3))
    if hasattr(db, 'set_setting'):
        try:
            db.set_setting('maintenance_scan_cursors', cursors)
        except sqlite3.Error as exc:
            if not locked(exc):
                raise
            deferred.append(dict(table='settings', reason='Busy; scan cursor not saved'))
    return deleted


class Maintenance:
    def __init__(self, db):
        self.db = db
        self.task = None
        self.scheduler = None
        self.stopping = asyncio.Event()
        self.running = False
        self.last = db.get_setting("maintenance_last", {})
        self.last_run = db.get_setting("maintenance_last_run", {})
        self.last_backup = db.get_setting("maintenance_last_backup", {})
        self.last_check = db.get_setting("maintenance_last_check", {})

    def settings(self):
        saved = self.db.all_settings()
        result = DEFAULTS | {key:saved[key] for key in DEFAULTS if key in saved}
        for key in DEFAULTS.keys()-{"maintenance_enabled"}:
            result[key] = max(1,min(3650,int(result[key])))
        return result

    def start(self):
        self.scheduler = asyncio.create_task(self._schedule())

    async def _schedule(self):
        while not self.stopping.is_set():
            try:
                settings=await asyncio.to_thread(self.settings)
            except Exception:
                logger.exception('Could not read maintenance schedule; retrying in 60 seconds')
                try:
                    await asyncio.wait_for(self.stopping.wait(), timeout=60)
                except asyncio.TimeoutError:
                    pass
                continue
            if self.stopping.is_set():
                break
            if settings["maintenance_enabled"] and not self.running:
                scheduled = self.last_run
                previous = scheduled.get("finished", "")
                try:
                    elapsed = (datetime.now(timezone.utc)-datetime.fromisoformat(previous)).total_seconds()
                except (ValueError,TypeError):
                    elapsed = float("inf")
                if elapsed >= (86400 if scheduled.get("status")=="success" else 3600):
                    self.trigger("maintenance")
            try:
                await asyncio.wait_for(self.stopping.wait(), timeout=60)
            except asyncio.TimeoutError:
                pass

    def trigger(self, action):
        if action not in ("maintenance","backup","check","deep-check"):
            raise ValueError("Unknown database maintenance action")
        if self.running:
            raise RuntimeError("Database maintenance is already running")
        if self.stopping.is_set():
            raise RuntimeError("Application is shutting down")
        self.running = True
        self.last = {"action": action, "status": "running", "started": stamp()}
        self.task = asyncio.create_task(self._run(action))
        return self.last.copy()

    async def _thread(self, function, *args):
        worker=asyncio.create_task(asyncio.to_thread(function,*args))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            # Threads cannot be cancelled: finish before closing SQLite.
            return await worker

    def _record(self,result):
        action=result["action"]
        if result["status"]=="failed":
            self.db.add_error("database","Database "+action+" failed: "+result["error"])
        else:
            self.db.add_event("INFO","Database "+action+" "+('deferred' if result['status']=='deferred' else 'completed'))
        self.db.set_setting("maintenance_last",result)
        if action=="maintenance":
            self.db.set_setting("maintenance_last_run",result)

    async def _run(self, action):
        result=self.last.copy()
        try:
            result.update(await self._thread(self._work,action),status="success")
            if result.get('cleanup', {}).get('deferred'):
                result['status'] = 'deferred'
        except Exception as exc:
            result.update(status="failed",error=str(exc))
        result["finished"]=stamp()
        self.last=result
        if action=="maintenance":
            self.last_run=result
        try:
            await self._thread(self._record,result)
        except Exception as exc:
            result.update(status="failed",error=result.get("error","")+"; unable to persist maintenance result: "+str(exc))
            import logging
            logging.getLogger("wx_echo.database").error(result["error"])
        finally:
            self.running=False

    def _work(self, action):
        settings = self.settings()
        output = {"settings_used":settings}
        if action in ("maintenance","backup"):
            self.last_backup = backup(self.db,settings["maintenance_backup_keep"])
            self.db.set_setting("maintenance_last_backup",self.last_backup)
            output["backup"] = self.last_backup
        if action in ("maintenance","check","deep-check"):
            if self.db.path == ":memory:":
                with connection(self.db) as conn:
                    checks=[r[0] for r in conn.execute("PRAGMA integrity_check" if action=="deep-check" else "PRAGMA quick_check")]
                    if checks != ["ok"] or conn.execute("PRAGMA foreign_key_check").fetchall():
                        raise RuntimeError("Database check failed")
                self.last_check={"result":"ok","deep":action=="deep-check","checked_at":stamp()}
            else:
                self.last_check=verify(Path(self.db.path).resolve(),action=="deep-check")
            self.db.set_setting("maintenance_last_check",self.last_check)
            output["check"]=self.last_check
        if action == "maintenance":
            output["deleted"]=clean(self.db,settings)
            output['cleanup'] = dict(getattr(self.db, 'maintenance_activity', {}))
            self.db.maintenance_activity = dict(stage='checkpoint and optimize')
            with connection(self.db, write=True, operation='checkpoint and optimize', budget=QUERY_SECONDS) as conn:
                output["checkpoint"]=list(conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone())
                conn.execute("PRAGMA optimize")
        self.db.maintenance_activity = dict(stage='storage')
        output["storage"]=storage(self.db)
        self.db.maintenance_activity = dict(stage='idle')
        return output

    async def close(self):
        self.stopping.set()
        if self.scheduler:
            await self.scheduler
        if self.task:
            await asyncio.shield(self.task)

    async def status(self):
        try:
            settings=await asyncio.to_thread(self.settings)
            info=await asyncio.to_thread(storage,self.db)
        except sqlite3.Error as exc:
            settings=DEFAULTS.copy()
            info=dict(error=str(exc), unavailable=True)
        last_run=self.last_run
        next_run=None
        if settings["maintenance_enabled"]:
            try:
                next_run=(datetime.fromisoformat(last_run["finished"])+timedelta(seconds=86400 if last_run.get("status")=="success" else 3600)).isoformat()
            except (KeyError,ValueError,TypeError):
                next_run="Due on startup"
        return {"running":self.running, "settings":settings, "last":self.last,
                "last_run":last_run,"next_run":next_run,
                "last_backup":self.last_backup,"last_check":self.last_check,
                "storage":info,"activity":dict(getattr(self.db,'maintenance_activity',{})),
                "contention":self.db.runtime_status() if hasattr(self.db,'runtime_status') else {}}


def main():
    """Offline operator checks, snapshots and optional compaction."""
    import argparse
    import threading
    from types import SimpleNamespace
    parser=argparse.ArgumentParser(description="NoticeEcho database maintenance")
    parser.add_argument("action", choices=("check","deep-check","backup","compact"))
    parser.add_argument("--database",required=True)
    parser.add_argument("--confirm-stopped",action="store_true",help="Confirm NoticeEcho is stopped before compaction")
    args=parser.parse_args()
    path=Path(args.database).resolve()
    if not path.is_file():
        parser.error("Database must already exist")
    if args.action=="compact" and not args.confirm_stopped:
        parser.error("Stop NoticeEcho and supply --confirm-stopped before offline compaction")
    db=SimpleNamespace(path=str(path),_lock=threading.Lock())
    try:
        if args.action in ("check","deep-check"):
            result=verify(path,args.action=="deep-check")
        else:
            result={"backup":backup(db,7)}
            if args.action=="compact":
                verify(path,True)
                info=storage(db)
                if info["free_disk_bytes"] < info["logical_bytes"]*3+1024*1024:
                    raise RuntimeError("Insufficient disk space for compaction")
                with connection(db) as conn:
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    conn.execute("VACUUM")
                result.update(check=verify(path,True),storage=storage(db))
        print(json.dumps(result,indent=2))
    except (OSError,sqlite3.Error,RuntimeError) as exc:
        parser.exit(1,str(exc)+"\n")


if __name__=="__main__":
    main()

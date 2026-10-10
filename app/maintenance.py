"""Independent, bounded SQLite retention and verified snapshot backups."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import sqlite3
import time
import uuid

DEFAULTS = {"maintenance_enabled": True, "maintenance_history_days": 90,
            "maintenance_log_days": 90, "maintenance_event_days": 30,
            "maintenance_error_days": 30, "maintenance_stale_days": 30,
            "maintenance_backup_keep": 7}
TABLES = ("settings", "service_history", "transmit_log", "errors", "events",
          "bom_current", "alert_state", "rfs_incidents", "traffic_items", "history", "rfs_history")
PENDING = "('queued','deferred','failed','interrupted')"
# Keep identity anchors even after stale current records leave the snapshot.
PROTECTED = f"""COALESCE(transmit_status,'') IN {PENDING}
 OR id IN (SELECT MAX(id) FROM service_history GROUP BY source,external_id)
 OR id IN (SELECT MAX(id) FROM service_history WHERE transmit_status IS NOT NULL GROUP BY source,external_id)
 OR id IN (SELECT MAX(id) FROM service_history WHERE transmit_status IN ('success','repeat_confirmed','unconfirmed') GROUP BY source,external_id)
 OR id IN (SELECT service_history_id FROM transmit_log WHERE service_history_id IS NOT NULL AND (outcome='submitting' OR json_extract(evidence,'$.retry_pending')=1 OR COALESCE(json_extract(evidence,'$.repeat_expires_at'),0)>CAST(strftime('%s','now') AS INTEGER)))"""


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connection(db):
    if db.path == ":memory:":
        with db._lock:
            yield db._conn
    else:
        conn = sqlite3.connect(Path(db.path).resolve().as_uri()+"?mode=rw", uri=True, timeout=1)
        try:
            conn.execute("PRAGMA busy_timeout=1000")
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
        finally:
            conn.close()


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
    with sqlite3.connect(path.as_uri()+"?mode=ro", uri=True) as conn:
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
    try:
        with connection(db) as src, sqlite3.connect(temporary) as dest:
            src.backup(dest, pages=256, sleep=0.01)
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


def clean(db, settings):
    now = datetime.now(timezone.utc)
    cutoff = lambda days: (now-timedelta(days=days)).isoformat(timespec="seconds")
    stale = cutoff(settings["maintenance_stale_days"])
    queries = {
        "rfs_incidents": (f"missing_polls>=2 AND last_seen<? AND NOT EXISTS (SELECT 1 FROM service_history h WHERE h.source='rfs' AND h.external_id=rfs_incidents.incident_id AND h.transmit_status IN {PENDING})", (stale,)),
        "traffic_items": (f"missing_polls>=2 AND last_seen<? AND NOT EXISTS (SELECT 1 FROM service_history h WHERE h.source='traffic' AND substr(h.external_id,instr(h.external_id,':')+1)=substr(traffic_items.item_id,instr(traffic_items.item_id,':')+1) AND h.transmit_status IN {PENDING})", (stale,)),
        "bom_current": (f"julianday(expires)<julianday(?) AND fetched_at<? AND NOT EXISTS (SELECT 1 FROM service_history h WHERE h.source='bom' AND h.external_id=bom_current.alert_id AND h.transmit_status IN {PENDING})", (stale,stale)),
        "alert_state": (f"julianday(expires)<julianday(?) AND NOT EXISTS (SELECT 1 FROM bom_current b WHERE b.alert_id=alert_state.alert_id) AND NOT EXISTS (SELECT 1 FROM service_history h WHERE h.source='bom' AND h.external_id=alert_state.alert_id AND h.transmit_status IN {PENDING})", (cutoff(2),)),
        "service_history": (f"ts<? AND NOT ({PROTECTED})", (cutoff(settings["maintenance_history_days"]),)),
        "transmit_log": ("ts<? AND outcome!='submitting' AND COALESCE(json_extract(evidence,'$.retry_pending'),0)!=1 AND COALESCE(json_extract(evidence,'$.repeat_expires_at'),0)<? AND NOT EXISTS (SELECT 1 FROM service_history h WHERE h.id=transmit_log.service_history_id) AND NOT EXISTS (SELECT 1 FROM service_history h,json_each(h.delivery_parts) p WHERE json_extract(p.value,'$.log_id')=transmit_log.id OR EXISTS (SELECT 1 FROM json_each(p.value,'$.attempt_ids') a WHERE a.value=transmit_log.id))", (cutoff(settings["maintenance_log_days"]),now.timestamp())),
        "events": ("ts<?", (cutoff(settings["maintenance_event_days"]),)),
        "errors": ("ts<?", (cutoff(settings["maintenance_error_days"]),)),
    }
    deleted = {}
    # Commit each small batch; bound one maintenance run even on a large backlog.
    for table,(where,args) in queries.items():
        deleted[table] = 0
        for _ in range(20):
            with connection(db) as conn:
                try:
                    cursor = conn.execute(f"DELETE FROM {table} WHERE rowid IN (SELECT rowid FROM {table} WHERE {where} LIMIT 250)", args)
                    conn.commit()
                    count = cursor.rowcount
                except Exception:
                    conn.rollback()
                    raise
            deleted[table] += count
            if count < 250:
                break
            time.sleep(0.005)
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
            settings=await asyncio.to_thread(self.settings)
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
            self.db.add_event("INFO","Database "+action+" completed")
        self.db.set_setting("maintenance_last",result)
        if action=="maintenance":
            self.db.set_setting("maintenance_last_run",result)

    async def _run(self, action):
        result=self.last.copy()
        try:
            result.update(await self._thread(self._work,action),status="success")
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
            with connection(self.db) as conn:
                output["checkpoint"]=list(conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone())
                conn.execute("PRAGMA optimize")
        output["storage"]=storage(self.db)
        return output

    async def close(self):
        self.stopping.set()
        if self.scheduler:
            await self.scheduler
        if self.task:
            await asyncio.shield(self.task)

    async def status(self):
        settings=await asyncio.to_thread(self.settings)
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
                "storage":await asyncio.to_thread(storage,self.db)}


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

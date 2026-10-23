"""One-time, offline migration to the NoticeEcho database filename."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile

DATABASE_NAME = "noticeecho.db"
LEGACY_NAMES = ("notice-echo.db", "wx-echo.db", "mesh-wx.db")


def migrate(database: str, *, confirm_stopped: bool = False) -> dict:
    if not confirm_stopped:
        raise RuntimeError("Stop NoticeEcho and supply --confirm-stopped before migrating")
    source = Path(database).expanduser().resolve()
    target = source.with_name(DATABASE_NAME)
    marker = source.parent / ".noticeecho-db-migration.json"
    if source == target:
        raise RuntimeError("The database already has the canonical name")
    if not source.is_file():
        raise RuntimeError("Source database must already exist")
    if target.exists():
        if marker.is_file():
            recorded = json.loads(marker.read_text())
            if recorded.get("source") == str(source) and recorded.get("target") == str(target):
                return recorded | {"status": "already migrated"}
        raise RuntimeError("noticeecho.db already exists; refusing to overwrite or merge databases")
    old_backups = Path(str(source) + ".backups")
    new_backups = Path(str(target) + ".backups")
    if new_backups.exists():
        raise RuntimeError("Destination backup directory already exists; refusing to overwrite it")
    # Work on a temporary copy. SQLite's backup API includes committed WAL data;
    # copying the main file alone would lose recently committed transactions.
    with tempfile.TemporaryDirectory(prefix=".noticeecho-migration-", dir=source.parent) as work:
        staged = Path(work) / DATABASE_NAME
        original = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=5)
        copied = sqlite3.connect(staged)
        try:
            original.backup(copied)
            result = [row[0] for row in copied.execute("PRAGMA integrity_check")]
            if result != ["ok"]:
                raise RuntimeError("Migrated database failed integrity check: " + "; ".join(result))
        finally:
            copied.close()
            original.close()
        staged.chmod(source.stat().st_mode & 0o777)
        if old_backups.exists():
            shutil.copytree(old_backups, Path(work) / "backups", symlinks=True)
        # A hard link publishes the completed database without overwriting an
        # existing destination, even if it appeared after the initial check.
        os.link(staged, target)
        try:
            if old_backups.exists():
                os.rename(Path(work) / "backups", new_backups)
        except OSError:
            target.unlink()
            raise
    result = {"status": "migrated", "source": str(source), "target": str(target),
              "backups": str(new_backups) if old_backups.exists() else None,
              "rollback": "Original database, WAL files and backups retained unchanged"}
    marker.write_text(json.dumps(result, indent=2) + "\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, help="Existing database to migrate")
    parser.add_argument("--confirm-stopped", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(migrate(args.database, confirm_stopped=args.confirm_stopped), indent=2))
    except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
        parser.exit(1, str(exc) + "\n")


if __name__ == "__main__":
    main()

"""Offline filename migration preserves committed WAL data and fails closed."""
import sqlite3

import pytest

from app.config import load_bootstrap
from app.migrate_database import migrate


def seed(path):
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("CREATE TABLE evidence (id INTEGER PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO evidence(value) VALUES ('retained history and settings')")
    conn.commit()
    return conn


def test_migration_copies_committed_wal_and_backups_and_is_repeatable(tmp_path, monkeypatch):
    source = tmp_path / 'wx-echo.db'
    original = seed(source)
    backups = tmp_path / 'wx-echo.db.backups'
    backups.mkdir()
    (backups / 'verified.db').write_bytes(b'original backup bytes')
    try:
        assert (tmp_path / 'wx-echo.db-wal').stat().st_size > 0
        result = migrate(str(source), confirm_stopped=True)
        target = tmp_path / 'noticeecho.db'
        with sqlite3.connect(target) as migrated:
            assert migrated.execute('SELECT * FROM evidence').fetchall() == original.execute('SELECT * FROM evidence').fetchall()
            assert migrated.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert source.is_file()
        assert (backups / 'verified.db').read_bytes() == b'original backup bytes'
        assert (tmp_path / 'noticeecho.db.backups' / 'verified.db').read_bytes() == b'original backup bytes'
        assert result['status'] == 'migrated'
        assert migrate(str(source), confirm_stopped=True)['status'] == 'already migrated'
        monkeypatch.setenv('NOTICE_ECHO_DB', str(source))
        assert load_bootstrap().db_path == str(target)
        target.unlink()
        with pytest.raises(RuntimeError, match='missing'):
            load_bootstrap()
    finally:
        original.close()


def test_requires_offline_confirmation_and_existing_source(tmp_path):
    source = tmp_path / 'wx-echo.db'
    with pytest.raises(RuntimeError, match='confirm-stopped'):
        migrate(str(source))
    with pytest.raises(RuntimeError, match='already exist'):
        migrate(str(source), confirm_stopped=True)
    assert not (tmp_path / 'noticeecho.db').exists()


def test_refuses_existing_destination_or_backup_folder(tmp_path):
    source = tmp_path / 'wx-echo.db'
    seed(source).close()
    target = tmp_path / 'noticeecho.db'
    target.write_bytes(b'never overwrite')
    with pytest.raises(RuntimeError, match='refusing'):
        migrate(str(source), confirm_stopped=True)
    assert target.read_bytes() == b'never overwrite'
    target.unlink()
    (tmp_path / 'noticeecho.db.backups').mkdir()
    with pytest.raises(RuntimeError, match='backup directory'):
        migrate(str(source), confirm_stopped=True)
    assert not target.exists()


def test_corrupt_source_does_not_publish_destination(tmp_path):
    source = tmp_path / 'wx-echo.db'
    source.write_bytes(b'not SQLite')
    with pytest.raises(sqlite3.DatabaseError):
        migrate(str(source), confirm_stopped=True)
    assert not (tmp_path / 'noticeecho.db').exists()


def test_new_configured_path_cannot_hide_legacy_database(tmp_path, monkeypatch):
    source = tmp_path / 'wx-echo.db'
    seed(source).close()
    monkeypatch.setenv('NOTICE_ECHO_DB', str(tmp_path / 'noticeecho.db'))
    with pytest.raises(RuntimeError, match='Legacy database found'):
        load_bootstrap()
    assert not (tmp_path / 'noticeecho.db').exists()

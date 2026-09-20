import sqlite3

import pytest

from backup import BackupError, backup_database, restore_database


def _make_db(path):
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE customers(id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO customers(name) VALUES('Alice')")
        conn.commit()


def test_backup_creates_integrity_checked_snapshot_and_retains_latest(tmp_path):
    source = tmp_path / "source.db"
    backup_dir = tmp_path / "backups"
    _make_db(source)

    created = []
    for _ in range(3):
        created.append(backup_database(source, backup_dir, retention=2))

    files = sorted(backup_dir.glob("beautybridge-*.db"))
    assert len(files) == 2
    assert created[-1].exists()

    with sqlite3.connect(created[-1]) as conn:
        assert conn.execute("SELECT name FROM customers").fetchone()[0] == "Alice"


def test_restore_refuses_overwrite_without_force(tmp_path):
    source = tmp_path / "source.db"
    backup_dir = tmp_path / "backups"
    target = tmp_path / "target.db"
    _make_db(source)
    backup = backup_database(source, backup_dir)

    _make_db(target)
    with pytest.raises(BackupError, match="already exists"):
        restore_database(backup, target)


def test_restore_replaces_database_when_forced(tmp_path):
    source = tmp_path / "source.db"
    backup_dir = tmp_path / "backups"
    target = tmp_path / "target.db"
    _make_db(source)
    backup = backup_database(source, backup_dir)

    with sqlite3.connect(target) as conn:
        conn.execute("CREATE TABLE customers(id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO customers(name) VALUES('Bob')")
        conn.commit()

    restore_database(backup, target, force=True)

    with sqlite3.connect(target) as conn:
        assert conn.execute("SELECT name FROM customers").fetchone()[0] == "Alice"


def test_backup_rejects_corrupt_source(tmp_path):
    source = tmp_path / "bad.db"
    source.write_bytes(b"not sqlite")
    with pytest.raises(BackupError, match="integrity"):
        backup_database(source, tmp_path / "backups")

"""SQLite backup and restore utilities for BeautyBridge.

Backups are file-level SQLite snapshots created with SQLite's online backup
API. Restore is deliberately explicit and refuses to overwrite an existing
database unless force=True.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_RETENTION = 14


class BackupError(RuntimeError):
    pass


def _integrity_ok(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        with sqlite3.connect(path) as conn:
            row = conn.execute("PRAGMA integrity_check").fetchone()
    except sqlite3.DatabaseError:
        return False
    return bool(row and str(row[0]).lower() == "ok")


def backup_database(
    source: str | Path,
    backup_dir: str | Path,
    *,
    retention: int = DEFAULT_RETENTION,
) -> Path:
    source = Path(source)
    backup_dir = Path(backup_dir)
    if not source.exists():
        raise BackupError(f"Database does not exist: {source}")

    backup_dir.mkdir(parents=True, exist_ok=True)
    if not _integrity_ok(source):
        raise BackupError(f"Source database failed integrity check: {source}")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    final_path = backup_dir / f"beautybridge-{stamp}-{uuid.uuid4().hex[:8]}.db"
    fd, tmp_name = tempfile.mkstemp(
        prefix=".beautybridge-",
        suffix=".db.tmp",
        dir=backup_dir,
    )
    os.close(fd)
    tmp_path = Path(tmp_name)

    try:
        with sqlite3.connect(source) as src, sqlite3.connect(tmp_path) as dst:
            src.backup(dst)
        if not _integrity_ok(tmp_path):
            raise BackupError("Backup failed integrity check")
        os.replace(tmp_path, final_path)
    finally:
        tmp_path.unlink(missing_ok=True)

    backups = sorted(
        backup_dir.glob("beautybridge-*.db"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for stale in backups[max(1, int(retention)):]:
        stale.unlink(missing_ok=True)

    return final_path


def restore_database(
    backup: str | Path,
    target: str | Path,
    *,
    force: bool = False,
) -> Path:
    backup = Path(backup)
    target = Path(target)

    if not backup.exists():
        raise BackupError(f"Backup does not exist: {backup}")
    if not _integrity_ok(backup):
        raise BackupError(f"Backup failed integrity check: {backup}")
    if target.exists() and not force:
        raise BackupError(
            f"Target already exists: {target}; use force=True to replace it"
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=".beautybridge-restore-",
        suffix=".db.tmp",
        dir=target.parent,
    )
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        with sqlite3.connect(backup) as src, sqlite3.connect(tmp_path) as dst:
            src.backup(dst)
        if not _integrity_ok(tmp_path):
            raise BackupError("Restored database failed integrity check")
        os.replace(tmp_path, target)
    finally:
        tmp_path.unlink(missing_ok=True)

    return target


def main() -> int:
    parser = argparse.ArgumentParser(description="BeautyBridge SQLite backup/restore")
    sub = parser.add_subparsers(dest="command", required=True)

    b = sub.add_parser("backup")
    b.add_argument("--db", required=True)
    b.add_argument("--dir", required=True)
    b.add_argument("--retention", type=int, default=DEFAULT_RETENTION)

    r = sub.add_parser("restore")
    r.add_argument("--backup", required=True)
    r.add_argument("--db", required=True)
    r.add_argument("--force", action="store_true")

    args = parser.parse_args()
    if args.command == "backup":
        print(backup_database(args.db, args.dir, retention=args.retention))
    else:
        print(restore_database(args.backup, args.db, force=args.force))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

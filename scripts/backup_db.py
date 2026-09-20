"""CLI entrypoint for production SQLite backups."""

from __future__ import annotations

import os
from pathlib import Path

from backup import backup_database


def main() -> int:
    db_path = Path(os.getenv("DB_PATH", "data/beautybridge.db"))
    backup_dir = Path(os.getenv("BACKUP_DIR", "data/backups"))
    retention = max(1, int(os.getenv("BACKUP_RETENTION", "14")))
    result = backup_database(db_path, backup_dir, retention=retention)
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""SQLite persistence with tenant isolation, event idempotency and a durable queue."""
from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from typing import Any


SCHEMA = """
CREATE TABLE IF NOT EXISTS inbound_events (
    event_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, sender_id TEXT NOT NULL,
    payload_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
    attempts INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_inbound_events_status ON inbound_events(status, created_at);
CREATE TABLE IF NOT EXISTS conversations (
    tenant_id TEXT NOT NULL, sender_id TEXT NOT NULL, state_json TEXT NOT NULL,
    updated_at REAL NOT NULL, PRIMARY KEY (tenant_id, sender_id)
);
CREATE TABLE IF NOT EXISTS booking_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, sender_id TEXT NOT NULL,
    details_json TEXT NOT NULL, status TEXT NOT NULL, admin_note TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_booking_requests_tenant_status ON booking_requests(tenant_id, status, created_at);
CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, event_type TEXT NOT NULL,
    subject_id TEXT NOT NULL DEFAULT '', payload_json TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL
);
"""


class Storage:
    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30, isolation_level="DEFERRED")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        if self.path != ":memory:":
            conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def enqueue_once(self, event_id: str, tenant_id: str, sender_id: str, payload: dict[str, Any]) -> bool:
        now = time.time()
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO inbound_events(event_id,tenant_id,sender_id,payload_json,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (event_id, tenant_id, sender_id, json.dumps(payload, ensure_ascii=False), now, now),
            )
            return cur.rowcount == 1

    def next_event(self) -> dict[str, Any] | None:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM inbound_events WHERE status='queued' ORDER BY created_at,id LIMIT 1").fetchone()
            if row is None:
                return None
            conn.execute("UPDATE inbound_events SET status='processing', attempts=attempts+1, updated_at=? WHERE event_id=?", (time.time(), row["event_id"]))
            result = dict(row)
            result["payload"] = json.loads(result.pop("payload_json"))
            return result

    def finish_event(self, event_id: str, *, error: str = "") -> None:
        now = time.time()
        status = "queued" if error else "processed"
        with self.connect() as conn:
            conn.execute("UPDATE inbound_events SET status=?, updated_at=? WHERE event_id=?", (status, now, event_id))
            if error:
                self.audit(conn, "inbound_event_retry", "", {"event_id": event_id, "error": error[:300]})

    def get_conversation(self, tenant_id: str, sender_id: str) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute("SELECT state_json FROM conversations WHERE tenant_id=? AND sender_id=?", (tenant_id, sender_id)).fetchone()
        return json.loads(row[0]) if row else {"step": "collecting", "details": {}}

    def save_conversation(self, tenant_id: str, sender_id: str, state: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute("INSERT INTO conversations(tenant_id,sender_id,state_json,updated_at) VALUES(?,?,?,?) ON CONFLICT(tenant_id,sender_id) DO UPDATE SET state_json=excluded.state_json,updated_at=excluded.updated_at", (tenant_id, sender_id, json.dumps(state, ensure_ascii=False), time.time()))

    def create_booking_request(self, tenant_id: str, sender_id: str, details: dict[str, Any], status: str) -> int:
        now = time.time()
        with self.connect() as conn:
            cur = conn.execute("INSERT INTO booking_requests(tenant_id,sender_id,details_json,status,created_at,updated_at) VALUES(?,?,?,?,?,?)", (tenant_id, sender_id, json.dumps(details, ensure_ascii=False), status, now, now))
            request_id = int(cur.lastrowid)
            self.audit(conn, "booking_request_created", str(request_id), {"status": status})
            return request_id

    def get_booking_request(self, request_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM booking_requests WHERE id=?", (int(request_id),)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["details"] = json.loads(result.pop("details_json"))
        return result

    def set_booking_status(self, request_id: int, status: str, admin_note: str = "") -> bool:
        allowed = {"confirmed", "rejected", "alternative_proposed"}
        if status not in allowed:
            raise ValueError("Unsupported admin booking status")
        with self.connect() as conn:
            row = conn.execute("SELECT status FROM booking_requests WHERE id=?", (int(request_id),)).fetchone()
            if row is None or row[0] != "pending_manual_confirmation":
                return False
            conn.execute("UPDATE booking_requests SET status=?,admin_note=?,updated_at=? WHERE id=?", (status, admin_note[:1000], time.time(), int(request_id)))
            self.audit(conn, "booking_status_changed", str(request_id), {"status": status})
            return True

    @staticmethod
    def audit(conn: sqlite3.Connection, event_type: str, subject_id: str, payload: dict[str, Any], tenant_id: str = "system") -> None:
        conn.execute("INSERT INTO audit_events(tenant_id,event_type,subject_id,payload_json,created_at) VALUES(?,?,?,?,?)", (tenant_id, event_type, subject_id, json.dumps(payload, ensure_ascii=False), time.time()))

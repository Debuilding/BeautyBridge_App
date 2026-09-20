import json
from datetime import datetime, timedelta

import main


def _insert_state(db_path, data, updated_at):
    main.DB_PATH = str(db_path)
    main.init_db()
    with main.db() as conn:
        conn.execute(
            "INSERT INTO state(brand,sender_id,data,updated_at) VALUES(?,?,?,?)",
            ("test", "sender-1", json.dumps(data), updated_at),
        )


def test_stale_collecting_state_is_cleared(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "STATE_TTL_HOURS", 48)
    db_path = tmp_path / "state.db"
    updated = (datetime.now() - timedelta(hours=72)).strftime("%Y-%m-%d %H:%M:%S")
    _insert_state(
        db_path,
        {
            "state": "COLLECTING",
            "service_id": "svc1",
            "date": "2026-09-23",
            "time": "16:00",
            "employee_id": "master1",
            "name": "Old Client",
            "phone": "380501234567",
            "photo": True,
        },
        updated,
    )

    assert main.state_get("test", "sender-1") == {}


def test_fresh_collecting_state_is_preserved(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "STATE_TTL_HOURS", 48)
    db_path = tmp_path / "state.db"
    updated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    data = {
        "state": "COLLECTING",
        "service_id": "svc1",
        "date": "2026-09-23",
        "time": "16:00",
        "employee_id": "master1",
        "name": "Fresh Client",
        "phone": "380501234567",
    }
    _insert_state(db_path, data, updated)

    assert main.state_get("test", "sender-1") == data


def test_pending_payment_state_survives_conversation_ttl(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "STATE_TTL_HOURS", 48)
    db_path = tmp_path / "state.db"
    updated = (datetime.now() - timedelta(hours=72)).strftime("%Y-%m-%d %H:%M:%S")
    data = {
        "state": "WAITING_PAYMENT",
        "appointment_id": 123,
        "name": "Paying Client",
        "phone": "380501234567",
    }
    _insert_state(db_path, data, updated)

    assert main.state_get("test", "sender-1") == data

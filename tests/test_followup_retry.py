from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import main
import universal_runtime as ur


def _seed_appointment(db_path, *, appointment_date, status="confirmed", reminder_sent=0, reinvite_sent=0):
    monkey_brand = "test"
    main.DB_PATH = str(db_path)
    main.init_db()
    with main.db() as conn:
        conn.execute(
            """
            INSERT INTO appointments(
                brand, sender_id, name, phone, service_id, service_name,
                appointment_date, appointment_time, employee_id, master_name,
                crm_visit_id, status, reminder_sent, reinvite_sent
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                monkey_brand, "sender-1", "Марія", "0501234567", "svc1",
                "Манікюр", appointment_date, "10:00", "master1", "Світлана",
                "crm-1", status, reminder_sent, reinvite_sent,
            ),
        )


def test_reminder_flag_updates_only_after_success(monkeypatch, tmp_path):
    today = datetime.now(ZoneInfo(ur.config.LOCAL_TZ)).date()
    _seed_appointment(tmp_path / "reminder.db", appointment_date=(today + timedelta(days=1)).isoformat())

    monkeypatch.setitem(
        ur.config.BRANDS,
        "test",
        {"enabled": True, "name": "Test", "follow_up_days": 21},
    )
    monkeypatch.setattr(ur.legacy, "cfg_for", lambda brand: {"enabled": True, "page_access_token": "token"})
    calls = []

    def fail_once_then_succeed(cfg, sender, text):
        calls.append(text)
        if len(calls) == 1:
            raise RuntimeError("Meta temporarily unavailable")

    monkeypatch.setattr(ur.legacy, "instagram_send", fail_once_then_succeed)

    ur.daily_tasks()
    with main.db() as conn:
        flag = conn.execute("SELECT reminder_sent FROM appointments WHERE sender_id='sender-1'").fetchone()[0]
    assert flag == 0

    ur.daily_tasks()
    with main.db() as conn:
        flag = conn.execute("SELECT reminder_sent FROM appointments WHERE sender_id='sender-1'").fetchone()[0]
    assert flag == 1
    assert len(calls) == 2


def test_retention_flag_updates_only_after_success(monkeypatch, tmp_path):
    today = datetime.now(ZoneInfo(ur.config.LOCAL_TZ)).date()
    target = today - timedelta(days=21)
    _seed_appointment(tmp_path / "retention.db", appointment_date=target.isoformat())

    monkeypatch.setitem(
        ur.config.BRANDS,
        "test",
        {"enabled": True, "name": "Test", "follow_up_days": 21},
    )
    monkeypatch.setattr(ur.legacy, "cfg_for", lambda brand: {"enabled": True, "page_access_token": "token"})
    calls = []

    def fail_once_then_succeed(cfg, sender, text):
        calls.append(text)
        if len(calls) == 1:
            raise RuntimeError("Meta temporarily unavailable")

    monkeypatch.setattr(ur.legacy, "instagram_send", fail_once_then_succeed)

    ur.daily_tasks()
    with main.db() as conn:
        flag = conn.execute("SELECT reinvite_sent FROM appointments WHERE sender_id='sender-1'").fetchone()[0]
    assert flag == 0

    ur.daily_tasks()
    with main.db() as conn:
        flag = conn.execute("SELECT reinvite_sent FROM appointments WHERE sender_id='sender-1'").fetchone()[0]
    assert flag == 1
    assert len(calls) == 2


def test_retention_skips_customer_with_future_booking(monkeypatch, tmp_path):
    today = datetime.now(ZoneInfo(ur.config.LOCAL_TZ)).date()
    target = today - timedelta(days=21)
    _seed_appointment(tmp_path / "future.db", appointment_date=target.isoformat())
    main.DB_PATH = str(tmp_path / "future.db")
    with main.db() as conn:
        future = today + timedelta(days=5)
        conn.execute(
            """
            INSERT INTO appointments(
                brand, sender_id, name, phone, service_id, service_name,
                appointment_date, appointment_time, employee_id, master_name,
                crm_visit_id, status
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "test", "sender-1", "Марія", "0501234567", "svc1",
                "Манікюр", future.isoformat(), "10:00", "master1", "Світлана",
                "crm-2", "confirmed",
            ),
        )

    monkeypatch.setitem(
        ur.config.BRANDS,
        "test",
        {"enabled": True, "name": "Test", "follow_up_days": 21},
    )
    calls = []
    monkeypatch.setattr(ur.legacy, "cfg_for", lambda brand: {"enabled": True, "page_access_token": "token"})
    monkeypatch.setattr(ur.legacy, "instagram_send", lambda *args: calls.append(args))

    ur.daily_tasks()

    with main.db() as conn:
        flag = conn.execute(
            "SELECT reinvite_sent FROM appointments WHERE brand='test' AND appointment_date=?",
            (target.isoformat(),),
        ).fetchone()[0]
    assert flag == 1
    assert calls == []

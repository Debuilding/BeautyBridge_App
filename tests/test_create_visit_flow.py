"""Tests for the CRM-independent local request flow."""
import json

import universal_runtime as ur
import main


CFG = {
    "name": "Test Salon",
    "crm_type": "manual",
    "booking_mode": "table",
    "services": {"svc1": {"name": "Манікюр комплекс", "duration": 120, "requires_photo": True}},
    "masters": {"master1": "Світлана"},
    "master_services": {"master1": ["svc1"]},
    "telegram_chat_id": "test-chat",
    "prepayment_required": False,
}


def _state():
    return {
        "state": "COLLECTING", "service_id": "svc1", "employee_id": "master1",
        "date": "2099-09-23", "time": "16:00", "name": "Марія",
        "phone": "380501234567", "photo": True,
    }


def _args():
    return {
        "service_id": "svc1", "employee_id": "master1", "date_str": "2099-09-23",
        "time_str": "16:00", "name": "Марія", "phone": "0501234567",
    }


def _patch_runtime(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "booking.db"))
    ur.migrate_database()
    monkeypatch.setattr(ur.legacy, "state_get", lambda brand, sender: _state())
    monkeypatch.setattr(ur, "strict_state_set", lambda *args, **kwargs: _state())
    monkeypatch.setattr(ur.legacy, "create_local_appointment", lambda *args, **kwargs: 101)
    notifications = []
    monkeypatch.setattr(
        ur, "send_admin_action_message",
        lambda *args, **kwargs: notifications.append((args, kwargs)) or True,
    )
    adapter_calls = []
    monkeypatch.setattr(ur, "adapter_for", lambda cfg: adapter_calls.append(cfg))
    return notifications, adapter_calls


def test_create_visit_saves_manual_request_once_and_is_idempotent(monkeypatch, tmp_path):
    notifications, adapter_calls = _patch_runtime(monkeypatch, tmp_path)
    first = json.loads(ur.handle_tool("rozmary", "sender-1", CFG, "create_visit", _args()))
    second = json.loads(ur.handle_tool("rozmary", "sender-1", CFG, "create_visit", _args()))

    assert first["status"] == "MANUAL_FALLBACK"
    assert first["appointment_id"] == 101
    assert first["payment_required"] is False
    assert second["status"] == "MANUAL_FALLBACK"
    assert second["idempotent"] is True
    assert len(notifications) == 1
    assert adapter_calls == []


def test_create_visit_never_checks_live_availability(monkeypatch, tmp_path):
    notifications, adapter_calls = _patch_runtime(monkeypatch, tmp_path)
    result = json.loads(ur.handle_tool("rozmary", "sender-2", CFG, "create_visit", _args()))

    assert result["status"] == "MANUAL_FALLBACK"
    assert notifications
    assert adapter_calls == []


def test_create_visit_rejects_missing_photo_before_creating_request(monkeypatch, tmp_path):
    _patch_runtime(monkeypatch, tmp_path)
    state = _state()
    state["photo"] = False
    monkeypatch.setattr(ur.legacy, "state_get", lambda brand, sender: state)

    result = json.loads(ur.handle_tool("rozmary", "sender-3", CFG, "create_visit", _args()))
    assert result["status"] == "VALIDATION_ERROR"
    assert "фото" in result["message"].lower()


def test_local_persistence_failure_returns_error_and_releases_claim(monkeypatch, tmp_path):
    _patch_runtime(monkeypatch, tmp_path)

    def fail_local(*args, **kwargs):
        raise RuntimeError("disk write failed")

    monkeypatch.setattr(ur.legacy, "create_local_appointment", fail_local)
    result = json.loads(ur.handle_tool("rozmary", "sender-4", CFG, "create_visit", _args()))

    assert result["status"] == "LOCAL_PERSISTENCE_ERROR"
    with main.db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM booking_claims").fetchone()[0]
    assert count == 0


def test_pending_telegram_booking_notice_is_retried_until_delivered(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "retry.db"))
    ur.migrate_database()
    with main.db() as conn:
        conn.execute(
            """
            INSERT INTO appointments(
                id, brand, sender_id, name, phone, service_id, service_name,
                appointment_date, appointment_time, employee_id, master_name,
                status, notes
            ) VALUES(201, 'rozmary', 'sender-retry', 'Марія', '380501234567',
                     'svc1', 'Манікюр комплекс', '2099-09-23', '16:00',
                     'master1', 'Світлана', 'pending_manual_confirmation',
                     'Awaiting admin slot verification via Telegram; no CRM integration. | TELEGRAM_NOTIFICATION_PENDING')
            """
        )

    monkeypatch.setattr(main, "cfg_for", lambda brand: {**CFG, "name": "Test Salon"})
    attempts = []

    def fake_send(cfg, appointment_id, text, buttons):
        attempts.append((appointment_id, text, buttons))
        return len(attempts) > 1

    monkeypatch.setattr(ur, "send_admin_action_message", fake_send)

    assert ur.retry_pending_admin_notifications(force=True) == 0
    with main.db() as conn:
        notes = conn.execute("SELECT notes FROM appointments WHERE id=201").fetchone()[0]
    assert "TELEGRAM_NOTIFICATION_PENDING" in notes

    assert ur.retry_pending_admin_notifications(force=True) == 1
    with main.db() as conn:
        notes = conn.execute("SELECT notes FROM appointments WHERE id=201").fetchone()[0]
    assert "TELEGRAM_NOTIFICATION_SENT" in notes
    assert "TELEGRAM_NOTIFICATION_PENDING" not in notes
    assert len(attempts) == 2
    assert attempts[0][0] == 201
    assert "2099-09-23" in attempts[0][1]
    assert attempts[0][2] == [
        ("✅ Час доступний", "bb:time_ok:201"),
        ("🕒 Час зайнятий", "bb:time_busy:201"),
    ]


def test_flow_gate_never_claims_admin_handoff_when_date_is_missing():
    state = {
        "state": "COLLECTING", "service_id": "svc1", "time": "після 12:00",
        "photo": True, "name": "Марія", "phone": "0501234567",
    }

    reply = ur.enforce_flow_gate(CFG, state, "Дякую! Передам запит адміністратору.")

    assert "точну дату" in reply
    assert "адміністратору" not in reply


def test_flow_gate_never_claims_admin_handoff_when_contact_is_missing():
    state = {
        "state": "COLLECTING", "service_id": "svc1", "date": "2099-09-23",
        "time": "після 12:00", "photo": True,
    }

    reply = ur.enforce_flow_gate(CFG, state, "Дякую за фото, передам адміністратору.")

    assert "ім'я та номер телефону" in reply
    assert "адміністратору" not in reply


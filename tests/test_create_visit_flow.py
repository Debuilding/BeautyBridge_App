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

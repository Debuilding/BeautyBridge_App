import json

import universal_runtime as ur
import main


class FakeCRM:
    def __init__(self, available=True):
        self.available = available
        self.create_calls = []

    def is_slot_available(self, service_id, employee_id, date_str, time_str):
        return self.available

    def create_booking(self, employee_id, service_id, date_str, time_str, name, phone):
        self.create_calls.append(
            (employee_id, service_id, date_str, time_str, name, phone)
        )
        return f"fake-crm-{len(self.create_calls)}"


CFG = {
    "name": "Test Salon",
    "crm_type": "fake",
    "services": {
        "svc1": {"name": "Манікюр комплекс", "duration": 120, "requires_photo": True},
    },
    "masters": {"master1": "Світлана"},
    "master_services": {"master1": ["svc1"]},
    "prepayment_required": True,
}


def _state():
    return {
        "state": "COLLECTING",
        "service_id": "svc1",
        "employee_id": "master1",
        "date": "2099-09-23",
        "time": "16:00",
        "name": "Марія",
        "phone": "380501234567",
        "photo": True,
    }


def _args():
    return {
        "service_id": "svc1",
        "employee_id": "master1",
        "date_str": "2099-09-23",
        "time_str": "16:00",
        "name": "Марія",
        "phone": "0501234567",
    }


def _patch_runtime(monkeypatch, tmp_path, fake):
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "booking.db"))
    ur.migrate_database()
    monkeypatch.setattr(ur.legacy, "state_get", lambda brand, sender: _state())
    monkeypatch.setattr(ur, "strict_state_set", lambda *args, **kwargs: _state())
    monkeypatch.setattr(ur.legacy, "create_local_appointment", lambda *args, **kwargs: 101)
    monkeypatch.setattr(ur.legacy, "telegram", lambda *args, **kwargs: None)
    monkeypatch.setattr(ur, "adapter_for", lambda cfg: fake)


def test_handle_create_visit_books_once_and_second_call_is_idempotent(monkeypatch, tmp_path):
    fake = FakeCRM(available=True)
    _patch_runtime(monkeypatch, tmp_path, fake)

    first = json.loads(ur.handle_tool("rozmary", "sender-1", CFG, "create_visit", _args()))
    second = json.loads(ur.handle_tool("rozmary", "sender-1", CFG, "create_visit", _args()))

    assert first["status"] == "SUCCESS"
    assert first["crm_id"] == "fake-crm-1"
    assert first["payment_required"] is True
    assert second["status"] == "SUCCESS"
    assert second["idempotent"] is True
    assert second["crm_visit_id"] == "fake-crm-1"
    assert len(fake.create_calls) == 1


def test_handle_create_visit_does_not_create_when_final_slot_check_fails(monkeypatch, tmp_path):
    fake = FakeCRM(available=False)
    _patch_runtime(monkeypatch, tmp_path, fake)

    result = json.loads(ur.handle_tool("rozmary", "sender-2", CFG, "create_visit", _args()))

    assert result["status"] == "SLOT_NO_LONGER_AVAILABLE"
    assert fake.create_calls == []

    # A later retry may try again because the failed availability claim is released.
    fake.available = True
    retry = json.loads(ur.handle_tool("rozmary", "sender-2", CFG, "create_visit", _args()))
    assert retry["status"] == "SUCCESS"
    assert len(fake.create_calls) == 1


def test_handle_create_visit_rejects_missing_photo_before_claim(monkeypatch, tmp_path):
    fake = FakeCRM(available=True)
    _patch_runtime(monkeypatch, tmp_path, fake)

    state = _state()
    state["photo"] = False
    monkeypatch.setattr(ur.legacy, "state_get", lambda brand, sender: state)

    result = json.loads(ur.handle_tool("rozmary", "sender-3", CFG, "create_visit", _args()))

    assert result["status"] == "VALIDATION_ERROR"
    assert "фото" in result["message"].lower()
    assert fake.create_calls == []
    with main.db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM booking_claims").fetchone()[0]
    assert count == 0

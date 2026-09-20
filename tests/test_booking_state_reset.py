import json

import main
import universal_runtime as ur


def _seed_state(tmp_path, data):
    main.DB_PATH = str(tmp_path / "state-reset.db")
    ur.migrate_database()
    with main.db() as conn:
        conn.execute(
            "INSERT INTO state(brand,sender_id,data,updated_at) VALUES(?,?,?,CURRENT_TIMESTAMP)",
            ("test", "sender-1", json.dumps(data)),
        )


def test_new_booking_clears_previous_payment_lifecycle(tmp_path, monkeypatch):
    old_state = {
        "state": "WAITING_PAYMENT",
        "appointment_id": 99,
        "payment_confirmed": True,
        "receipt_confirmed": True,
        "receipt": True,
        "photo": True,
        "nails_photo_url": "https://old.example/photo.jpg",
        "service_id": "old-service",
        "date": "2099-09-20",
        "time": "10:00",
        "employee_id": "old-master",
        "name": "Old Client",
        "phone": "380501111111",
    }
    _seed_state(tmp_path, old_state)

    monkeypatch.setattr(ur.legacy, "telegram", lambda *args, **kwargs: None)

    result = ur.handle_tool(
        "test",
        "sender-1",
        {"crm_type": "manual"},
        "remember_booking",
        {
            "service_id": "new-service",
            "date_str": "2099-09-23",
            "time_str": "16:00",
            "employee_id": "new-master",
            "name": "New Client",
            "phone": "380502222222",
        },
    )

    assert json.loads(result)["status"] == "REMEMBERED"
    state = main.state_get("test", "sender-1")
    assert state == {
        "state": "COLLECTING",
        "service_id": "new-service",
        "date": "2099-09-23",
        "time": "16:00",
        "employee_id": "new-master",
        "name": "New Client",
        "phone": "380502222222",
    }


def test_new_booking_from_confirmed_state_does_not_reuse_old_photo_or_appointment(
    tmp_path,
    monkeypatch,
):
    old_state = {
        "state": "BOOKED_CONFIRMED",
        "appointment_id": 101,
        "payment_confirmed": True,
        "receipt_confirmed": True,
        "photo": True,
        "nails_photo_url": "https://old.example/photo.jpg",
    }
    _seed_state(tmp_path, old_state)
    monkeypatch.setattr(ur.legacy, "telegram", lambda *args, **kwargs: None)

    ur.handle_tool(
        "test",
        "sender-1",
        {"crm_type": "manual"},
        "remember_booking",
        {"service_id": "new-service"},
    )

    state = main.state_get("test", "sender-1")
    assert state == {
        "state": "COLLECTING",
        "service_id": "new-service",
    }

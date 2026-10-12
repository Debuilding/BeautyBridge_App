from flow_guard import next_flow_reply


def test_waiting_admin_confirmation_reply_includes_request_details():
    reply = next_flow_reply(
        {
            "services": {"manicure": {"name": "Манікюр"}},
        },
        {
            "state": "WAITING_ADMIN_CONFIRMATION",
            "service_id": "manicure",
            "date": "2026-10-14",
            "time": "11:00",
        },
    )

    assert "Манікюр" in reply
    assert "2026-10-14" in reply
    assert "11:00" in reply
    assert "уточню доступність" in reply.lower()
    assert "адміністратор" not in reply
    assert "передала заявку" not in reply


def test_waiting_client_time_confirmation_asks_for_yes_or_no():
    reply = next_flow_reply({}, {"state": "WAITING_CLIENT_TIME_CONFIRMATION"})
    assert "підходить" in reply.lower()
    assert "так" in reply.lower()
    assert "ні" in reply.lower()



def test_client_acceptance_returns_confirmation_without_sending_it_twice(monkeypatch):
    import universal_runtime as ur

    cfg = {"name": "Test Salon", "address": "Test address"}
    state = {
        "state": "WAITING_CLIENT_TIME_CONFIRMATION",
        "appointment_id": 101,
        "name": "Марія",
    }
    row = (
        101, "rozmary", "sender-1", "Марія", "380501234567",
        "Манікюр", "2026-10-14", "11:00", "Світлана",
        "waiting_client_confirmation", 0, 0, "",
    )
    instagram_sends = []
    admin_sends = []

    monkeypatch.setattr(ur, "audit_event", lambda *a, **k: None)
    monkeypatch.setattr(ur.legacy, "cfg_for", lambda brand: cfg)
    monkeypatch.setattr(ur.legacy, "save_message", lambda *a, **k: None)
    monkeypatch.setattr(ur.legacy, "state_get", lambda *a, **k: state)
    monkeypatch.setattr(ur, "strict_state_set", lambda *a, **k: state)
    monkeypatch.setattr(ur, "appointment_row", lambda appointment_id: row)
    monkeypatch.setattr(ur, "update_appointment", lambda *a, **k: None)
    monkeypatch.setattr(ur, "booking_address_text", lambda cfg: "Адреса: Test address")
    monkeypatch.setattr(ur, "send_admin_telegram", lambda *a, **k: admin_sends.append((a, k)) or True)
    monkeypatch.setattr(ur.legacy, "instagram_send", lambda *a, **k: instagram_sends.append((a, k)))

    reply = ur.process_with_ai("rozmary", "sender-1", "Так")

    assert "ваш запис підтверджено" in reply.lower()
    assert instagram_sends == []
    assert len(admin_sends) == 1



def test_waiting_admin_confirmation_does_not_call_ai_or_mutate_pending_request(monkeypatch):
    import universal_runtime as ur

    cfg = {"services": {"manicure": {"name": "Манікюр"}}}
    state = {
        "state": "WAITING_ADMIN_CONFIRMATION",
        "appointment_id": 101,
        "service_id": "manicure",
        "date": "2026-10-14",
        "time": "11:00",
        "employee_id": "master-1",
        "name": "Марія",
        "phone": "380501234567",
    }
    saved_messages = []
    monkeypatch.setattr(ur, "audit_event", lambda *a, **k: None)
    monkeypatch.setattr(ur.legacy, "cfg_for", lambda brand: cfg)
    monkeypatch.setattr(ur.legacy, "save_message", lambda *a, **k: saved_messages.append(a))
    monkeypatch.setattr(ur.legacy, "state_get", lambda *a, **k: state)
    monkeypatch.setattr(ur.legacy, "ai", None)

    reply = ur.process_with_ai("rozmary", "sender-1", "А скільки коштує дизайн?")

    assert "Манікюр" in reply
    assert "2026-10-14" in reply
    assert "11:00" in reply
    assert state["state"] == "WAITING_ADMIN_CONFIRMATION"
    assert state["appointment_id"] == 101
    assert len(saved_messages) == 2

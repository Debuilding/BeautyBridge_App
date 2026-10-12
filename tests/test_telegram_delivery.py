import requests

import main


class FakeResponse:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


def test_telegram_retries_transient_server_error(monkeypatch):
    calls = []
    responses = iter([FakeResponse(500, "temporary"), FakeResponse(200, "ok")])

    monkeypatch.setattr(main, "TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setattr(main, "TELEGRAM_RETRY_BACKOFF_SECONDS", 0)
    monkeypatch.setattr(main.requests, "post", lambda *args, **kwargs: calls.append(args) or next(responses))

    assert main.telegram({"telegram_chat_id": "123"}, "hello") is True
    assert len(calls) == 2


def test_telegram_does_not_retry_permanent_client_error(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setattr(main, "TELEGRAM_RETRY_BACKOFF_SECONDS", 0)
    monkeypatch.setattr(main.requests, "post", lambda *args, **kwargs: calls.append(args) or FakeResponse(401, "unauthorized"))

    assert main.telegram({"telegram_chat_id": "123"}, "hello") is False
    assert len(calls) == 1


def test_telegram_retries_network_error(monkeypatch):
    calls = []
    def fake_post(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            raise requests.ConnectionError("temporary network")
        return FakeResponse(200, "ok")

    monkeypatch.setattr(main, "TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setattr(main, "TELEGRAM_RETRY_BACKOFF_SECONDS", 0)
    monkeypatch.setattr(main.requests, "post", fake_post)

    assert main.telegram({"telegram_chat_id": "123"}, "hello") is True
    assert len(calls) == 2


def test_telegram_returns_false_when_configuration_is_missing(monkeypatch):
    monkeypatch.setattr(main, "TELEGRAM_BOT_TOKEN", "")
    assert main.telegram({"telegram_chat_id": "123"}, "hello") is False


class FakePollingResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {"ok": True, "result": []}

    def json(self):
        return self._payload


def test_telegram_poll_retries_transient_server_error(monkeypatch):
    import universal_runtime as ur

    calls = []
    responses = iter([
        FakePollingResponse(502),
        FakePollingResponse(200),
    ])
    monkeypatch.setattr(ur.config, "TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setattr(ur.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        ur.requests,
        "get",
        lambda *args, **kwargs: calls.append((args, kwargs)) or next(responses),
    )

    ur.telegram_poll_once()

    assert len(calls) == 2


def test_telegram_poll_retries_network_error(monkeypatch):
    import universal_runtime as ur

    calls = []

    def fake_get(*args, **kwargs):
        calls.append((args, kwargs))
        if len(calls) == 1:
            raise requests.Timeout("temporary timeout")
        return FakePollingResponse(200)

    monkeypatch.setattr(ur.config, "TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setattr(ur.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(ur.requests, "get", fake_get)

    ur.telegram_poll_once()

    assert len(calls) == 2


def test_telegram_poll_does_not_retry_client_error(monkeypatch):
    import universal_runtime as ur

    calls = []
    monkeypatch.setattr(ur.config, "TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setattr(
        ur.requests,
        "get",
        lambda *args, **kwargs: calls.append((args, kwargs)) or FakePollingResponse(401),
    )

    ur.telegram_poll_once()

    assert len(calls) == 1



def test_admin_time_confirmation_keeps_request_pending_if_instagram_delivery_fails(monkeypatch):
    import universal_runtime as ur

    cfg = {"name": "Rozmary"}
    row = (
        17, "rozmary", "instagram-user", "Client", "+380000000000",
        "Манікюр", "2026-10-20", "11:00", "Майстер", "pending_manual_confirmation",
        0, None, None,
    )
    appointment_updates = []
    state_updates = []

    monkeypatch.setattr(ur, "_find_appointment_brand", lambda _appointment_id: (cfg, row))
    monkeypatch.setattr(
        ur.legacy,
        "instagram_send",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("temporary Instagram failure")),
    )
    monkeypatch.setattr(
        ur,
        "update_appointment",
        lambda appointment_id, **kwargs: appointment_updates.append((appointment_id, kwargs)),
    )
    monkeypatch.setattr(
        ur,
        "strict_state_set",
        lambda *args, **kwargs: state_updates.append((args, kwargs)),
    )

    result = ur._handle_admin_callback("bb:time_ok:17")

    assert "Заявка залишилася на перевірці" in result
    assert appointment_updates == []
    assert state_updates == []



def test_admin_unavailable_time_keeps_request_pending_if_instagram_delivery_fails(monkeypatch):
    import universal_runtime as ur

    cfg = {"name": "Rozmary"}
    row = (
        18, "rozmary", "instagram-user", "Client", "+380000000000",
        "Манікюр", "2026-10-20", "11:00", "Майстер", "pending_manual_confirmation",
        0, None, None,
    )
    appointment_updates = []
    state_updates = []

    monkeypatch.setattr(ur, "_find_appointment_brand", lambda _appointment_id: (cfg, row))
    monkeypatch.setattr(
        ur.legacy,
        "instagram_send",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("temporary Instagram failure")),
    )
    monkeypatch.setattr(
        ur,
        "update_appointment",
        lambda appointment_id, **kwargs: appointment_updates.append((appointment_id, kwargs)),
    )
    monkeypatch.setattr(
        ur,
        "strict_state_set",
        lambda *args, **kwargs: state_updates.append((args, kwargs)),
    )

    result = ur._handle_admin_callback("bb:time_busy:18")

    assert "Заявка залишилася на перевірці" in result
    assert appointment_updates == []
    assert state_updates == []



def test_client_acceptance_requests_prepayment_when_required(monkeypatch):
    import universal_runtime as ur

    cfg = {
        "name": "Rozmary",
        "prepayment_required": True,
        "prepayment_amount": 200,
        "card_number": "4111111111111111",
        "card_name": "Rozmary",
    }
    state = {
        "state": ur.BotState.WAITING_CLIENT_TIME_CONFIRMATION.value,
        "appointment_id": 17,
        "name": "Client",
    }
    row = (
        17, "rozmary", "instagram-user", "Client", "+380000000000",
        "Манікюр", "2026-10-20", "11:00", "Майстер",
        "waiting_client_confirmation", 0, 0, None,
    )
    appointment_updates = []
    state_updates = []
    admin_messages = []

    monkeypatch.setattr(ur, "audit_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(ur.legacy, "cfg_for", lambda _brand: cfg)
    monkeypatch.setattr(ur.legacy, "save_message", lambda *args, **kwargs: None)
    monkeypatch.setattr(ur.legacy, "state_get", lambda *_args, **_kwargs: state)
    monkeypatch.setattr(ur, "appointment_row", lambda _appointment_id: row)
    monkeypatch.setattr(
        ur, "update_appointment",
        lambda appointment_id, **kwargs: appointment_updates.append((appointment_id, kwargs)),
    )
    monkeypatch.setattr(
        ur, "strict_state_set",
        lambda *args, **kwargs: state_updates.append((args, kwargs)),
    )
    monkeypatch.setattr(
        ur, "send_admin_telegram",
        lambda _cfg, message: admin_messages.append(message),
    )

    reply = ur.process_with_ai("rozmary", "instagram-user", "Так")

    assert appointment_updates == [(17, {"status": "booked_awaiting_payment"})]
    assert state_updates[0][1]["state"] == ur.BotState.WAITING_PAYMENT.value
    assert state_updates[0][1]["appointment_id"] == 17
    assert "передоплату 200 грн" in reply
    assert "4111111111111111" in reply
    assert "ваш запис підтверджено" not in reply.lower()
    assert "очікується передоплата" in admin_messages[0].lower()
    assert ur.can_transition(
        ur.BotState.WAITING_CLIENT_TIME_CONFIRMATION.value,
        ur.BotState.WAITING_PAYMENT.value,
    )


def test_client_acceptance_confirms_immediately_when_prepayment_not_required(monkeypatch):
    import universal_runtime as ur

    cfg = {"name": "Rozmary", "prepayment_required": False, "address": "Lviv"}
    state = {
        "state": ur.BotState.WAITING_CLIENT_TIME_CONFIRMATION.value,
        "appointment_id": 19,
        "name": "Client",
    }
    row = (
        19, "rozmary", "instagram-user", "Client", "+380000000000",
        "Манікюр", "2026-10-20", "11:00", "Майстер",
        "waiting_client_confirmation", 0, 0, None,
    )
    appointment_updates = []
    state_updates = []
    admin_messages = []

    monkeypatch.setattr(ur, "audit_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(ur.legacy, "cfg_for", lambda _brand: cfg)
    monkeypatch.setattr(ur.legacy, "save_message", lambda *args, **kwargs: None)
    monkeypatch.setattr(ur.legacy, "state_get", lambda *_args, **_kwargs: state)
    monkeypatch.setattr(ur, "appointment_row", lambda _appointment_id: row)
    monkeypatch.setattr(
        ur, "update_appointment",
        lambda appointment_id, **kwargs: appointment_updates.append((appointment_id, kwargs)),
    )
    monkeypatch.setattr(
        ur, "strict_state_set",
        lambda *args, **kwargs: state_updates.append((args, kwargs)),
    )
    monkeypatch.setattr(
        ur, "send_admin_telegram",
        lambda _cfg, message: admin_messages.append(message),
    )

    reply = ur.process_with_ai("rozmary", "instagram-user", "Так")

    assert appointment_updates == [(19, {"status": "confirmed"})]
    assert state_updates[0][1]["state"] == ur.BotState.BOOKED_CONFIRMED.value
    assert "ваш запис підтверджено" in reply.lower()
    assert "Адреса: Lviv" in reply
    assert "очікується передоплата" not in admin_messages[0].lower()



def test_admin_manual_confirmation_only_offers_time_and_waits_for_client_acceptance(monkeypatch):
    import universal_runtime as ur

    cfg = {"name": "Rozmary"}
    row = (
        21, "rozmary", "instagram-user", "Client", "+380000000000",
        "Манікюр", "2026-10-20", "11:00", "Майстер",
        "pending_manual_confirmation", 0, 0, None,
    )
    appointment_updates = []
    state_updates = []
    sent_messages = []

    monkeypatch.setattr(ur, "appointment_row", lambda _appointment_id: row)
    monkeypatch.setattr(ur.legacy, "cfg_for", lambda _brand: cfg)
    monkeypatch.setattr(ur, "audit_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        ur.legacy, "instagram_send",
        lambda _cfg, _sender, message: sent_messages.append(message),
    )
    monkeypatch.setattr(
        ur, "update_appointment",
        lambda appointment_id, **kwargs: appointment_updates.append((appointment_id, kwargs)),
    )
    monkeypatch.setattr(
        ur, "strict_state_set",
        lambda *args, **kwargs: state_updates.append((args, kwargs)),
    )

    with ur.legacy.app.app_context():
        response = ur.confirm_manual_booking(21)

    assert response.get_json()["status"] == "waiting_client_confirmation"
    assert appointment_updates == [(21, {"status": "waiting_client_confirmation"})]
    assert state_updates[0][1]["state"] == ur.BotState.WAITING_CLIENT_TIME_CONFIRMATION.value
    assert "Напишіть «Так»" in sent_messages[0]
    assert "запис підтверджено" not in sent_messages[0].lower()


def test_admin_manual_confirmation_keeps_request_pending_if_offer_delivery_fails(monkeypatch):
    import universal_runtime as ur

    cfg = {"name": "Rozmary"}
    row = (
        22, "rozmary", "instagram-user", "Client", "+380000000000",
        "Манікюр", "2026-10-20", "11:00", "Майстер",
        "pending_manual_confirmation", 0, 0, None,
    )
    appointment_updates = []
    state_updates = []

    monkeypatch.setattr(ur, "appointment_row", lambda _appointment_id: row)
    monkeypatch.setattr(ur.legacy, "cfg_for", lambda _brand: cfg)
    monkeypatch.setattr(ur, "audit_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        ur.legacy, "instagram_send",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("temporary Instagram failure")),
    )
    monkeypatch.setattr(
        ur, "update_appointment",
        lambda appointment_id, **kwargs: appointment_updates.append((appointment_id, kwargs)),
    )
    monkeypatch.setattr(
        ur, "strict_state_set",
        lambda *args, **kwargs: state_updates.append((args, kwargs)),
    )

    with ur.legacy.app.app_context():
        response, status_code = ur.confirm_manual_booking(22)

    assert status_code == 502
    assert "remains pending" in response.get_json()["error"]
    assert appointment_updates == []
    assert state_updates == []

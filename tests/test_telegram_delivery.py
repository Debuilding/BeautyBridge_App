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

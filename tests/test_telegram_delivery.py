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

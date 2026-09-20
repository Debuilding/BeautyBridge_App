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

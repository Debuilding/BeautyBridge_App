"""
Regression coverage for the "nails photo gets lost" gap found in production:
a client's pre-booking nails photo was read off the webhook payload but
never stored anywhere, so by the time the payment receipt arrived, staff
in Telegram only ever saw the receipt - never the nails photo the whole
requires_photo step exists for in the first place.

This covers:
  1. send_admin_telegram_album() sends a real Telegram sendMediaGroup call
     with both photos when given two URLs (and degrades sensibly for 1/0).
  2. _payment_receipt() actually pulls the previously-stored nails photo
     back out of state and includes it alongside the receipt, with full
     booking details in the caption - not just the receipt alone.

No real Telegram/network/DB calls - everything relevant is monkeypatched.
"""

import types

import universal_runtime as ur


class FakeResponse:
    status_code = 200


def test_send_admin_telegram_album_two_photos(monkeypatch):
    calls = []

    def fake_post(url, json=None, timeout=None):
        calls.append((url, json))
        return FakeResponse()

    monkeypatch.setattr(ur.config, "TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setattr(ur.config, "ADMIN_CHAT_ID", "12345")
    monkeypatch.setattr("requests.post", fake_post)

    ur.send_admin_telegram_album(
        {"telegram_chat_id": "999"},
        "Заявка від клієнта",
        ["https://example.com/nails.jpg", "https://example.com/receipt.jpg"],
    )

    assert len(calls) == 1
    url, payload = calls[0]
    assert url.endswith("/sendMediaGroup")
    assert payload["chat_id"] == "999"
    assert len(payload["media"]) == 2
    assert payload["media"][0]["media"] == "https://example.com/nails.jpg"
    assert payload["media"][0]["caption"].startswith("Заявка")
    assert payload["media"][1]["caption"] == ""  # caption only once, not duplicated


def test_send_admin_telegram_album_single_photo_uses_sendphoto(monkeypatch):
    calls = []
    monkeypatch.setattr(ur.config, "TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setattr(ur.config, "ADMIN_CHAT_ID", "12345")
    monkeypatch.setattr("requests.post", lambda url, json=None, timeout=None: calls.append((url, json)) or FakeResponse())

    ur.send_admin_telegram_album({}, "text", ["https://example.com/only.jpg", None])

    assert len(calls) == 1
    assert calls[0][0].endswith("/sendPhoto")


def test_payment_receipt_includes_previously_stored_nails_photo(monkeypatch):
    telegram_calls = []
    instagram_calls = []

    fake_state = {"nails_photo_url": "https://example.com/nails.jpg"}
    fake_row = (
        1, "rozmary", "sender-1", "Марія", "0501234567", "Манікюр комплекс",
        "2026-09-21", "14:00", "Світлана", "confirmed", 0, 0, None,
    )

    fake_legacy = types.SimpleNamespace(
        cfg_for=lambda brand: {"name": "Rozmary", "telegram_chat_id": "999"},
        state_get=lambda brand, sender: dict(fake_state),
        state_set=lambda brand, sender, **kw: fake_state.update(kw) or fake_state,
        instagram_send=lambda cfg, sender, text: instagram_calls.append(text),
        telegram=lambda cfg, text: telegram_calls.append(text),
    )

    monkeypatch.setattr(ur, "legacy", fake_legacy)
    monkeypatch.setattr(ur, "appointment_row", lambda appointment_id: fake_row)
    monkeypatch.setattr(ur, "update_appointment", lambda *a, **kw: None)

    album_calls = []
    monkeypatch.setattr(
        ur, "send_admin_telegram_album",
        lambda cfg, caption, photos: album_calls.append((cfg, caption, photos)),
    )

    ur._payment_receipt("rozmary", "sender-1", 1, "https://example.com/receipt.jpg")

    assert len(album_calls) == 1
    cfg, caption, photos = album_calls[0]
    assert photos == ["https://example.com/nails.jpg", "https://example.com/receipt.jpg"]
    assert "Марія" in caption
    assert "Світлана" in caption
    assert "2026-09-21" in caption

    assert len(instagram_calls) == 1
    assert "14:00" in instagram_calls[0]

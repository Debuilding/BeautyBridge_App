import json

from beautybridge_v3.channels.instagram import normalize_events, verify_signature
from beautybridge_v3.config import Settings, TenantConfig
from beautybridge_v3.conversation import ConversationEngine
from beautybridge_v3.crm import get_adapter
from beautybridge_v3.domain import BookingStatus
from beautybridge_v3.storage import Storage


def test_bookon_is_manual_only_and_does_not_query_provider():
    adapter = get_adapter("bookon")
    result = adapter.create_booking({"requested_date": "2099-09-23", "preferred_time": "10:00"})
    assert result.status == BookingStatus.PENDING_MANUAL_CONFIRMATION.value


def test_unknown_crm_is_not_implicitly_bookon():
    result = get_adapter("some-unknown-crm").create_booking({})
    assert result.status == "unsupported_crm"


def test_webhook_event_normalization_ignores_echo_and_non_text():
    payload = {"entry": [{"id": "page-1", "messaging": [
        {"sender": {"id": "user-1"}, "timestamp": 123, "message": {"mid": "m1", "text": "Hello"}},
        {"sender": {"id": "page-1"}, "timestamp": 124, "message": {"mid": "m2", "text": "echo", "is_echo": True}},
        {"sender": {"id": "user-2"}, "timestamp": 125, "message": {"mid": "m3", "attachments": []}},
    ]}]}
    assert normalize_events(payload) == [{"page_id": "page-1", "sender_id": "user-1", "event_id": "m1", "text": "Hello"}]


def test_meta_signature_verification():
    import hashlib, hmac
    body = b'{"object":"instagram"}'
    signature = "sha256=" + hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()
    assert verify_signature(body, signature, "test-secret")
    assert not verify_signature(body, signature, "wrong-secret")


def test_event_idempotency_and_tenant_isolation(tmp_path):
    db = Storage(str(tmp_path / "test.sqlite3"))
    payload = {"text": "hi"}
    assert db.enqueue_once("event-1", "salon-a", "sender-1", payload)
    assert not db.enqueue_once("event-1", "salon-a", "sender-1", payload)
    assert db.enqueue_once("event-2", "salon-b", "sender-1", payload)
    db.save_conversation("salon-a", "sender-1", {"details": {"service_id": "salon-a-service"}})
    db.save_conversation("salon-b", "sender-1", {"details": {"service_id": "salon-b-service"}})
    assert db.get_conversation("salon-a", "sender-1")["details"]["service_id"] == "salon-a-service"
    assert db.get_conversation("salon-b", "sender-1")["details"]["service_id"] == "salon-b-service"


def test_conversation_merge_preserves_known_fields_and_validates_inputs():
    engine = ConversationEngine()
    merged = engine.merge_extracted(
        {"service_id": "manicure", "requested_date": "2099-09-23", "preferred_time": "10:30"},
        {"client_name": "Olena", "client_phone": "+380501234567", "preferred_time": "not-a-time"},
    )
    assert merged["service_id"] == "manicure"
    assert merged["requested_date"] == "2099-09-23"
    assert merged["preferred_time"] == ""
    assert merged["client_name"] == "Olena"


def test_only_pending_manual_request_can_be_confirmed(tmp_path):
    db = Storage(str(tmp_path / "test.sqlite3"))
    request_id = db.create_booking_request("salon-a", "sender-1", {"service_id": "manicure"}, "pending_manual_confirmation")
    assert db.set_booking_status(request_id, "confirmed")
    assert not db.set_booking_status(request_id, "confirmed")


def test_tenant_configuration_defaults_to_manual():
    tenant = TenantConfig.from_dict("rozmary", {"name": "Rozmary"})
    assert tenant.crm_type == "manual"
    assert tenant.language == "uk"

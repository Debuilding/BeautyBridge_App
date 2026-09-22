import main
import universal_runtime as ur


def test_outbound_delivery_claim_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "outbound.db"))
    ur.migrate_database()

    key, status = ur._claim_outbound_delivery("test", "sender-1", [10, 11])
    assert key
    assert status == "SEND"

    same_key, same_status = ur._claim_outbound_delivery("test", "sender-1", [10, 11])
    assert same_key == key
    assert same_status == "IN_PROGRESS"

    ur._finish_outbound_delivery(key, True)

    sent_key, sent_status = ur._claim_outbound_delivery("test", "sender-1", [10, 11])
    assert sent_key == key
    assert sent_status == "ALREADY_SENT"


def test_failed_outbound_delivery_can_be_retried(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "outbound.db"))
    ur.migrate_database()

    key, status = ur._claim_outbound_delivery("test", "sender-1", [20])
    assert status == "SEND"

    ur._finish_outbound_delivery(key, False)

    retry_key, retry_status = ur._claim_outbound_delivery("test", "sender-1", [20])
    assert retry_key == key
    assert retry_status == "SEND"

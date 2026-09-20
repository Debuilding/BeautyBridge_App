import threading

import main
import universal_runtime as ur


def test_booking_idempotency_key_is_stable_and_distinct():
    a = ur.booking_idempotency_key("rozmary", "user1", "svc", "master", "2026-09-23", "16:00", "Maria", "380501234567")
    b = ur.booking_idempotency_key("rozmary", "user1", "svc", "master", "2026-09-23", "16:00", "Maria", "380501234567")
    c = ur.booking_idempotency_key("rozmary", "user1", "svc", "master", "2026-09-23", "16:30", "Maria", "380501234567")
    assert a == b
    assert a != c


def test_concurrent_booking_claim_allows_only_one_owner(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "claims.db"))
    ur.migrate_database()

    key = ur.booking_idempotency_key(
        "rozmary", "user1", "svc", "master", "2026-09-23", "16:00", "Maria", "380501234567"
    )
    results = []
    lock = threading.Lock()

    def worker():
        result = ur.claim_booking("rozmary", "user1", key)
        with lock:
            results.append(result)

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert sum(1 for item in results if item["claimed"]) == 1
    assert sum(1 for item in results if item["status"] == "IN_PROGRESS") == 12

    with main.db() as conn:
        rows = conn.execute(
            "SELECT booking_key, status FROM booking_claims WHERE booking_key=?",
            (key,),
        ).fetchall()
    assert rows == [(key, "IN_PROGRESS")]


def test_terminal_claim_is_returned_as_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "claims.db"))
    ur.migrate_database()

    key = ur.booking_idempotency_key(
        "rozmary", "user1", "svc", "master", "2026-09-23", "16:00", "Maria", "380501234567"
    )
    assert ur.claim_booking("rozmary", "user1", key)["claimed"]
    ur.finalize_booking_claim(key, "SUCCESS", appointment_id=42, crm_visit_id="crm-42")

    result = ur.claim_booking("rozmary", "user1", key)
    assert result["claimed"] is False
    assert result["status"] == "SUCCESS"
    assert result["appointment_id"] == 42
    assert result["crm_visit_id"] == "crm-42"

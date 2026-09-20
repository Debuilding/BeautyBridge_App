import json

import main
import universal_runtime as ur


def test_audit_event_stores_hashed_subject_not_raw_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "audit.db"))
    ur.migrate_database()

    event_id = ur.audit_event(
        "booking_test",
        brand="test",
        sender="raw-instagram-id",
        appointment_id=42,
        correlation_id="corr-1",
        actor="test",
        payload={"status": "SUCCESS", "phone": "0501234567"},
    )

    assert event_id is not None
    with main.db() as conn:
        row = conn.execute(
            "SELECT subject_hash,payload FROM audit_events WHERE id=?",
            (event_id,),
        ).fetchone()

    assert row[0]
    assert row[0] != "raw-instagram-id"
    assert "raw-instagram-id" not in row[1]


def test_audit_event_listing_filters_and_caps_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "audit.db"))
    ur.migrate_database()

    for index in range(5):
        ur.audit_event(
            "booking" if index % 2 else "message",
            brand="test",
            sender=f"user-{index}",
            payload={"index": index},
        )

    events = ur.list_audit_events(brand="test", event_type="booking", limit=999)
    assert len(events) == 2
    assert all(event["event_type"] == "booking" for event in events)
    assert all(isinstance(event["payload"], dict) for event in events)


def test_admin_audit_endpoint_requires_admin_token(monkeypatch):
    monkeypatch.setattr(main, "ADMIN_API_TOKEN", "secret")
    client = main.app.test_client()

    unauthorized = client.get("/admin/audit/events")
    assert unauthorized.status_code == 401

    authorized = client.get(
        "/admin/audit/events?limit=5",
        headers={"X-Admin-Token": "secret"},
    )
    assert authorized.status_code == 200
    assert "events" in authorized.get_json()

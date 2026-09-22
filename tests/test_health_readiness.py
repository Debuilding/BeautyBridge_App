import main


def test_health_live_is_always_available():
    client = main.app.test_client()
    response = client.get("/health/live")
    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


def test_health_ready_returns_503_when_critical_dependencies_are_missing(monkeypatch):
    monkeypatch.setattr(main, "ai", None)
    monkeypatch.setattr(main, "META_APP_SECRET", "")
    monkeypatch.setattr(main, "VERIFY_TOKEN", "")
    monkeypatch.setattr(main, "BRANDS", {})
    client = main.app.test_client()

    response = client.get("/health/ready")
    payload = response.get_json()

    assert response.status_code == 503
    assert payload["status"] == "not_ready"
    assert payload["checks"]["database"] is True
    assert payload["checks"]["ai"] is False
    assert payload["checks"]["meta_webhook"] is False
    assert payload["checks"]["enabled_brand"] is False
    assert payload["checks"]["instagram"] is False


def test_health_ready_returns_200_when_dependencies_are_present(monkeypatch):
    class FakeAI:
        pass

    monkeypatch.setattr(main, "ai", FakeAI())
    monkeypatch.setattr(main, "META_APP_SECRET", "test-secret")
    monkeypatch.setattr(main, "VERIFY_TOKEN", "test-verify")
    monkeypatch.setattr(
        main,
        "BRANDS",
        {
            "test": {
                "enabled": True,
                "page_id": "page-1",
                "page_access_token": "token-1",
            }
        },
    )
    client = main.app.test_client()

    response = client.get("/health/ready")
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["status"] == "ready"
    assert all(payload["checks"].values())

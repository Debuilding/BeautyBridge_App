import config
import main
import universal_runtime as ur


def _shared_brands():
    return {
        "rozmary": {
            "id": "rozmary",
            "enabled": True,
            "name": "Rozmary",
            "page_id": "page-shared",
            "page_access_token": "page-token",
            "address": "Rozmary address",
        },
        "space": {
            "id": "space",
            "enabled": True,
            "name": "Space",
            "page_id": "page-shared",
            "page_access_token": "page-token",
            "address": "Space address",
        },
    }


def test_first_message_prompts_for_location_and_does_not_guess(tmp_path, monkeypatch):
    monkeypatch.setenv("SHARED_INSTAGRAM_BRANDS", "rozmary,space")
    monkeypatch.setattr(config, "BRANDS", _shared_brands())
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "locations.db"))
    ur.migrate_database()

    brand, text, reply = ur.route_location_message("rozmary", "sender-1", "Привіт!")

    assert brand == "rozmary"
    assert text == ""
    assert "Rozmary" in reply
    assert "Space" in reply
    assert ur.get_selected_location("sender-1", "page-shared") is None


def test_selected_location_persists_and_routes_future_messages(tmp_path, monkeypatch):
    monkeypatch.setenv("SHARED_INSTAGRAM_BRANDS", "rozmary,space")
    monkeypatch.setattr(config, "BRANDS", _shared_brands())
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "locations.db"))
    ur.migrate_database()

    brand, text, reply = ur.route_location_message("rozmary", "sender-1", "Space")
    assert brand == "space"
    assert text == ""
    assert reply and "Space" in reply
    assert ur.get_selected_location("sender-1", "page-shared") == "space"

    # The location lives in its own durable table, not in expiring conversation state.
    main.state_set("space", "sender-1", state="START")
    brand, text, reply = ur.route_location_message("rozmary", "sender-1", "Хочу манікюр")
    assert (brand, text, reply) == ("space", "Хочу манікюр", None)


def test_client_can_switch_location_in_conversation(tmp_path, monkeypatch):
    monkeypatch.setenv("SHARED_INSTAGRAM_BRANDS", "rozmary,space")
    monkeypatch.setattr(config, "BRANDS", _shared_brands())
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "locations.db"))
    ur.migrate_database()
    ur.set_selected_location("sender-1", "rozmary")

    brand, text, reply = ur.route_location_message(
        "rozmary", "sender-1", "Space, хочу записатися на манікюр"
    )

    assert brand == "space"
    assert "манікюр" in text
    assert "Space" not in text
    assert reply is None
    assert ur.get_selected_location("sender-1", "page-shared") == "space"


def test_shared_instagram_credentials_are_inherited_only_when_explicitly_enabled(monkeypatch):
    monkeypatch.setattr(
        config,
        "load_client_blueprints",
        lambda: {
            "rozmary": {"enabled": True, "name": "Rozmary"},
            "space": {"enabled": True, "name": "Space"},
        },
    )
    monkeypatch.setenv("SHARED_INSTAGRAM_BRANDS", "rozmary,space")
    monkeypatch.delenv("SPACE_ENABLED", raising=False)
    monkeypatch.setenv("ROZMARY_PAGE_ID", "page-shared")
    monkeypatch.setenv("ROZMARY_PAGE_ACCESS_TOKEN", "test-page-token")
    for key in ("SPACE_PAGE_ID", "SPACE_PAGE_ACCESS_TOKEN"):
        monkeypatch.delenv(key, raising=False)

    brands = config.build_brands()

    assert brands["rozmary"]["page_id"] == "page-shared"
    assert brands["space"]["page_id"] == "page-shared"
    assert brands["space"]["page_access_token"] == "test-page-token"

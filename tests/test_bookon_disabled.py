"""Regression tests: Bookon is never an automatic booking backend."""

import config


def test_bookon_configuration_is_forced_to_table_mode(monkeypatch):
    monkeypatch.setattr(
        config,
        "load_client_blueprints",
        lambda: {
            "rozmary": {
                "name": "Rozmary",
                "crm_type": "bookon",
                "booking_mode": "crm",
                "crm": {"type": "bookon"},
            }
        },
    )
    for key in (
        "ROZMARY_CRM_TYPE",
        "ROZMARY_BOOKING_MODE",
        "ROZMARY_ENABLED",
        "ROZMARY_LANGUAGE",
    ):
        monkeypatch.delenv(key, raising=False)

    brand = config.build_brands()["rozmary"]
    assert brand["configured_crm_type"] == "bookon"
    assert brand["booking_mode"] == "table"
    assert brand["booking_backend"] == "table"
    assert brand["crm_type"] == "manual"
    assert brand["crm"]["type"] == "manual"

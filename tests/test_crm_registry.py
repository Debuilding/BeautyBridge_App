from crm.registry import available_crm_types, get_crm_adapter


def test_unknown_crm_never_becomes_bookon():
    adapter = get_crm_adapter({"crm_type": "some_future_crm", "crm": {"type": "some_future_crm"}})
    assert adapter.type_name == "unsupported"


def test_bookon_is_not_an_automatic_connector():
    adapter = get_crm_adapter({"crm_type": "bookon", "crm": {"type": "bookon"}})
    assert adapter.type_name == "unsupported"


def test_onboarding_lists_future_connectors():
    types = {item["type"]: item for item in available_crm_types()}
    assert "altegio" in types
    assert "yclients" in types
    assert types["altegio"]["implemented"] is False


def test_bookon_is_listed_as_unimplemented_manual_fallback():
    types = {item["type"]: item for item in available_crm_types()}
    assert types["bookon"]["implemented"] is False
    assert types["bookon"]["mode"] == "manual_fallback"

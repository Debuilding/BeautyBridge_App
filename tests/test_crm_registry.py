from crm.registry import available_crm_types, get_crm_adapter


def test_unknown_crm_never_becomes_bookon():
    adapter = get_crm_adapter({"crm_type": "some_future_crm", "crm": {"type": "some_future_crm"}})
    assert adapter.type_name == "unsupported"


def test_bookon_is_explicit_connector():
    adapter = get_crm_adapter({"crm_type": "bookon", "crm": {"type": "bookon"}})
    assert adapter.type_name == "bookon"


def test_onboarding_lists_future_connectors():
    types = {item["type"]: item for item in available_crm_types()}
    assert "altegio" in types
    assert "yclients" in types
    assert types["altegio"]["implemented"] is False

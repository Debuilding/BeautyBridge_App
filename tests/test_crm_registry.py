from crm.registry import available_crm_types, get_crm_adapter


def test_unknown_crm_uses_unsupported_adapter():
    adapter = get_crm_adapter({"crm_type": "some_future_crm", "crm": {"type": "some_future_crm"}})
    assert adapter.type_name == "unsupported"


def test_registry_exposes_manual_mode_only_for_current_flow():
    types = {item["type"]: item for item in available_crm_types()}
    assert set(types) == {"manual", "table"}
    assert types["manual"]["implemented"] is True
    assert types["table"]["implemented"] is True

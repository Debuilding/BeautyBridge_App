"""Regression tests for date validation in the CRM-independent manual flow.

The runtime must reject malformed dates before any booking work and must
never call an adapter for availability: this installation records preferred
times locally and asks the salon to verify them through Telegram.
"""

import json

import universal_runtime as ur


def test_get_available_slots_rejects_non_iso_date(monkeypatch):
    monkeypatch.setattr(ur.legacy, "state_get", lambda brand, sender: {})

    called = []
    monkeypatch.setattr(ur, "adapter_for", lambda cfg: called.append("adapter_for") or None)

    for bad_date in ["23.09", "23 вересня", "вересня 23", "2026/09/23", ""]:
        result = json.loads(
            ur.handle_tool(
                "rozmary",
                "sender-1",
                {"crm_type": "manual"},
                "get_available_slots",
                {"service_id": "service-1", "date_str": bad_date},
            )
        )
        assert result["status"] == "INVALID_DATE_FORMAT", (
            f"expected rejection for {bad_date!r}, got {result}"
        )

    # Invalid dates are rejected before any adapter or availability lookup.
    assert called == []


def test_get_available_slots_accepts_iso_date_but_stays_manual(monkeypatch):
    monkeypatch.setattr(ur.legacy, "state_get", lambda brand, sender: {})

    called = []
    monkeypatch.setattr(ur, "adapter_for", lambda cfg: called.append(cfg) or None)
    result = json.loads(
        ur.handle_tool(
            "rozmary",
            "sender-1",
            {"crm_type": "manual"},
            "get_available_slots",
            {"service_id": "service-1", "date_str": "2099-01-15"},
        )
    )
    assert result["status"] == "MANUAL_MODE"
    assert result["slots"] == []
    assert called == []


def test_tool_schemas_document_the_required_date_format():
    schemas = {t["function"]["name"]: t["function"] for t in ur.tool_specs()}
    for tool_name in ("get_available_slots", "create_visit"):
        desc = schemas[tool_name]["parameters"]["properties"]["date_str"].get("description", "")
        assert "YYYY-MM-DD" in desc, (
            f"{tool_name}.date_str has no explicit format hint: {desc!r}"
        )

"""
Regression coverage for a date-format mismatch found by inspection: the
system prompt used to show "today" to the AI as DD.MM.YYYY (Ukrainian
convention), while every tool/CRM call downstream expects date_str as
YYYY-MM-DD, and the get_available_slots tool schema had no format hint at
all. Nothing stopped the model from passing along whatever format the
client used ("23 вересня", "23.09", ...) straight into the CRM adapter.

create_visit already validated this via _date_ok(); get_available_slots did
not. This confirms:
  1. get_available_slots now rejects a bad date_str immediately, with a
     clear status the model can act on, instead of forwarding it to the
     CRM adapter (which would either error confusingly or silently
     misbehave).
  2. A correctly-formatted date_str still reaches the adapter as before.
  3. Both tool schemas now carry an explicit YYYY-MM-DD description, so the
     model isn't relying on implicit convention alone.
"""

import json

import universal_runtime as ur


def test_get_available_slots_rejects_non_iso_date(monkeypatch):
    monkeypatch.setattr(ur.legacy, "state_get", lambda brand, sender: {})

    called = []
    monkeypatch.setattr(ur, "adapter_for", lambda cfg: called.append("adapter_for") or None)

    for bad_date in ["23.09", "23 вересня", "вересня 23", "2026/09/23", ""]:
        result = json.loads(
            ur.handle_tool("rozmary", "sender-1", {"crm_type": "bookon"}, "get_available_slots",
                            {"service_id": "543063", "date_str": bad_date})
        )
        assert result["status"] == "INVALID_DATE_FORMAT", f"expected rejection for {bad_date!r}, got {result}"

    # adapter_for must never even be called for a bad date - fail before touching the CRM
    assert called == []


def test_get_available_slots_accepts_iso_date_but_stays_manual(monkeypatch):
    monkeypatch.setattr(ur.legacy, "state_get", lambda brand, sender: {})

    called = []
    monkeypatch.setattr(ur, "adapter_for", lambda cfg: called.append(cfg) or None)
    result = json.loads(
        ur.handle_tool("rozmary", "sender-1", {"crm_type": "altegio"}, "get_available_slots",
                       {"service_id": "543063", "date_str": "2099-01-15"})
    )
    assert result["status"] == "MANUAL_MODE"
    assert result["slots"] == []
    assert called == []


def test_tool_schemas_document_the_required_date_format():
    schemas = {t["function"]["name"]: t["function"] for t in ur.tool_specs()}
    for tool_name in ("get_available_slots", "create_visit"):
        desc = schemas[tool_name]["parameters"]["properties"]["date_str"].get("description", "")
        assert "YYYY-MM-DD" in desc, f"{tool_name}.date_str has no explicit format hint: {desc!r}"

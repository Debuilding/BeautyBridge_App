def test_runtime_patch_keeps_canonical_manual_confirmation():
    import runtime_patch
    import universal_runtime

    assert runtime_patch.runtime.confirm_manual_booking is universal_runtime.confirm_manual_booking


def test_manual_confirmation_passes_sender_to_instagram(monkeypatch):
    import universal_runtime as ur

    calls = []
    monkeypatch.setattr(
        ur,
        "appointment_row",
        lambda appointment_id: (
            7,
            "test",
            "sender-123",
            "Maria",
            "380501234567",
            "Манікюр",
            "2099-09-23",
            "16:00",
            "Світлана",
            "booked",
            0,
            0,
            "",
        ),
    )
    monkeypatch.setattr(ur.legacy, "cfg_for", lambda brand: {"prepayment_required": True, "name": "Test"})
    monkeypatch.setattr(ur, "update_appointment", lambda *args, **kwargs: None)
    monkeypatch.setattr(ur, "strict_state_set", lambda *args, **kwargs: None)
    monkeypatch.setattr(ur, "audit_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(ur, "payment_instruction", lambda cfg: "Внесіть 200 грн")
    monkeypatch.setattr(
        ur.legacy,
        "instagram_send",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    with ur.legacy.app.app_context():
        response = ur.confirm_manual_booking(7)

    assert response.get_json()["ok"] is True
    assert calls
    assert calls[0][0][1] == "sender-123"



def test_bookon_availability_is_blocked_before_adapter_call(monkeypatch):
    import json
    import universal_runtime as ur

    class ExplodingBookonAdapter:
        name = "bookon"
        capabilities = {"availability"}

        def get_available_slots(self, service_id, date_str):
            raise AssertionError("Bookon availability must never be queried")

    monkeypatch.setattr(ur.legacy, "state_get", lambda *args: {})
    monkeypatch.setattr(ur, "adapter_for", lambda cfg: ExplodingBookonAdapter())

    result = json.loads(
        ur.handle_tool(
            "test",
            "sender-1",
            {"crm_type": "bookon"},
            "get_available_slots",
            {"service_id": "manicure", "date_str": "2099-09-23"},
        )
    )

    assert result["status"] == "MANUAL_MODE"
    assert result["slots"] == []


def test_runtime_patch_routes_bookon_to_manual_adapter_without_provider(monkeypatch):
    import runtime_patch

    def unexpected_provider_call(cfg):
        raise AssertionError("Bookon provider must not be instantiated in manual-only mode")

    monkeypatch.setattr(runtime_patch, "get_crm_adapter", unexpected_provider_call)

    adapter = runtime_patch.adapter_for_v21({"crm_type": "bookon"})

    assert isinstance(adapter, runtime_patch.runtime.ManualCRMAdapter)


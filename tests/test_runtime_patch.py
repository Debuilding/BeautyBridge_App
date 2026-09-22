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

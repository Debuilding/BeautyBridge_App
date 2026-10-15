from flow_guard import guard_ai_reply


def test_handoff_claim_is_blocked_without_selected_service():
    cfg = {"services": {"svc1": {"name": "Манікюр комплекс"}}}
    state = {
        "state": "COLLECTING",
        "date": "2026-10-17",
        "time": "після 14:00",
        "photo": True,
    }
    reply = guard_ai_reply(
        cfg,
        state,
        "Чудово, я передам ваш запит адміністратору, щоб уточнити доступність часу після 14:00 в суботу.",
    )
    assert "яку саме послугу" in reply.lower()
    assert "манікюр комплекс" in reply.lower()
    assert "адміністратору" not in reply.lower()


def test_missing_service_does_not_override_unrelated_reply():
    reply = guard_ai_reply(
        {"services": {}},
        {"state": "COLLECTING"},
        "Доброго дня! Чим можу допомогти?",
    )
    assert reply == "Доброго дня! Чим можу допомогти?"

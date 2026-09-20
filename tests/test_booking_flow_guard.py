import universal_runtime as ur
from flow_guard import guard_ai_reply, next_flow_reply


CFG = {
    "name": "Test Salon",
    "services": {
        "svc1": {"name": "Манікюр комплекс", "duration": 120, "requires_photo": True},
    },
    "masters": {"master1": "Світлана"},
    "master_services": {"master1": ["svc1"]},
}


def state_ready(**extra):
    state = {
        "state": "COLLECTING",
        "service_id": "svc1",
        "date": "2099-09-23",
        "time": "16:00",
        "employee_id": "master1",
        "name": "Марія",
        "phone": "380501234567",
        "photo": True,
    }
    state.update(extra)
    return state


def test_guard_blocks_fake_booking_confirmation_without_create_visit():
    reply = "Чудово! Я вас записала на 23 вересня о 16:00 ✅"
    guarded = guard_ai_reply(CFG, state_ready(), reply, [])
    assert "записала" not in guarded.lower()
    assert "передоплат" not in guarded.lower()
    assert guarded == next_flow_reply(CFG, state_ready())


def test_guard_allows_confirmation_after_success():
    reply = "Готово, запис підтверджено на 23 вересня о 16:00 ✅"
    guarded = guard_ai_reply(
        CFG,
        state_ready(state="WAITING_PAYMENT"),
        reply,
        [{"name": "create_visit", "status": "SUCCESS"}],
    )
    assert guarded == reply


def test_guard_manual_fallback_never_claims_confirmed_booking():
    reply = "Готово, запис підтверджено ✅"
    guarded = guard_ai_reply(
        CFG,
        state_ready(),
        reply,
        [{"name": "create_visit", "status": "MANUAL_FALLBACK"}],
    )
    assert "підтверджено" not in guarded.lower()
    assert "адміністратору" in guarded.lower()


def test_guard_blocks_premature_payment_request_after_tool_failure():
    reply = "Чудово! Запис готовий, внесіть передоплату 200 грн."
    guarded = guard_ai_reply(
        CFG,
        state_ready(),
        reply,
        [{"name": "create_visit", "status": "VALIDATION_ERROR"}],
    )
    assert "передоплату" not in guarded.lower()
    assert "ім'я" not in guarded.lower()


def test_photo_is_a_hard_gate_for_required_photo_service():
    state = state_ready(photo=False, name="", phone="")
    assert "фото" in next_flow_reply(CFG, state).lower()


def test_create_visit_cannot_use_unpersisted_values():
    state = state_ready(name="", phone="")
    args = {
        "service_id": "svc1",
        "employee_id": "master1",
        "date_str": "2099-09-23",
        "time_str": "16:00",
        "name": "Марія",
        "phone": "0501234567",
    }
    ok, message, _cleaned = ur.validate_booking(CFG, state, args)
    assert not ok
    assert "поле name" in message.lower()

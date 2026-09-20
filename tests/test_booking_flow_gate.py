"""
Deterministic, server-side booking-flow gate tests. These exist because the
AI could previously skip the photo step, use stale name/phone pulled from
conversation history/tool-call arguments instead of confirmed state, or
just claim "Записую вас..." in its free-form reply without create_visit
ever returning SUCCESS. None of this is prevented by prompt wording alone -
these tests check the actual server-side gate: booking_next_required_step(),
enforce_flow_gate(), and handle_tool()'s create_visit branch.
"""

import json

import universal_runtime as ur


CFG = {
    "name": "Rozmary",
    "language": "uk",
    "crm_type": "bookon",
    "prepayment_required": True,
    "services": {
        "543063": {"name": "Манікюр комплекс гель", "duration": 95, "requires_photo": True},
    },
    "masters": {"36645": "Світлана"},
    "master_services": {},
}


def _full_state(**overrides):
    state = {
        "service_id": "543063",
        "date": "2099-01-15",
        "time": "10:00",
        "employee_id": "36645",
        "photo": True,
        "name": "Марія",
        "phone": "380501234567",
    }
    state.update(overrides)
    return state


# 1. Missing photo


def test_gate_reports_need_photo_when_required_and_missing():
    state = _full_state(photo=False)
    assert ur.booking_next_required_step(CFG, state) == "need_photo"


def test_reply_overridden_when_photo_missing():
    state = _full_state(photo=False)
    reply = ur.enforce_flow_gate(CFG, state, "Записую вас на манікюр, все готово!")
    assert reply != "Записую вас на манікюр, все готово!"
    assert "фото" in reply.lower()


# 2. Missing name/phone


def test_gate_reports_need_contact_when_missing():
    state = _full_state(name="", phone="")
    assert ur.booking_next_required_step(CFG, state) == "need_contact"

    state2 = _full_state(phone="123")  # too short to be a valid phone
    assert ur.booking_next_required_step(CFG, state2) == "need_contact"


def test_reply_overridden_when_contact_missing():
    state = _full_state(name="", phone="")
    reply = ur.enforce_flow_gate(CFG, state, "Ваш запис підтверджено!")
    assert "ім'я" in reply.lower() or "телефон" in reply.lower()


# 3. create_visit blocked without photo, adapter never touched


def test_create_visit_blocked_without_photo(monkeypatch):
    called = []
    monkeypatch.setattr(ur, "adapter_for", lambda cfg: called.append("adapter_for") or None)
    monkeypatch.setattr(ur.legacy, "state_get", lambda brand, sender: _full_state(photo=False))

    result = json.loads(
        ur.handle_tool(
            "rozmary", "sender-1", CFG, "create_visit",
            {"service_id": "543063", "date_str": "2099-01-15", "time_str": "10:00",
             "employee_id": "36645", "name": "Марія", "phone": "380501234567"},
        )
    )
    assert result["status"] == "VALIDATION_ERROR"
    assert "фото" in result["message"].lower()
    assert called == []  # never even reached the CRM adapter


# 4. AI cannot bluff a false "success" past the server gate


def test_false_success_claim_is_overridden_not_trusted(monkeypatch):
    # AI tries to claim success while state shows the flow isn't actually done.
    state = _full_state(photo=False)
    reply = ur.enforce_flow_gate(
        CFG, state, "Вітаю! Вас успішно записано на манікюр, чекаємо!"
    )
    assert "успішно записано" not in reply
    assert "фото" in reply.lower()


def test_create_visit_cannot_be_forced_by_ai_supplied_args(monkeypatch):
    """Even if the AI's tool-call args claim a name/phone/photo state is
    missing, the server only trusts state - args are ignored entirely."""
    calls = []

    class FakeAdapter:
        def is_slot_available(self, *a, **kw):
            return True

        def create_booking(self, *a, **kw):
            calls.append(a)
            return "crm-123"

    monkeypatch.setattr(ur, "adapter_for", lambda cfg: FakeAdapter())
    monkeypatch.setattr(ur.legacy, "state_get", lambda brand, sender: _full_state(photo=False, name="", phone=""))
    monkeypatch.setattr(ur.legacy, "create_local_appointment", lambda *a, **kw: 1)
    monkeypatch.setattr(ur, "strict_state_set", lambda *a, **kw: None)
    monkeypatch.setattr(ur.legacy, "telegram", lambda *a, **kw: None)

    # AI lies in its own args: claims a name/phone that were never confirmed via remember_booking
    result = json.loads(
        ur.handle_tool(
            "rozmary", "sender-1", CFG, "create_visit",
            {"service_id": "543063", "date_str": "2099-01-15", "time_str": "10:00",
             "employee_id": "36645", "name": "Fake Name", "phone": "380999999999"},
        )
    )
    assert result["status"] == "VALIDATION_ERROR"
    assert calls == []  # CRM was never actually called with the AI's made-up values


# 5. SUCCESS with prepayment_required -> payment instructions enforced


def test_payment_instructions_enforced_after_success(monkeypatch):
    monkeypatch.setattr(ur, "payment_instruction", lambda cfg: "Оплатіть 200 грн на картку 0000")

    state = _full_state()
    state["state"] = ur.BotState.WAITING_PAYMENT.value
    state["appointment_id"] = 42

    reply = ur.enforce_flow_gate(CFG, state, "Дякуємо, чекаємо вас!")
    assert "Оплатіть 200 грн" in reply
    assert "Дякуємо, чекаємо вас!" in reply  # original reply preserved, not replaced


def test_payment_instructions_not_duplicated_if_already_present(monkeypatch):
    monkeypatch.setattr(ur, "payment_instruction", lambda cfg: "Оплатіть 200 грн на картку 0000")

    state = _full_state()
    state["state"] = ur.BotState.WAITING_PAYMENT.value
    state["appointment_id"] = 42

    reply = ur.enforce_flow_gate(CFG, state, "Дякуємо! Оплатіть 200 грн на картку 0000, будь ласка.")
    assert reply.count("Оплатіть 200 грн") == 1


# 6. Stale conversation-history data never substitutes for state


def test_stale_args_do_not_fill_missing_state_fields(monkeypatch):
    """Regression: create_visit used to pull name/phone straight from the
    AI's tool-call args, which could echo old values from earlier in the
    conversation even if state was never actually updated via
    remember_booking (e.g. client changed their mind, or the AI
    misremembered). Now only state counts."""
    monkeypatch.setattr(ur.legacy, "state_get", lambda brand, sender: _full_state(name="", phone=""))

    result = json.loads(
        ur.handle_tool(
            "rozmary", "sender-1", CFG, "create_visit",
            {
                "service_id": "543063", "date_str": "2099-01-15", "time_str": "10:00",
                "employee_id": "36645",
                # AI supplies plausible-looking but never-confirmed contact info
                "name": "Ольга (зі вчорашньої розмови)", "phone": "380631112233",
            },
        )
    )
    assert result["status"] == "VALIDATION_ERROR"
    assert "need_contact" in result["message"] or "телефон" in result["message"].lower() or "ім'я" in result["message"].lower()

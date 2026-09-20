"""Deterministic safety gates for the AI booking conversation.

The language model may draft the wording, but these guards decide whether a
reply is allowed to claim a booking or request payment.
"""

from __future__ import annotations

import re
from typing import Any, Iterable


CONFIRMED_STATES = {
    "WAITING_PAYMENT",
    "PAYMENT_PENDING_VERIFICATION",
    "BOOKED_CONFIRMED",
}


def payment_eligible(state: dict[str, Any], cfg: dict[str, Any] | None = None) -> bool:
    if state.get("state") != "WAITING_PAYMENT":
        return False
    if not state.get("appointment_id"):
        return False
    if cfg is not None and not bool(cfg.get("prepayment_required")):
        return False
    return True


RISKY_STATUS_MESSAGES = {
    "SLOT_NO_LONGER_AVAILABLE": "Цей час уже зайняли 😔 Давайте перевіримо актуальні вільні слоти ще раз.",
    "CRM_ERROR": "Не вдалося автоматично завершити запис. Я передала заявку адміністратору, щоб перевірити її вручну.",
    "VALIDATION_ERROR": "Ще не всі дані для запису підтверджені. Давайте дозберемо їх по черзі.",
    "INVALID_DATE_FORMAT": "Не вдалося розпізнати дату для запису. Підкажіть, будь ласка, дату ще раз.",
    "BOOKING_IN_PROGRESS": "Запит на запис уже обробляється. Хвилинку, будь ласка 🤍",
    "CRM_CREATED_PENDING_RECONCILIATION": "Запис створено в CRM, але його потрібно додатково перевірити адміністратору. Я вже передала інформацію 🤍",
    "RECONCILIATION_REQUIRED": "Попередню спробу запису потрібно перевірити адміністратору перед повторною операцією. Я вже передала заявку 🤍",
}

# Match actual claims that a booking was made/confirmed, not generic words
# such as "для запису" or "питання щодо запису".
CONFIRMATION_PATTERNS = (
    r"\bзаписую\b",
    r"\bзаписуємо\b",
    r"\bвас\s+запис(?:ала|али|ано)?\b",
    r"\bзапис\s+(?:підтверджено|створено|готов(?:ий|а|о)|успішно)\b",
    r"\bпідтвердж(?:ено|ений)\s+(?:ваш\s+)?запис\b",
    r"\bзабронюва\w*\b",
    r"\bappointment\s+(?:is\s+)?confirmed\b",
    r"\bbooking\s+(?:is\s+)?confirmed\b",
    r"\bbooked\b",
)

# Match actual requests/instructions to pay, not neutral references such as
# "оплату підтверджено" or "квитанцію отримано".
PAYMENT_REQUEST_PATTERNS = (
    r"\b(?:внесіть|внести|оплатіть|оплатити|сплатіть|сплатити)\s+"
    r"(?:будь\s+ласка,\s*)?(?:передоплату|оплату|завдаток)\b",
    r"\b(?:потрібно|необхідно)\s+(?:внести|оплатити|сплатити)\s+"
    r"(?:передоплату|оплату|завдаток)\b",
    r"\b(?:внесіть|внести)\s+(?:prepayment|payment)\b",
    r"\b(?:pay|payment\s+is\s+due|make\s+a\s+payment)\b",
    r"\b(?:pay|make|send)\s+(?:the\s+)?prepayment\b",
)


def contains_booking_confirmation(text: str) -> bool:
    return any(re.search(pattern, text or "", re.IGNORECASE) for pattern in CONFIRMATION_PATTERNS)


def contains_payment_request(text: str) -> bool:
    return any(re.search(pattern, text or "", re.IGNORECASE) for pattern in PAYMENT_REQUEST_PATTERNS)


def service_requires_photo(cfg: dict[str, Any], state: dict[str, Any]) -> bool:
    service_id = str(state.get("service_id") or "")
    service = (cfg.get("services") or {}).get(service_id, {})
    return bool(isinstance(service, dict) and service.get("requires_photo"))


def next_flow_reply(cfg: dict[str, Any], state: dict[str, Any]) -> str:
    if state.get("state") == "WAITING_ADMIN_CONFIRMATION":
        return "Я передала заявку адміністратору. Чекаємо підтвердження запису 🤍"
    if state.get("state") == "PAYMENT_PENDING_VERIFICATION":
        return "Дякую! Я передала чек адміністратору. Чекаємо підтвердження оплати 🤍"
    if state.get("state") == "WAITING_PAYMENT" and state.get("appointment_id"):
        return "Запис створено. Залишилося внести передоплату та надіслати квитанцію 🤍"
    if not state.get("service_id"):
        return "Підкажіть, будь ласка, яку саме послугу хочете 😊"
    if not state.get("date"):
        return "Підкажіть, будь ласка, бажану дату 📅"
    if not state.get("time"):
        return "На який час вам зручно? 🕐"
    if not state.get("employee_id"):
        return "До якого майстра хочете записатися?"
    if service_requires_photo(cfg, state) and not state.get("photo"):
        return "Надішліть, будь ласка, фото нігтів — воно потрібне перед записом 📸"
    if not state.get("name"):
        return "Підкажіть, будь ласка, ваше ім'я."
    if not state.get("phone"):
        return "І ще, будь ласка, номер телефону для запису 📱"
    return "Перевіряю запис ще раз, хвилинку 🤍"


def _success_payment_required(results: Iterable[dict[str, Any]]) -> bool:
    for item in results:
        if item.get("name") != "create_visit" or item.get("status") != "SUCCESS":
            continue
        raw = item.get("raw") or {}
        return bool(raw.get("payment_required"))
    return False


def guard_ai_reply(
    cfg: dict[str, Any],
    state: dict[str, Any],
    reply: str,
    tool_results: Iterable[dict[str, Any]] | None = None,
) -> str:
    results = list(tool_results or [])
    create_results = [item for item in results if item.get("name") == "create_visit"]
    statuses = {str(item.get("status") or "") for item in create_results}
    attempted = bool(create_results)
    succeeded = "SUCCESS" in statuses
    manual = "MANUAL_FALLBACK" in statuses
    confirmation_claim = contains_booking_confirmation(reply)
    payment_request = contains_payment_request(reply)

    if manual and (confirmation_claim or payment_request):
        return next_flow_reply(cfg, {**state, "state": "WAITING_ADMIN_CONFIRMATION"})

    if attempted and not (succeeded or manual):
        if "RECONCILIATION_REQUIRED" in statuses:
            return RISKY_STATUS_MESSAGES["RECONCILIATION_REQUIRED"]
        for status in statuses:
            if status in RISKY_STATUS_MESSAGES:
                if confirmation_claim or payment_request:
                    return RISKY_STATUS_MESSAGES[status]
                break
        if confirmation_claim or payment_request:
            return next_flow_reply(cfg, state)

    if payment_request:
        # A successful CRM write without prepayment does not authorize a
        # payment request. An existing WAITING_PAYMENT state is sufficient
        # only when the tenant actually requires prepayment.
        payment_is_allowed = payment_eligible(state, cfg)
        if attempted and succeeded:
            payment_is_allowed = payment_is_allowed and _success_payment_required(results)
        if not payment_is_allowed:
            safe_state = state
            if state.get("state") == "WAITING_PAYMENT" and not state.get("appointment_id"):
                safe_state = {**state, "state": "COLLECTING"}
            return next_flow_reply(cfg, safe_state)

    if confirmation_claim and not succeeded and not manual:
        return next_flow_reply(cfg, state)

    if state.get("state") == "WAITING_ADMIN_CONFIRMATION" and (confirmation_claim or payment_request):
        return next_flow_reply(cfg, state)

    return (reply or "").strip()

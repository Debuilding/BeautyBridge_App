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
RISKY_STATUS_MESSAGES = {
    "SLOT_NO_LONGER_AVAILABLE": "Цей час уже зайняли 😔 Давайте перевіримо актуальні вільні слоти ще раз.",
    "CRM_ERROR": "Не вдалося автоматично завершити запис. Я передала заявку адміністратору, щоб перевірити її вручну.",
    "VALIDATION_ERROR": "Ще не всі дані для запису підтверджені. Давайте дозберемо їх по черзі.",
    "INVALID_DATE_FORMAT": "Не вдалося розпізнати дату для запису. Підкажіть, будь ласка, дату ще раз.",
}
CONFIRMATION_RE = re.compile(
    r"(запис(?:ала|али|ано|ую|уємо)?|забронюва\w*|підтвердж\w*\s+запис|"
    r"appointment\s+(?:is\s+)?confirmed|booked|confirmed)",
    re.IGNORECASE,
)
PAYMENT_RE = re.compile(
    r"(передоплат\w*|оплат\w*|картк\w*|квитанц\w*|prepayment|payment|receipt)",
    re.IGNORECASE,
)


def contains_booking_confirmation(text: str) -> bool:
    return bool(CONFIRMATION_RE.search(text or ""))


def contains_payment_request(text: str) -> bool:
    return bool(PAYMENT_RE.search(text or ""))


def service_requires_photo(cfg: dict[str, Any], state: dict[str, Any]) -> bool:
    service_id = str(state.get("service_id") or "")
    service = (cfg.get("services") or {}).get(service_id, {})
    return bool(isinstance(service, dict) and service.get("requires_photo"))


def next_flow_reply(cfg: dict[str, Any], state: dict[str, Any]) -> str:
    if state.get("state") == "WAITING_ADMIN_CONFIRMATION":
        return "Я передала заявку адміністратору. Чекаємо підтвердження запису 🤍"
    if state.get("state") == "PAYMENT_PENDING_VERIFICATION":
        return "Дякую! Я передала чек адміністратору. Чекаємо підтвердження оплати 🤍"
    if state.get("state") == "WAITING_PAYMENT":
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

    if manual and contains_booking_confirmation(reply):
        return next_flow_reply(cfg, {**state, "state": "WAITING_ADMIN_CONFIRMATION"})

    if attempted and not (succeeded or manual):
        if contains_booking_confirmation(reply) or contains_payment_request(reply):
            for status in statuses:
                if status in RISKY_STATUS_MESSAGES:
                    return RISKY_STATUS_MESSAGES[status]
            return next_flow_reply(cfg, state)

    if not attempted and state.get("state") not in CONFIRMED_STATES | {"WAITING_ADMIN_CONFIRMATION"}:
        if contains_booking_confirmation(reply) or contains_payment_request(reply):
            return next_flow_reply(cfg, state)

    return (reply or "").strip()

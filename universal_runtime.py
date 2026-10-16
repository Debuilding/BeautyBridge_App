"""
BeautyBridge manual booking runtime.

Instagram conversation -> local booking request -> Telegram slot check ->
client confirmation -> final Telegram notice and salon details.
No external CRM availability or booking calls are made.
"""

from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import logging
import os
import re
import threading
import requests
import time
import uuid
from datetime import datetime, timedelta
from functools import wraps
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

from flask import jsonify, request

import config
import main as legacy
from states import BotState, can_transition
from flow_guard import guard_ai_reply, next_flow_reply
from process_role import background_enabled

LOGGER = logging.getLogger(__name__)

LANGUAGE_NAMES = {
    "uk": "українською",
    "en": "English",
    "de": "Deutsch",
    "fr": "français",
    "es": "español",
    "ro": "română",
}

SUPPORTED_AUTO_CRM = set()
# Current production behavior is manual-only. Future providers must be
# implemented and enabled explicitly per tenant after API review.
KNOWN_CRM_TYPES = {"manual", "table"}


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on", "y"}



BOOKING_CLAIM_RECONCILIATION_MINUTES = max(
    5,
    int(os.getenv("BOOKING_CLAIM_RECONCILIATION_MINUTES", "30")),
)
RUN_QUEUE_WORKER = background_enabled("RUN_QUEUE_WORKER")
QUEUE_POLL_SECONDS = max(0.25, float(os.getenv("QUEUE_POLL_SECONDS", "0.75")))
QUEUE_RETRY_SECONDS = max(30, int(os.getenv("QUEUE_RETRY_SECONDS", "300")))
OUTBOUND_RETRY_SECONDS = max(
    QUEUE_RETRY_SECONDS,
    int(os.getenv("OUTBOUND_RETRY_SECONDS", str(QUEUE_RETRY_SECONDS))),
)
REQUIRE_META_SIGNATURE = env_bool("REQUIRE_META_SIGNATURE", True)


# ---------------------------------------------------------------------------
# SQLite migration / persistence extensions
# ---------------------------------------------------------------------------


def _table_exists(conn, table: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone())


def _columns(conn, table: str) -> set[str]:
    if not _table_exists(conn, table):
        return set()
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _add_column(conn, table: str, name: str, definition: str) -> None:
    if name not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def migrate_database() -> None:
    """Bring old V1/V2 SQLite schemas forward without destroying data."""
    with legacy.db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS messages(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT,
                brand TEXT,
                sender_id TEXT,
                role TEXT,
                content TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS events(
                message_id TEXT PRIMARY KEY,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS state(
                brand TEXT,
                sender_id TEXT,
                data TEXT NOT NULL DEFAULT '{}',
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(brand, sender_id)
            );
            CREATE TABLE IF NOT EXISTS appointments(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT,
                brand TEXT,
                sender_id TEXT,
                name TEXT,
                phone TEXT,
                service_id TEXT,
                service_name TEXT,
                appointment_date TEXT,
                appointment_time TEXT,
                employee_id TEXT,
                master_name TEXT,
                crm_visit_id TEXT,
                status TEXT DEFAULT 'requested',
                paid INTEGER DEFAULT 0,
                receipt_received INTEGER DEFAULT 0,
                reminder_sent INTEGER DEFAULT 0,
                reinvite_sent INTEGER DEFAULT 0,
                notes TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS scheduler_runs(
                job_key TEXT PRIMARY KEY,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS booking_claims(
                booking_key TEXT PRIMARY KEY,
                brand TEXT NOT NULL,
                sender_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'IN_PROGRESS',
                appointment_id INTEGER,
                crm_visit_id TEXT,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS audit_events(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                brand TEXT,
                subject_hash TEXT,
                appointment_id INTEGER,
                correlation_id TEXT,
                actor TEXT NOT NULL DEFAULT 'system',
                payload TEXT NOT NULL DEFAULT '{}',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_audit_events_created
                ON audit_events(created_at);
            CREATE INDEX IF NOT EXISTS idx_audit_events_brand_type
                ON audit_events(brand, event_type, created_at);
            CREATE TABLE IF NOT EXISTS message_queue(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                brand TEXT NOT NULL,
                sender_id TEXT NOT NULL,
                text TEXT NOT NULL,
                created_at REAL NOT NULL,
                claimed_at REAL,
                processed_at REAL
            );
            CREATE TABLE IF NOT EXISTS outbound_messages(
                idempotency_key TEXT PRIMARY KEY,
                brand TEXT NOT NULL,
                sender_id TEXT NOT NULL,
                message_hash TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'PENDING',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_outbound_messages_status
                ON outbound_messages(status, updated_at);
            """
        )

        for name, definition in {
            "conversation_id": "TEXT",
            "brand": "TEXT",
            "sender_id": "TEXT",
            "role": "TEXT",
            "content": "TEXT",
            "created_at": "DATETIME",
        }.items():
            _add_column(conn, "messages", name, definition)

        for name, definition in {
            "conversation_id": "TEXT",
            "brand": "TEXT",
            "sender_id": "TEXT",
            "status": "TEXT DEFAULT 'requested'",
            "paid": "INTEGER DEFAULT 0",
            "receipt_received": "INTEGER DEFAULT 0",
            "reminder_sent": "INTEGER DEFAULT 0",
            "reinvite_sent": "INTEGER DEFAULT 0",
            "notes": "TEXT",
        }.items():
            _add_column(conn, "appointments", name, definition)

        if _table_exists(conn, "processed_events"):
            conn.execute(
                "INSERT OR IGNORE INTO events(message_id, created_at) "
                "SELECT message_id, created_at FROM processed_events"
            )

        if _table_exists(conn, "user_state"):
            rows = conn.execute(
                """
                SELECT brand, sender_id, state, nails_photo_received,
                       receipt_received, active_appointment_id,
                       selected_service_id, selected_employee_id,
                       selected_date, selected_time, client_name, client_phone,
                       updated_at
                FROM user_state
                """
            ).fetchall()
            for row in rows:
                (
                    brand,
                    sender_id,
                    state_name,
                    nails,
                    receipt,
                    appointment_id,
                    service_id,
                    employee_id,
                    selected_date,
                    selected_time,
                    client_name,
                    client_phone,
                    updated_at,
                ) = row
                payload = {
                    "state": state_name or BotState.START.value,
                    "photo": bool(nails),
                    "receipt": bool(receipt),
                    "appointment_id": appointment_id,
                    "service_id": service_id,
                    "employee_id": employee_id,
                    "date": selected_date,
                    "time": selected_time,
                    "name": client_name,
                    "phone": client_phone,
                }
                conn.execute(
                    """
                    INSERT INTO state(brand, sender_id, data, updated_at)
                    VALUES(?,?,?,COALESCE(?,CURRENT_TIMESTAMP))
                    ON CONFLICT(brand,sender_id) DO UPDATE SET
                        data=excluded.data,
                        updated_at=excluded.updated_at
                    """,
                    (
                        brand,
                        sender_id,
                        json.dumps(payload, ensure_ascii=False),
                        updated_at,
                    ),
                )
        conn.commit()


migrate_database()


def _audit_subject_hash(brand: str | None, sender: str | None) -> str | None:
    if not brand or not sender:
        return None
    return hashlib.sha256(f"{brand}:{sender}".encode("utf-8")).hexdigest()


def audit_event(
    event_type: str,
    *,
    brand: str | None = None,
    sender: str | None = None,
    appointment_id: int | None = None,
    correlation_id: str | None = None,
    actor: str = "system",
    payload: dict[str, Any] | None = None,
) -> int | None:
    """Append a privacy-conscious operational audit event.

    Raw client names/phones/photo URLs must never be passed in payload.
    Sender identity is represented by a one-way tenant-scoped hash.
    """
    try:
        with legacy.db() as conn:
            cur = conn.execute(
                """
                INSERT INTO audit_events(
                    event_type, brand, subject_hash, appointment_id,
                    correlation_id, actor, payload
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (
                    str(event_type),
                    brand,
                    _audit_subject_hash(brand, sender),
                    appointment_id,
                    correlation_id,
                    str(actor),
                    json.dumps(payload or {}, ensure_ascii=False, sort_keys=True)[:10000],
                ),
            )
            return int(cur.lastrowid)
    except Exception:
        LOGGER.exception("Audit event write failed: %s", event_type)
        return None


def list_audit_events(
    *,
    brand: str | None = None,
    event_type: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 200))
    sql = [
        "SELECT id,event_type,brand,subject_hash,appointment_id,correlation_id,actor,payload,created_at",
        "FROM audit_events",
        "WHERE 1=1",
    ]
    params: list[Any] = []
    if brand:
        sql.append("AND brand=?")
        params.append(brand)
    if event_type:
        sql.append("AND event_type=?")
        params.append(event_type)
    sql.append("ORDER BY id DESC LIMIT ?")
    params.append(limit)

    with legacy.db() as conn:
        rows = conn.execute(" ".join(sql), params).fetchall()

    result = []
    for row in rows:
        payload = {}
        try:
            payload = json.loads(row[7] or "{}")
            if not isinstance(payload, dict):
                payload = {}
        except json.JSONDecodeError:
            payload = {}
        result.append(
            {
                "id": row[0],
                "event_type": row[1],
                "brand": row[2],
                "subject_hash": row[3],
                "appointment_id": row[4],
                "correlation_id": row[5],
                "actor": row[6],
                "payload": payload,
                "created_at": row[8],
            }
        )
    return result


# ---------------------------------------------------------------------------
# CRM abstraction
# ---------------------------------------------------------------------------


class CRMError(RuntimeError):
    """Expected adapter/CRM error. Booking layer will use manual fallback."""


class ManualBookingRequired(CRMError):
    """The tenant deliberately uses manual booking instead of a live CRM."""


class CRMAdapter:
    """Stable contract used by BeautyBridge core, independent of provider."""

    name = "base"
    capabilities: set[str] = set()

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def get_available_slots(self, service_id: str, date_str: str) -> list[dict]:
        raise NotImplementedError

    def is_slot_available(
        self,
        service_id: str,
        employee_id: str,
        date_str: str,
        time_str: str,
    ) -> bool:
        slots = self.get_available_slots(service_id, date_str)
        return any(
            str(slot.get("employee_id")) == str(employee_id)
            and slot.get("date") == date_str
            and slot.get("time") == time_str
            for slot in slots
        )

    def create_booking(
        self,
        employee_id: str,
        service_id: str,
        date_str: str,
        time_str: str,
        name: str,
        phone: str,
    ) -> str:
        raise NotImplementedError

    def cancel_booking(self, crm_visit_id: str) -> bool:
        raise CRMError(f"{self.name} does not implement cancellation")

    def reschedule_booking(
        self,
        crm_visit_id: str,
        employee_id: str,
        date_str: str,
        time_str: str,
    ) -> bool:
        raise CRMError(f"{self.name} does not implement rescheduling")


class ManualCRMAdapter(CRMAdapter):
    name = "manual"
    capabilities = {"local_booking_request"}

    def get_available_slots(self, service_id: str, date_str: str) -> list[dict]:
        return []

    def is_slot_available(self, service_id: str, employee_id: str, date_str: str, time_str: str) -> bool:
        return True

    def create_booking(self, employee_id, service_id, date_str, time_str, name, phone) -> str:
        raise ManualBookingRequired("manual mode")


class UnsupportedCRMAdapter(ManualCRMAdapter):
    name = "unsupported"

    def __init__(self, cfg: dict, requested_type: str):
        super().__init__(cfg)
        self.requested_type = requested_type

    def get_available_slots(self, service_id: str, date_str: str) -> list[dict]:
        return []


def adapter_for(cfg: dict) -> CRMAdapter:
    """The core intentionally uses manual booking only; no external CRM calls."""
    return ManualCRMAdapter(cfg)


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def normalize_phone(value: Any) -> str:
    raw = str(value or "").strip()
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith("0") and len(digits) == 10:
        digits = "380" + digits[1:]
    elif len(digits) == 9:
        digits = "380" + digits
    return digits


def is_valid_phone(value: str) -> bool:
    return bool(re.fullmatch(r"\d{9,15}", normalize_phone(value)))


def _date_ok(value: str) -> bool:
    try:
        requested = datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return False
    return requested >= datetime.now().date()


def _time_ok(value: str) -> bool:
    return bool(re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", str(value or "")))


def booking_next_required_step(cfg: dict, state: dict) -> str:
    """
    Pure, deterministic function of (cfg, state) only. This is the single
    source of truth for what the booking flow still needs.
    """
    service_id = str(state.get("service_id") or "").strip()
    if not service_id:
        return "no_active_booking"

    date_str = str(state.get("date") or "").strip()
    if not date_str:
        return "need_date"

    time_str = str(state.get("time") or "").strip()
    employee_id = str(state.get("employee_id") or "").strip()
    if not time_str:
        return "need_time_master"

    services = cfg.get("services", {})
    service = services.get(service_id, {}) if isinstance(services, dict) else {}
    if service.get("requires_photo") and not state.get("photo"):
        return "need_photo"

    name = str(state.get("name") or "").strip()
    phone = normalize_phone(state.get("phone"))
    if not name or not is_valid_phone(phone):
        return "need_contact"

    appointment_id = state.get("appointment_id")
    booked_state = state.get("state")
    if appointment_id and booked_state in (
        BotState.WAITING_PAYMENT.value,
        BotState.BOOKED_CONFIRMED.value,
        BotState.WAITING_ADMIN_CONFIRMATION.value,
    ):
        if booked_state == BotState.WAITING_PAYMENT.value and not bool(
            state.get("receipt_confirmed") or state.get("payment_confirmed")
        ):
            return "need_payment"
        return "done"

    return "ready_to_book"


_STEP_MESSAGES = {
    "uk": {
        "need_date": "Підкажіть, будь ласка, точну дату суботи (наприклад, 17.10), щоб я правильно оформила заявку.",
        "need_time_master": "На котру годину або приблизно на який час вам зручно записатися?",
        "need_photo": "Будь ласка, надішліть фото ваших нігтів, щоб продовжити запис.",
        "need_contact": "Підкажіть, будь ласка, ваше ім'я та номер телефону для завершення запису.",
    },
    "en": {
        "need_date": "Could you please confirm the exact date for Saturday (for example, 17 October) so I can prepare the request correctly?",
        "need_time_master": "What time, or approximate time, would work for you?",
        "need_photo": "Please send a photo of your nails to continue the booking.",
        "need_contact": "Please share your name and phone number to complete the booking.",
    },
}


def _step_message(cfg: dict, gate: str) -> str:
    if gate == "no_active_booking":
        services = cfg.get("services", {})
        names = [
            str(item.get("name") or service_id).strip()
            for service_id, item in services.items()
            if isinstance(item, dict)
        ] if isinstance(services, dict) else []
        if names:
            return (
                "Підкажіть, будь ласка, яку саме послугу бажаєте? "
                "Можу допомогти з такими послугами: " + ", ".join(names) + "."
            )
        return "Підкажіть, будь ласка, яку саме послугу бажаєте?"
    lang = str(cfg.get("language") or "uk").lower()
    table = _STEP_MESSAGES.get(lang, _STEP_MESSAGES["uk"])
    return table.get(gate, table["need_photo"])


def enforce_flow_gate(cfg: dict, state: dict, reply: str) -> str:
    gate = booking_next_required_step(cfg, state)

    # Never allow the model to claim that a request was handed to the admin
    # while required booking fields are still missing. In particular, a
    # weekday such as "Saturday" must be persisted as a concrete date before
    # create_visit can run; otherwise no Telegram notification exists.
    if gate in {"no_active_booking", "need_date", "need_time_master", "need_photo", "need_contact"}:
        return _step_message(cfg, gate)

    if gate == "need_payment":
        instructions = payment_instruction(cfg)
        if instructions and instructions not in reply:
            return f"{reply}\n\n{instructions}".strip() if reply else instructions

    return reply


def _manual_time_preference_ok(value: str) -> bool:
    """Accept an explicitly stated approximate time for manual admin review."""
    import re

    value = " ".join(str(value or "").split())
    if not value or len(value) > 80:
        return False
    if _time_ok(value):
        return True

    # Examples: "після 12:00", "после 16", "до 14:30", "after 12".
    if re.search(
        r"(?i)\b(після|после|до|близько|около|приблизно|примерно|after|before|around)\s*\d{1,2}(?::\d{2})?\b",
        value,
    ):
        return True

    # Broad parts of day are preferences, not claims of availability.
    return bool(re.fullmatch(
        r"(?i)(зранку|вранці|утром|ранком|після обіду|по обіді|днём|днем|увечері|ввечері|вечером|у першій половині дня|у другій половині дня)",
        value,
    ))


def validate_booking(cfg: dict, state: dict, args: dict) -> tuple[bool, str, dict]:
    service_id = str(args.get("service_id") or "").strip()
    employee_id = str(args.get("employee_id") or "").strip()
    date_str = str(args.get("date_str") or "").strip()
    time_str = str(args.get("time_str") or "").strip()
    name = str(args.get("name") or "").strip()
    phone = normalize_phone(args.get("phone"))

    services = cfg.get("services", {})
    masters = cfg.get("masters", {})
    master_services = cfg.get("master_services", {})

    if services and service_id not in services:
        return False, "Невідома послуга. Використовуй тільки послугу з каталогу клієнта.", {}
    if masters and employee_id and employee_id not in masters:
        return False, "Невідомий майстер. Обери майстра з доступного каталогу.", {}

    allowed = master_services.get(employee_id) or [] if employee_id else []
    if employee_id and allowed and service_id not in allowed:
        return False, "Обраний майстер не виконує цю послугу.", {}

    if not _date_ok(date_str):
        return False, "Дата має бути коректною і не бути в минулому.", {}
    if not (_time_ok(time_str) or _manual_time_preference_ok(time_str)):
        return False, "Вкажи бажаний час, наприклад 14:30 або «після 12:00».", {}
    if not name:
        return False, "Потрібне ім'я клієнта.", {}
    if not is_valid_phone(phone):
        return False, "Потрібен коректний номер телефону.", {}

    # create_visit may only use values already persisted by remember_booking.
    state_fields = {
        "service_id": service_id,
        "employee_id": employee_id,
        "date": date_str,
        "time": time_str,
        "name": name,
        "phone": phone,
    }
    for state_key, requested_value in state_fields.items():
        persisted = str(state.get(state_key) or "").strip()
        if (state_key != "employee_id" and not persisted) or persisted != requested_value:
            return False, f"Поле {state_key} ще не підтверджене в поточному стані діалогу.", {}

    service = services.get(service_id, {}) if isinstance(services, dict) else {}
    if service.get("requires_photo") and not state.get("photo"):
        return False, "Для цієї послуги спочатку потрібно отримати фото.", {}

    cleaned = {
        "service_id": service_id,
        "employee_id": employee_id,
        "date_str": date_str,
        "time_str": time_str,
        "name": name,
        "phone": phone,
    }
    return True, "OK", cleaned


# ---------------------------------------------------------------------------
# State / appointment helpers
# ---------------------------------------------------------------------------


def strict_state_set(brand: str, sender: str, **updates) -> dict:
    current = legacy.state_get(brand, sender)
    requested = updates.get("state")
    if requested:
        current_name = current.get("state") or BotState.START.value
        if current_name != requested and not can_transition(current_name, requested):
            LOGGER.warning("Blocked invalid state transition %s/%s: %s -> %s", brand, sender, current_name, requested)
            return current
    return legacy.state_set(brand, sender, **updates)


def appointment_row(appointment_id: int):
    with legacy.db() as conn:
        return conn.execute(
            "SELECT id,brand,sender_id,name,phone,service_name,appointment_date,appointment_time,master_name,status,paid,receipt_received,crm_visit_id FROM appointments WHERE id=?",
            (appointment_id,),
        ).fetchone()


def update_appointment(appointment_id: int, **fields) -> None:
    if not fields:
        return
    allowed = {"status", "paid", "receipt_received", "notes", "reminder_sent", "reinvite_sent", "crm_visit_id"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return
    setters = ", ".join(f"{key}=?" for key in fields)
    params = list(fields.values()) + [appointment_id]
    with legacy.db() as conn:
        conn.execute(f"UPDATE appointments SET {setters} WHERE id=?", params)


def booking_address_text(cfg: dict) -> str:
    pieces = []
    if cfg.get("address"):
        pieces.append(f"Адреса: {cfg['address']} 📍")
    if cfg.get("phone"):
        pieces.append(f"Телефон: {cfg['phone']} 📱")
    if cfg.get("wifi"):
        wifi = f"Wi-Fi: {cfg['wifi']}"
        if cfg.get("wifi_password"):
            wifi += f" | Пароль: {cfg['wifi_password']}"
        pieces.append(wifi)
    return "\n".join(pieces)


def payment_instruction(cfg: dict) -> str:
    amount = cfg.get("prepayment_amount", 0)
    card = cfg.get("card_number", "")
    owner = cfg.get("card_name", "")
    if not amount:
        return "Передоплата не потрібна."
    text = f"Запис створено 🌷 Внесіть, будь ласка, передоплату {amount} грн як гарантію запису."
    if card:
        text += f"\nКартка: {card}"
    if owner:
        text += f" ({owner})"
    text += "\nПісля оплати надішліть фото квитанції сюди."
    return text


def send_after_payment_confirmed(cfg: dict, sender: str) -> None:
    message = "Оплату підтверджено ❤️\n"
    address = booking_address_text(cfg)
    if address:
        message += address + "\n"
    message += "До зустрічі! 🌸"
    try:
        legacy.instagram_send(cfg, sender, message)
    except Exception:
        LOGGER.exception("Failed to send post-payment confirmation")


# ---------------------------------------------------------------------------
# AI flow
# ---------------------------------------------------------------------------


def tool_specs() -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": "remember_booking",
                "description": "Зберігає вже названі клієнтом дані. Не втрачай їх між повідомленнями.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "service_id": {"type": "string"},
                        "date_str": {"type": "string"},
                        "time_str": {"type": "string"},
                        "employee_id": {"type": "string"},
                        "name": {"type": "string"},
                        "phone": {"type": "string"},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_available_slots",
                "description": "Автоматичний календар не підключено. Не використовуй цей інструмент для пошуку слотів; запитай бажаний або приблизний час клієнта. Виклик повертає лише MANUAL_MODE без доступних слотів.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "service_id": {"type": "string"},
                        "date_str": {
                            "type": "string",
                            "description": "Дата у форматі YYYY-MM-DD (РРРР-ММ-ДД), наприклад '2026-09-23'. Ніколи не передавай дату в іншому форматі.",
                        },
                    },
                    "required": ["service_id", "date_str"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "create_visit",
                "description": "Створює заявку/запис лише після серверної перевірки всіх даних.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "service_id": {"type": "string"},
                        "date_str": {
                            "type": "string",
                            "description": "Дата у форматі YYYY-MM-DD (РРРР-ММ-ДД), наприклад '2026-09-23'. Ніколи не передавай дату в іншому форматі.",
                        },
                        "time_str": {"type": "string"},
                        "employee_id": {"type": "string"},
                        "name": {"type": "string"},
                        "phone": {"type": "string"},
                    },
                    "required": ["service_id", "date_str", "time_str", "name", "phone"],
                },
            },
        },
    ]


def build_prompt(brand: str, cfg: dict, state: dict) -> str:
    language = LANGUAGE_NAMES.get(str(cfg.get("language", "uk")).lower(), "мовою клієнта")
    services = "\n".join(
        f"{sid} — {item.get('name', sid)}"
        for sid, item in cfg.get("services", {}).items()
    ) or "каталог ще не налаштований"
    masters = "\n".join(
        f"{sid} — {name}"
        for sid, name in cfg.get("masters", {}).items()
    ) or "майстри ще не налаштовані"
    crm = adapter_for(cfg)
    capabilities = ", ".join(sorted(crm.capabilities)) or "manual_fallback"
    missing = [
        key
        for key, value in {
            "service_id": state.get("service_id"),
            "date": state.get("date"),
            "time": state.get("time"),
            "name": state.get("name"),
            "phone": state.get("phone"),
        }.items()
        if not value
    ]
    prompt = f"""
Ти — AI-адміністратор {cfg.get('name')}. Відповідай коротко, природно, без роботизованих шаблонів. Основна мова цього салону: {language}.
МОВА СПІЛКУВАННЯ — ОБОВ’ЯЗКОВЕ ПРАВИЛО:
- Мова за замовчуванням — українська.
- Якщо клієнт пише російською, завжди відповідай українською. Не переходь на російську, не коментуй і не виправляй мову клієнта.
- Якщо клієнт пише будь-якою іншою мовою (англійською, польською, румунською, німецькою, французькою, іспанською або іншою), відповідай цією мовою, навіть якщо її немає в переліку налаштувань салону.
- Якщо мова змішана, обирай чітко домінантну мову, але ніколи не обирай російську; якщо визначити мову складно — відповідай українською.
- Зберігай обрану мову в наступних повідомленнях, доки клієнт явно не попросить іншу або не перейде на іншу мову.
- Назви послуг, ціни, правила запису й факти про салон не перекладай так, щоб змінити їхній зміст; використовуй лише дані конфігурації.
Сьогодні: {datetime.now(ZoneInfo(cfg.get('local_tz') or config.LOCAL_TZ)).strftime('%Y-%m-%d')} ({datetime.now(ZoneInfo(cfg.get('local_tz') or config.LOCAL_TZ)).strftime('%d.%m.%Y')}).
У викликах функцій (date_str) використовуй ЛИШЕ формат РРРР-ММ-ДД (наприклад 2026-09-23), незалежно від того, як дату написав клієнт (\"23 вересня\", \"23.09\", \"завтра\" тощо) — переведи її у цей формат сам, орієнтуючись на сьогоднішню дату вище.

BOOKING_FLOW=Instagram -> local request -> Telegram admin check -> client confirmation
STATE={json.dumps(state, ensure_ascii=False)}
MISSING={missing}

ЖОРСТКІ ПРАВИЛА:
1. Ти — адміністратор салону в цьому Instagram-діалозі. КАТЕГОРИЧНО ЗАБОРОНЕНО писати «передам адміністратору», «повідомлю адміністратора», «адміністратор перевірить» або будь-які подібні фрази — ти і є адміністратор. Замість цього: «Перевірю цей час і напишу вам» або «Уточню наявність і одразу напишу 🤍».
2. Перед відповіддю зберігай через remember_booking усі відомі поля: service_id, date_str, time_str, employee_id, name, phone. Не питай повторно те, що вже збережено.
3. Записуй бажаний або приблизний час клієнта. Немає автоматичного календаря, тому ніколи не вигадуй, що слот вільний, і не пропонуй вигадані доступні слоти.
4. Якщо бракує послуги, дати, бажаного часу, потрібного фото, імені або телефону — запитай лише наступну необхідну деталь. Майстер необов’язковий: якщо клієнту байдуже, не наполягай і не вигадуй, хто буде на зміні; залиш employee_id порожнім.
5. Якщо requires_photo=true, попроси фото до створення заявки.
6. Коли всі обов'язкові дані зібрані, виклич create_visit. Це створює локальну заявку і надсилає адміну запит у Telegram; це ще НЕ підтверджений запис.
7. Після відправлення заявки скажи природно, що уточниш бажаний час і напишеш клієнту. Не кажи «запис підтверджено» і не проси оплату.
8. Після відповіді з Telegram клієнту надсилається запропонований слот. Дочекайся явного «Так» або «Ні»; не завершуй підтвердження самостійно.
9. Якщо клієнт відповідає «Ні», запитай інший бажаний або приблизний час і створи нову заявку після збору даних.
10. Адресу, телефон салону та Wi-Fi повідомляй тільки після того, як клієнт погодився на час, перевірений адміністратором.
11. Ціни, послуги, тривалість і правила бери лише з конфігурації. Не вигадуй дані та доплати.
12. Спілкуйся коротко, тепло й природно; не став кілька зайвих запитань поспіль.
13. Дотримуйся мовної політики вище: українська за замовчуванням, російською не відповідай.
14. Для перенесення, скасування чи нестандартної домовленості збери деталі й створи локальний запит; не обіцяй змін без перевірки.
15. Ніколи не викликай зовнішні CRM/API для пошуку доступності або створення запису.

ПОСЛУГИ:
{services}

МАЙСТРИ:
{masters}

ПРАЙС:
{cfg.get('price_text', '')}

ПЕРЕДОПЛАТА: {cfg.get('prepayment_amount', 0)} грн.
"""

    # Rozmary uses the versioned salon-assistant prompt from the repository.
    # Keep the runtime's live state/configuration above authoritative.
    if str(brand or "").strip().casefold().startswith("rozmary"):
        prompt_path = os.path.join(os.path.dirname(__file__), "docs", "BEAUTYBRIDGE_PROMPT_V4.md")
        try:
            with open(prompt_path, "r", encoding="utf-8") as prompt_file:
                versioned_prompt = prompt_file.read().strip()
            if versioned_prompt:
                prompt += "\n\nROZMARY_PROMPT_V4 (additional behavior rules; follow live STATE and application workflow):\n" + versioned_prompt
        except OSError:
            LOGGER.exception("Rozmary prompt v4 could not be loaded from %s", prompt_path)

    return prompt


def sanitize_reply(cfg: dict, state: dict, reply: str) -> str:
    # Salon details are only allowed after both admin and client confirmed the slot.
    if state.get("state") == BotState.BOOKED_CONFIRMED.value:
        return reply
    cleaned = reply
    for secret in (cfg.get("address"), cfg.get("phone"), cfg.get("wifi"), cfg.get("wifi_password")):
        if secret:
            cleaned = cleaned.replace(str(secret), "")
    return cleaned.strip()


def booking_idempotency_key(
    brand: str,
    sender: str,
    service_id: str,
    employee_id: str,
    date_str: str,
    time_str: str,
    name: str,
    phone: str,
) -> str:
    canonical = "|".join(
        str(value or "").strip().lower()
        for value in (
            brand,
            sender,
            service_id,
            employee_id,
            date_str,
            time_str,
            name,
            phone,
        )
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def claim_booking(brand: str, sender: str, booking_key: str) -> dict:
    with legacy.db() as conn:
        before = conn.total_changes
        conn.execute(
            """
            INSERT OR IGNORE INTO booking_claims(
                booking_key, brand, sender_id, status, updated_at
            ) VALUES(?,?,?,?,CURRENT_TIMESTAMP)
            """,
            (booking_key, brand, sender, "IN_PROGRESS"),
        )
        claimed = conn.total_changes > before
        row = conn.execute(
            """
            SELECT booking_key, status, appointment_id, crm_visit_id, updated_at
            FROM booking_claims
            WHERE booking_key=?
            """,
            (booking_key,),
        ).fetchone()

        if row and row[1] == "IN_PROGRESS" and not claimed:
            try:
                updated_at = datetime.fromisoformat(str(row[4]))
                age = datetime.utcnow() - updated_at
            except (TypeError, ValueError):
                age = timedelta.max

            if age >= timedelta(minutes=BOOKING_CLAIM_RECONCILIATION_MINUTES):
                conn.execute(
                    """
                    UPDATE booking_claims
                    SET status='RECONCILIATION_REQUIRED', updated_at=CURRENT_TIMESTAMP
                    WHERE booking_key=? AND status='IN_PROGRESS'
                    """,
                    (booking_key,),
                )
                row = conn.execute(
                    """
                    SELECT booking_key, status, appointment_id, crm_visit_id, updated_at
                    FROM booking_claims
                    WHERE booking_key=?
                    """,
                    (booking_key,),
                ).fetchone()

    return {
        "claimed": claimed,
        "status": row[1] if row else "UNKNOWN",
        "appointment_id": row[2] if row else None,
        "crm_visit_id": row[3] if row else None,
    }


def finalize_booking_claim(
    booking_key: str,
    status: str,
    appointment_id: int | None = None,
    crm_visit_id: str | None = None,
) -> None:
    with legacy.db() as conn:
        conn.execute(
            """
            UPDATE booking_claims
            SET status=?, appointment_id=?, crm_visit_id=?, updated_at=CURRENT_TIMESTAMP
            WHERE booking_key=?
            """,
            (status, appointment_id, crm_visit_id, booking_key),
        )


def release_booking_claim(booking_key: str) -> None:
    with legacy.db() as conn:
        conn.execute("DELETE FROM booking_claims WHERE booking_key=?", (booking_key,))


BOOKING_STATE_LIFECYCLE_KEYS = {
    "appointment_id",
    "payment_confirmed",
    "receipt_confirmed",
    "receipt",
    "nails_photo_url",
    "photo",
}


def _replace_state(brand: str, sender: str, data: dict[str, Any]) -> dict[str, Any]:
    with legacy.db() as conn:
        conn.execute(
            """
            INSERT INTO state(brand, sender_id, data, updated_at)
            VALUES(?,?,?,CURRENT_TIMESTAMP)
            ON CONFLICT(brand,sender_id) DO UPDATE SET
                data=excluded.data,
                updated_at=CURRENT_TIMESTAMP
            """,
            (brand, sender, json.dumps(data, ensure_ascii=False)),
        )
    return data


def _begin_new_booking(current: dict[str, Any], updates: dict[str, Any]) -> bool:
    current_state = str(current.get("state") or BotState.START.value)
    lifecycle_states = {
        BotState.WAITING_PAYMENT.value,
        BotState.PAYMENT_PENDING_VERIFICATION.value,
        BotState.WAITING_ADMIN_CONFIRMATION.value,
        BotState.WAITING_CLIENT_TIME_CONFIRMATION.value,
        BotState.BOOKED_CONFIRMED.value,
    }

    # Repeating the same booking details must not reset a lifecycle state:
    # otherwise a casual "thanks/yes" could erase appointment_id and permit
    # a duplicate local request. Only a changed booking identity starts anew.
    identity_changed = any(
        key in updates
        and str(current.get(key) or "").strip() != str(updates[key] or "").strip()
        for key in ("service_id", "date", "time", "employee_id")
    )
    if current_state in lifecycle_states:
        return bool(updates) and identity_changed

    # During ordinary collection, changing a date/time/master/service should
    # update the existing draft, not replace it with a partial set of fields.
    # Reset only when a stale appointment id is still attached to the state.
    return bool(current.get("appointment_id")) and bool(updates)


def _state_for_new_booking(updates: dict[str, Any]) -> dict[str, Any]:
    data = {
        "state": BotState.COLLECTING.value,
    }
    data.update(updates)
    return data


def handle_tool(brand: str, sender: str, cfg: dict, name: str, args: dict) -> str:
    """Booking tools use a local request + Telegram verification only. No CRM calls."""
    state = legacy.state_get(brand, sender)

    if name == "remember_booking":
        updates = {}
        for key, arg_key in (
            ("service_id", "service_id"), ("date", "date_str"), ("time", "time_str"),
            ("employee_id", "employee_id"), ("name", "name"), ("phone", "phone"),
        ):
            value = args.get(arg_key)
            if value:
                value = normalize_phone(value) if key == "phone" else str(value).strip()
                updates[key] = value
        if updates:
            current_state = str(state.get("state") or BotState.START.value)
            if (
                updates.get("service_id")
                and state.get("service_id")
                and str(updates["service_id"]) != str(state.get("service_id"))
            ):
                # A different service invalidates any previously supplied
                # photo, which may have been required only for the old one.
                updates["photo"] = False
                updates["nails_photo_url"] = ""
            new_booking = _begin_new_booking(state, updates)
            if current_state in {
                BotState.WAITING_ADMIN_CONFIRMATION.value,
                BotState.WAITING_CLIENT_TIME_CONFIRMATION.value,
            }:
                return json.dumps({
                    "status": "BOOKING_IN_PROGRESS",
                    "appointment_id": state.get("appointment_id"),
                }, ensure_ascii=False)
            if current_state in {
                BotState.WAITING_PAYMENT.value,
                BotState.PAYMENT_PENDING_VERIFICATION.value,
                BotState.BOOKED_CONFIRMED.value,
            } and not new_booking:
                return json.dumps({
                    "status": "ALREADY_BOOKED" if current_state == BotState.BOOKED_CONFIRMED.value else "BOOKING_IN_PROGRESS",
                    "appointment_id": state.get("appointment_id"),
                }, ensure_ascii=False)
            if new_booking:
                _replace_state(brand, sender, _state_for_new_booking(updates))
            else:
                updates["state"] = BotState.COLLECTING.value
                strict_state_set(brand, sender, **updates)
        return json.dumps({"status": "REMEMBERED", **updates}, ensure_ascii=False)

    if name == "get_available_slots":
        date_str = str(args.get("date_str") or "")
        if not _date_ok(date_str):
            return json.dumps({"status": "INVALID_DATE_FORMAT", "message": "Потрібна дата у форматі YYYY-MM-DD, яка не минула."}, ensure_ascii=False)
        return json.dumps({
            "status": "MANUAL_MODE",
            "slots": [],
            "message": "Live availability is not connected. Ask for the client's preferred date and approximate time; never invent available slots. Once all details are collected, create a request for admin verification in Telegram.",
        }, ensure_ascii=False)

    if name == "create_visit":
        gate = booking_next_required_step(cfg, state)
        if gate != "ready_to_book":
            message = {
                "need_photo": _step_message(cfg, "need_photo"),
                "need_contact": _step_message(cfg, "need_contact"),
                "need_date": "Спочатку потрібно визначити бажану дату.",
                "need_time_master": "Спочатку потрібно визначити бажаний час і майстра.",
                "need_payment": "Ця заявка вже обробляється.",
                "done": "Цю заявку вже завершено.",
                "no_active_booking": "Спочатку потрібно вибрати послугу.",
            }.get(gate, "Ще не всі дані для запису підтверджені.")
            return json.dumps({"status": "VALIDATION_ERROR", "message": message}, ensure_ascii=False)

        canonical_args = {
            "service_id": state.get("service_id"), "employee_id": state.get("employee_id"),
            "date_str": state.get("date"), "time_str": state.get("time"),
            "name": state.get("name"), "phone": state.get("phone"),
        }
        ok, message, cleaned = validate_booking(cfg, state, canonical_args)
        if not ok:
            return json.dumps({"status": "VALIDATION_ERROR", "message": message}, ensure_ascii=False)

        service_id = cleaned["service_id"]
        employee_id = cleaned["employee_id"]
        service_name = cfg.get("services", {}).get(service_id, {}).get("name", service_id)
        master_name = cfg.get("masters", {}).get(employee_id, employee_id or "не обрано")
        booking_key = booking_idempotency_key(
            brand, sender, service_id, employee_id, cleaned["date_str"],
            cleaned["time_str"], cleaned["name"], cleaned["phone"],
        )
        claim = claim_booking(brand, sender, booking_key)
        if not claim["claimed"]:
            return json.dumps({
                "status": "MANUAL_FALLBACK" if claim.get("appointment_id") else claim.get("status", "BOOKING_IN_PROGRESS"),
                "appointment_id": claim.get("appointment_id"),
                "idempotent": True,
            }, ensure_ascii=False)

        try:
            appt_id = legacy.create_local_appointment(
                brand, sender, name=cleaned["name"], phone=cleaned["phone"],
                service_id=service_id, service_name=service_name,
                date=cleaned["date_str"], time=cleaned["time_str"],
                employee_id=employee_id, master_name=master_name,
                crm_visit_id="", status="pending_manual_confirmation",
                notes="Awaiting admin slot verification via Telegram; no CRM integration. | TELEGRAM_NOTIFICATION_IN_PROGRESS",
            )
        except Exception:
            LOGGER.exception("Failed to persist manual booking request for %s/%s", brand, sender)
            try:
                release_booking_claim(booking_key)
            except Exception:
                LOGGER.exception("Failed to release booking claim after persistence failure")
            return json.dumps({
                "status": "LOCAL_PERSISTENCE_ERROR",
                "message": "Не вдалося зберегти заявку. Спробуйте ще раз трохи пізніше.",
            }, ensure_ascii=False)
        strict_state_set(
            brand, sender, state=BotState.WAITING_ADMIN_CONFIRMATION.value,
            appointment_id=appt_id, service_id=service_id, date=cleaned["date_str"],
            time=cleaned["time_str"], employee_id=employee_id,
            name=cleaned["name"], phone=cleaned["phone"],
        )
        notification_sent = send_admin_action_message(
            cfg, appt_id,
            "\n".join([
                "📝 НОВА ЗАЯВКА — перевірити бажаний час",
                f"Салон: {cfg.get('name')}", f"Клієнт: {cleaned['name']}",
                f"Телефон: {cleaned['phone']}", f"Послуга: {service_name}",
                f"Майстер: {master_name}", f"Бажана дата: {cleaned['date_str']}",
                f"Бажаний / орієнтовний час: {cleaned['time_str']}",
                "Статус: очікує перевірки. Підтвердження клієнту ще не надсилалось.",
            ]),
            [("✅ Час доступний", f"bb:time_ok:{appt_id}"), ("🕒 Час зайнятий", f"bb:time_busy:{appt_id}")],
        )
        if not notification_sent:
            LOGGER.error("Manual booking request %s was saved but Telegram notification failed", appt_id)
        # Persist delivery state so the worker can retry failed Telegram notices.
        update_appointment(
            appt_id,
            notes=(
                "Awaiting admin slot verification via Telegram; no CRM integration. | "
                + ("TELEGRAM_NOTIFICATION_SENT" if notification_sent else "TELEGRAM_NOTIFICATION_PENDING")
            ),
        )
        finalize_booking_claim(booking_key, "MANUAL_FALLBACK", appointment_id=appt_id)
        return json.dumps({
            "status": "MANUAL_FALLBACK", "appointment_id": appt_id,
            "service": service_name, "master": master_name,
            "telegram_notified": bool(notification_sent), "payment_required": False,
        }, ensure_ascii=False)

    return json.dumps({"status": "UNKNOWN_TOOL"}, ensure_ascii=False)

def _waiting_admin_notification_reply(brand: str, sender: str, state: dict) -> str | None:
    """Return a truthful client reply only for a real pending request."""
    appointment_id = state.get("appointment_id")
    if not appointment_id:
        return None
    try:
        row = appointment_row(int(appointment_id))
    except (TypeError, ValueError):
        return None
    if (
        not row
        or str(row[1]) != str(brand)
        or str(row[2]) != str(sender)
        or str(row[9]) != "pending_manual_confirmation"
    ):
        return None

    with legacy.db() as conn:
        result = conn.execute(
            "SELECT notes FROM appointments WHERE id=?", (int(appointment_id),)
        ).fetchone()
    notes = str(result[0] or "") if result else ""

    if "TELEGRAM_NOTIFICATION_SENT" not in notes:
        # Reset an interrupted IN_PROGRESS marker to a durable retryable state.
        update_appointment(
            int(appointment_id),
            notes=(
                "Awaiting admin slot verification via Telegram; no CRM integration. | "
                "TELEGRAM_NOTIFICATION_PENDING"
            ),
        )
        retry_pending_admin_notifications(force=True)
        with legacy.db() as conn:
            result = conn.execute(
                "SELECT notes FROM appointments WHERE id=?", (int(appointment_id),)
            ).fetchone()
        notes = str(result[0] or "") if result else ""

    if "TELEGRAM_NOTIFICATION_SENT" in notes:
        requested_time = str(row[7] or "").strip()
        if requested_time:
            service_name = str(row[5] or "").strip()
            requested_date = str(row[6] or "").strip()
            when = ", ".join(part for part in (requested_date, requested_time) if part)
            request_label = f" на {service_name}" if service_name else ""
            return (
                f"Дякую! Заявку{request_label} передано на перевірку часу — {when}. "
                "Напишу вам, щойно отримаю відповідь 🌷"
            )
        return "Дякую! Заявку передано на перевірку часу. Напишу вам, щойно отримаю відповідь 🌷"

    return (
        "Заявку збережено, але повідомлення для перевірки часу ще не доставлено. "
        "Запис не підтверджено; система повторить спробу автоматично."
    )


def process_with_ai(brand: str, sender: str, text: str) -> str:
    correlation_id = uuid.uuid4().hex
    audit_event(
        "ai_message_received",
        brand=brand,
        sender=sender,
        correlation_id=correlation_id,
        actor="ai",
        payload={"text_length": len(text or "")},
    )
    cfg = legacy.cfg_for(brand)
    legacy.save_message(brand, sender, "user", text)
    state = legacy.state_get(brand, sender)

    # A persisted WAITING_ADMIN_CONFIRMATION state is not proof that Telegram
    # delivered the admin notification. Verify the appointment and durable
    # delivery marker before telling the client the request was handed off.
    if state.get("state") == BotState.WAITING_ADMIN_CONFIRMATION.value:
        reply = _waiting_admin_notification_reply(brand, sender, state)
        if reply is not None:
            legacy.save_message(brand, sender, "assistant", reply)
            return reply

        # Recover stale/orphaned state instead of looping forever on a booking
        # that has no matching pending appointment.
        LOGGER.error(
            "Stale WAITING_ADMIN_CONFIRMATION state without a valid pending appointment for %s/%s",
            brand, sender,
        )
        strict_state_set(
            brand, sender, state=BotState.COLLECTING.value, appointment_id=None
        )
        state = legacy.state_get(brand, sender)

    if state.get("state") == BotState.WAITING_CLIENT_TIME_CONFIRMATION.value:
        answer = str(text or "").strip().casefold()
        yes = bool(re.search(r"^(так|підходить|підтверджую|підтверджено|ок|окей|добре|yes|yeah|yep|да|подходит|согласен|согласна)\b", answer))
        no = bool(re.search(r"^(ні|нет|не\s+підходить|не\s+подходит|інший\s+час|другий\s+час|no|nope)\b", answer))
        appointment_id = state.get("appointment_id")
        if not yes and not no:
            reply = "Підкажіть, будь ласка: цей час вам підходить? Напишіть «Так» або «Ні» 🤍"
            legacy.save_message(brand, sender, "assistant", reply)
            return reply
        if no:
            if appointment_id:
                update_appointment(int(appointment_id), status="client_declined_time")
            strict_state_set(
                brand, sender, state=BotState.COLLECTING.value,
                time="", appointment_id=None,
            )
            reply = "Добре 🤍 Напишіть, будь ласка, який інший час або проміжок вам зручний."
            legacy.save_message(brand, sender, "assistant", reply)
            return reply

        if not appointment_id:
            LOGGER.error("Client confirmed proposed time but appointment id is missing for %s/%s", brand, sender)
            reply = "Перепрошую, уточнімо бажаний час ще раз. На яку дату й годину вам зручно?"
            legacy.save_message(brand, sender, "assistant", reply)
            return reply
        row = appointment_row(int(appointment_id))
        if not row:
            LOGGER.error("Client confirmed proposed time but appointment %s was not found", appointment_id)
            reply = "Перепрошую, не вдалося завершити підтвердження. Напишіть, будь ласка, бажану дату й час ще раз."
            legacy.save_message(brand, sender, "assistant", reply)
            return reply
        _, _brand, _sender, client_name, _phone, service_name, appt_date, appt_time, master_name, appt_status, *_ = row
        if appt_status != "waiting_client_confirmation":
            reply = "Ця пропозиція часу вже неактуальна. Напишіть, будь ласка, бажану дату й час ще раз 🤍"
            legacy.save_message(brand, sender, "assistant", reply)
            return reply
        prepayment_required = bool(cfg.get("prepayment_required"))
        if prepayment_required:
            update_appointment(int(appointment_id), status="booked_awaiting_payment")
            strict_state_set(
                brand, sender,
                state=BotState.WAITING_PAYMENT.value,
                appointment_id=int(appointment_id),
                payment_confirmed=False,
                receipt_confirmed=False,
            )
            final_message = "\n".join([
                "🌸 Час погоджено! Щоб закріпити запис, внесіть передоплату.",
                str(service_name or ""),
                f"{appt_date} о {appt_time}",
                f"Майстер: {master_name}" if master_name else "",
                payment_instruction(cfg),
            ]).strip()
            admin_status = "Клієнт підтвердив запропонований час в Instagram; очікується передоплата."
        else:
            update_appointment(int(appointment_id), status="confirmed")
            strict_state_set(
                brand, sender,
                state=BotState.BOOKED_CONFIRMED.value,
                appointment_id=int(appointment_id),
            )
            final_message = "\n".join([
                "🌸 Чудово, ваш запис підтверджено!",
                str(service_name or ""),
                f"{appt_date} о {appt_time}",
                f"Майстер: {master_name}" if master_name else "",
                "Чекаємо на вас ❤️",
                booking_address_text(cfg),
            ]).strip()
            admin_status = "Клієнт підтвердив запропонований час в Instagram; запис завершено."

        # The queue worker is the single owner of Instagram delivery.
        # Sending here as well would deliver this confirmation twice.
        try:
            send_admin_telegram(cfg, "\n".join([
                f"📌 ЗМІНА СТАТУСУ — заявка #{appointment_id}",
                f"Клієнт: {client_name or state.get('name') or '—'}",
                f"Послуга: {service_name}",
                f"Дата й час: {appt_date} {appt_time}",
                f"Майстер: {master_name or '—'}",
                admin_status,
            ]))
        except Exception:
            LOGGER.exception("Failed to send final admin Telegram notification")
        legacy.save_message(brand, sender, "assistant", final_message)
        return final_message

    if not legacy.ai:
        raise RuntimeError("OPENAI_API_KEY is not configured")

    messages = [
        {"role": "system", "content": build_prompt(brand, cfg, state)},
        *legacy.history(brand, sender, 14),
    ]
    tool_results: list[dict[str, Any]] = []

    response = legacy.ai.chat.completions.create(
        model=config.OPENAI_MODEL,
        messages=messages,
        tools=tool_specs(),
        tool_choice="auto",
        temperature=0.35,
        max_tokens=700,
    )
    assistant = response.choices[0].message

    if assistant.tool_calls:
        messages.append(assistant)
        for call in assistant.tool_calls:
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            result = handle_tool(brand, sender, cfg, call.function.name, args)
            try:
                parsed_result = json.loads(result)
            except json.JSONDecodeError:
                parsed_result = {"status": "UNKNOWN_TOOL_RESULT"}
            tool_results.append(
                {
                    "name": call.function.name,
                    "status": parsed_result.get("status"),
                    "raw": parsed_result,
                }
            )
            audit_event(
                "ai_tool_result",
                brand=brand,
                sender=sender,
                appointment_id=parsed_result.get("appointment_id"),
                correlation_id=correlation_id,
                actor="ai",
                payload={
                    "tool": call.function.name,
                    "status": parsed_result.get("status"),
                    "idempotent": bool(parsed_result.get("idempotent")),
                },
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "name": call.function.name,
                    "content": result,
                }
            )
        messages[0] = {"role": "system", "content": build_prompt(brand, cfg, legacy.state_get(brand, sender))}
        final = legacy.ai.chat.completions.create(
            model=config.OPENAI_MODEL,
            messages=messages,
            temperature=0.3,
            max_tokens=700,
        )
        reply = final.choices[0].message.content or ""
    else:
        reply = assistant.content or ""

    # Do not rely on the model to remember the final create_visit tool call.
    # Once persisted state is complete, create the pending request exactly once
    # through the same validated tool path; Telegram is triggered only here.
    current_state = legacy.state_get(brand, sender)
    has_create_visit = any(item.get("name") == "create_visit" for item in tool_results)
    if not has_create_visit and booking_next_required_step(cfg, current_state) == "ready_to_book":
        result = handle_tool(
            brand,
            sender,
            cfg,
            "create_visit",
            {
                "service_id": current_state.get("service_id"),
                "date_str": current_state.get("date"),
                "time_str": current_state.get("time"),
                "employee_id": current_state.get("employee_id") or "",
                "name": current_state.get("name"),
                "phone": current_state.get("phone"),
            },
        )
        try:
            parsed_result = json.loads(result)
        except json.JSONDecodeError:
            parsed_result = {"status": "UNKNOWN_TOOL_RESULT"}
        tool_results.append({
            "name": "create_visit",
            "status": parsed_result.get("status"),
            "raw": parsed_result,
        })
        audit_event(
            "ai_tool_result",
            brand=brand,
            sender=sender,
            appointment_id=parsed_result.get("appointment_id"),
            correlation_id=correlation_id,
            actor="runtime",
            payload={
                "tool": "create_visit",
                "status": parsed_result.get("status"),
                "idempotent": bool(parsed_result.get("idempotent")),
                "telegram_notified": parsed_result.get("telegram_notified"),
            },
        )
        current_state = legacy.state_get(brand, sender)

    reply = guard_ai_reply(cfg, current_state, reply, tool_results)
    reply = sanitize_reply(cfg, current_state, reply)
    reply = enforce_flow_gate(cfg, current_state, reply)

    booking_result = next(
        (item.get("raw", {}) for item in reversed(tool_results)
         if item.get("name") == "create_visit"),
        {},
    )
    if booking_result.get("status") == "MANUAL_FALLBACK" and "telegram_notified" in booking_result:
        if booking_result["telegram_notified"]:
            requested_time = str(current_state.get("time") or "").strip()
            reply = (
                f"Зараз перевірю бажаний час — {requested_time} — і напишу вам, щойно уточню 🌷"
                if requested_time
                else "Зараз перевірю бажаний час і напишу вам, щойно уточню 🌷"
            )
        else:
            reply = (
                "Заявку збережено, але запит на перевірку часу не вдалося надіслати. "
                "Запис ще не підтверджено; спробуйте, будь ласка, написати трохи пізніше."
            )
    elif booking_result.get("status") == "LOCAL_PERSISTENCE_ERROR":
        reply = booking_result.get("message") or "Не вдалося зберегти заявку. Спробуйте ще раз трохи пізніше."

    legacy.save_message(brand, sender, "assistant", reply)
    return reply


legacy.handle_tool = handle_tool
legacy.process_with_ai = process_with_ai
legacy.crm_type = lambda cfg: str(cfg.get("crm_type") or cfg.get("crm", {}).get("type") or "manual").lower()


# ---------------------------------------------------------------------------
# Persistent inbound queue / debounce
# ---------------------------------------------------------------------------


def enqueue_message(brand: str, sender: str, text: str) -> None:
    if not text:
        return
    with legacy.db() as conn:
        conn.execute(
            "INSERT INTO message_queue(brand,sender_id,text,created_at) VALUES(?,?,?,?)",
            (brand, sender, text[:8000], time.time()),
        )


def _outbound_key(
    brand: str,
    sender: str,
    queue_ids: list[int],
) -> str:
    raw = "|".join(
        [brand, sender, ",".join(str(item) for item in sorted(queue_ids))]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _claim_outbound_delivery(
    brand: str,
    sender: str,
    queue_ids: list[int],
) -> tuple[str, str]:
    key = _outbound_key(brand, sender, queue_ids)
    now = time.time()
    message_hash = hashlib.sha256(
        "|".join(str(item) for item in sorted(queue_ids)).encode("utf-8")
    ).hexdigest()

    with legacy.db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT status, updated_at
            FROM outbound_messages
            WHERE idempotency_key=?
            """,
            (key,),
        ).fetchone()

        if row is None:
            conn.execute(
                """
                INSERT INTO outbound_messages(
                    idempotency_key, brand, sender_id, message_hash,
                    status, created_at, updated_at
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (key, brand, sender, message_hash, "SENDING", now, now),
            )
            conn.commit()
            return key, "SEND"

        status, updated_at = row
        age = now - float(updated_at or 0)
        if status == "SENT":
            conn.commit()
            return key, "ALREADY_SENT"

        if status == "SENDING" and age < OUTBOUND_RETRY_SECONDS:
            conn.commit()
            return key, "IN_PROGRESS"

        conn.execute(
            """
            UPDATE outbound_messages
            SET status='SENDING', updated_at=?
            WHERE idempotency_key=?
            """,
            (now, key),
        )
        conn.commit()

    return key, "SEND"


def _finish_outbound_delivery(key: str, success: bool) -> None:
    with legacy.db() as conn:
        if success:
            conn.execute(
                """
                UPDATE outbound_messages
                SET status='SENT', updated_at=?
                WHERE idempotency_key=?
                """,
                (time.time(), key),
            )
        else:
            conn.execute(
                """
                UPDATE outbound_messages
                SET status='PENDING', updated_at=?
                WHERE idempotency_key=?
                """,
                (time.time(), key),
            )


def _claim_queue_rows(limit: int = 100) -> list[tuple[int, str, str, str]]:
    now = time.time()
    cutoff = now - float(os.getenv("DEBOUNCE_SECONDS", "1.0"))
    reclaim_before = now - QUEUE_RETRY_SECONDS
    with legacy.db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            """
            SELECT id, brand, sender_id, text
            FROM message_queue
            WHERE processed_at IS NULL
              AND ((claimed_at IS NULL AND created_at <= ?) OR claimed_at < ?)
            ORDER BY id
            LIMIT ?
            """,
            (cutoff, reclaim_before, limit),
        ).fetchall()
        if rows:
            ids = [row[0] for row in rows]
            marks = ",".join("?" for _ in ids)
            conn.execute(
                f"UPDATE message_queue SET claimed_at=? WHERE id IN ({marks})",
                [now, *ids],
            )
        conn.commit()
    return rows


def queue_worker() -> None:
    while True:
        try:
            rows = _claim_queue_rows()
            if not rows:
                time.sleep(QUEUE_POLL_SECONDS)
                continue
            grouped: Dict[tuple[str, str], list[tuple[int, str]]] = {}
            for row_id, brand, sender, text in rows:
                grouped.setdefault((brand, sender), []).append((row_id, text))
            for (brand, sender), items in grouped.items():
                combined = " ".join(text for _, text in items).strip()
                outbound_key = None
                try:
                    reply = process_with_ai(brand, sender, combined)
                    outbound_key, delivery_state = _claim_outbound_delivery(
                        brand,
                        sender,
                        [row_id for row_id, _ in items],
                    )

                    if delivery_state == "ALREADY_SENT":
                        delivery_ok = True
                    elif delivery_state == "IN_PROGRESS":
                        delivery_ok = False
                        LOGGER.warning(
                            "Outbound Instagram delivery already in progress for %s/%s",
                            brand,
                            sender,
                        )
                    else:
                        delivery_ok = True
                        if reply:
                            try:
                                legacy.instagram_send(
                                    legacy.cfg_for(brand),
                                    sender,
                                    reply,
                                )
                            except Exception:
                                delivery_ok = False
                                raise
                        _finish_outbound_delivery(outbound_key, True)

                    if delivery_ok:
                        with legacy.db() as conn:
                            marks = ",".join("?" for _ in items)
                            conn.execute(
                                f"UPDATE message_queue SET processed_at=? WHERE id IN ({marks})",
                                [time.time(), *[row_id for row_id, _ in items]],
                            )
                except Exception as exc:
                    LOGGER.exception("Queued message processing failed")
                    if outbound_key:
                        _finish_outbound_delivery(outbound_key, False)
                    legacy.telegram(
                        legacy.cfg_for(brand),
                        f"⚠️ BeautyBridge error {brand}: {exc}",
                    )
                    with legacy.db() as conn:
                        marks = ",".join("?" for _ in items)
                        conn.execute(
                            f"UPDATE message_queue SET claimed_at=NULL WHERE id IN ({marks})",
                            [row_id for row_id, _ in items],
                        )
        except Exception:
            LOGGER.exception("Queue worker failure")
            time.sleep(QUEUE_POLL_SECONDS)


def buffer_message(brand: str, sender: str, text: str) -> None:
    enqueue_message(brand, sender, text)


legacy.buffer_message = buffer_message


# ---------------------------------------------------------------------------
# Secure webhook / payments
# ---------------------------------------------------------------------------


def verify_meta_signature(raw_body: bytes, signature_header: str) -> bool:
    secret = config.META_APP_SECRET
    if not secret or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    provided = signature_header.split("=", 1)[1]
    return hmac.compare_digest(expected, provided)


def send_admin_action_message(
    cfg: dict,
    appointment_id: int,
    text: str,
    buttons: list[tuple[str, str]],
) -> bool:
    """Send a Ukrainian admin message with inline workflow buttons."""
    chat = cfg.get("telegram_chat_id") or config.ADMIN_CHAT_ID
    if not chat:
        LOGGER.error(
            "Telegram admin action message skipped for appointment %s: chat id is not configured",
            appointment_id,
        )
        return False
    if not config.TELEGRAM_BOT_TOKEN:
        LOGGER.error(
            "Telegram admin action message skipped for appointment %s: bot token is not configured",
            appointment_id,
        )
        return False
    markup = {
        "inline_keyboard": [
            [{"text": label, "callback_data": data}]
            for label, data in buttons
        ]
    }
    try:
        sent = bool(
            legacy.telegram_api(
                "sendMessage",
                {
                    "chat_id": chat,
                    "text": str(text)[:4000],
                    "reply_markup": markup,
                },
                token=config.TELEGRAM_BOT_TOKEN,
            )
        )
        if not sent:
            LOGGER.error(
                "Telegram admin action message was not delivered for appointment %s",
                appointment_id,
            )
        return sent
    except Exception:
        LOGGER.exception(
            "Telegram admin action message failed for appointment %s",
            appointment_id,
        )
        return False


PENDING_NOTIFICATION_RETRY_INTERVAL_SECONDS = max(
    5, int(os.getenv("PENDING_NOTIFICATION_RETRY_INTERVAL_SECONDS", "30"))
)
_last_pending_notification_retry = 0.0


def retry_pending_admin_notifications(limit: int = 50, force: bool = False) -> int:
    """Retry durable Telegram delivery for saved manual requests that are still pending."""
    global _last_pending_notification_retry
    now = time.monotonic()
    if not force and now - _last_pending_notification_retry < PENDING_NOTIFICATION_RETRY_INTERVAL_SECONDS:
        return 0
    _last_pending_notification_retry = now

    with legacy.db() as conn:
        rows = conn.execute(
            """
            SELECT id, brand, sender_id, name, phone, service_name,
                   appointment_date, appointment_time, employee_id, master_name
            FROM appointments
            WHERE status='pending_manual_confirmation'
              AND (
                  notes LIKE '%TELEGRAM_NOTIFICATION_PENDING%'
                  OR (
                      notes LIKE '%TELEGRAM_NOTIFICATION_IN_PROGRESS%'
                      AND created_at <= datetime('now', '-60 seconds')
                  )
              )
            ORDER BY id
            LIMIT ?
            """,
            (max(1, min(int(limit), 200)),),
        ).fetchall()

    delivered = 0
    for row in rows:
        (
            appointment_id, brand, sender_id, client_name, phone, service_name,
            appointment_date, appointment_time, employee_id, master_name,
        ) = row
        cfg = legacy.cfg_for(str(brand))
        message = "\n".join([
            "📝 НОВА ЗАЯВКА — перевірити бажаний час",
            f"Салон: {cfg.get('name')}",
            f"Клієнт: {client_name or '—'}",
            f"Телефон: {phone or '—'}",
            f"Послуга: {service_name or '—'}",
            f"Майстер: {master_name or employee_id or '—'}",
            f"Бажана дата: {appointment_date or '—'}",
            f"Бажаний / орієнтовний час: {appointment_time or '—'}",
            "Статус: очікує перевірки. Підтвердження клієнту ще не надсилалось.",
        ])
        sent = send_admin_action_message(
            cfg,
            int(appointment_id),
            message,
            [
                ("✅ Час доступний", f"bb:time_ok:{appointment_id}"),
                ("🕒 Час зайнятий", f"bb:time_busy:{appointment_id}"),
            ],
        )
        update_appointment(
            int(appointment_id),
            notes=(
                "Awaiting admin slot verification via Telegram; no CRM integration. | "
                + ("TELEGRAM_NOTIFICATION_SENT" if sent else "TELEGRAM_NOTIFICATION_PENDING")
            ),
        )
        if sent:
            delivered += 1
    return delivered


def send_admin_telegram(cfg: dict, text: str, photo_url: Optional[str] = None) -> bool:
    chat = cfg.get("telegram_chat_id") or config.ADMIN_CHAT_ID
    if not chat:
        LOGGER.error("Telegram send skipped: chat id is not configured")
        return False
    try:
        if photo_url:
            telegram_api = getattr(legacy, "telegram_api", None)
            if not telegram_api:
                LOGGER.error("Telegram API helper is unavailable")
                return False
            return bool(
                telegram_api(
                    "sendPhoto",
                    {"chat_id": chat, "photo": photo_url, "caption": str(text)[:1000]},
                    token=config.TELEGRAM_BOT_TOKEN,
                )
            )
        return bool(legacy.telegram(cfg, text))
    except Exception:
        LOGGER.exception("Telegram admin notification failed")
        return False


def send_admin_telegram_album(cfg: dict, caption: str, photo_urls: list) -> None:
    """Send up to 10 photos as one Telegram album (media group), with the
    caption attached to the first photo. Falls back to single sendPhoto
    calls if there's only one photo, or to a plain text message if there
    are none - no new long-lived infra, just one more outbound API call."""
    token = config.TELEGRAM_BOT_TOKEN
    chat = cfg.get("telegram_chat_id") or config.ADMIN_CHAT_ID
    if not token or not chat:
        return
    photo_urls = [u for u in photo_urls if u]
    if not photo_urls:
        legacy.telegram(cfg, caption)
        return
    if len(photo_urls) == 1:
        send_admin_telegram(cfg, caption, photo_url=photo_urls[0])
        return
    try:
        telegram_api = getattr(legacy, "telegram_api", None)
        if not telegram_api:
            LOGGER.error("Telegram API helper is unavailable")
            return
        media = [
            {"type": "photo", "media": url, "caption": str(caption)[:1000] if i == 0 else ""}
            for i, url in enumerate(photo_urls[:10])
        ]
        telegram_api(
            "sendMediaGroup",
            {"chat_id": chat, "media": media},
            token=config.TELEGRAM_BOT_TOKEN,
        )
    except Exception:
        LOGGER.exception("Telegram admin album notification failed")


def _payment_receipt(brand: str, sender: str, appointment_id: int, photo_url: Optional[str]) -> None:
    cfg = legacy.cfg_for(brand)
    current = legacy.state_get(brand, sender)
    nails_photo_url = current.get("nails_photo_url")
    row = appointment_row(appointment_id)

    update_appointment(appointment_id, status="receipt_pending_verification", receipt_received=1)
    audit_event(
        "payment_receipt_received",
        brand=brand,
        sender=sender,
        appointment_id=appointment_id,
        actor="instagram",
        payload={"photo_attached": bool(photo_url), "nails_photo_attached": bool(nails_photo_url)},
    )
    strict_state_set(
        brand,
        sender,
        state=BotState.PAYMENT_PENDING_VERIFICATION.value,
        receipt=True,
        payment_confirmed=False,
    )

    if row:
        (
            _id, _brand, sender_id, name, phone, service_name,
            appt_date, appt_time, master_name, *_rest,
        ) = row
        details = (
            f"💳 НОВА ЗАЯВКА НА ПІДТВЕРДЖЕННЯ\n"
            f"Салон: {cfg.get('name')}\n"
            f"Клієнт: {name or '—'} ({phone or '—'})\n"
            f"Instagram ID: {sender_id}\n"
            f"Майстер: {master_name or '—'}\n"
            f"Послуга: {service_name or '—'}\n"
            f"Дата/час: {appt_date or '—'} {appt_time or ''}\n"
            f"Заявка: {appointment_id}\n"
            "Перевірте фото/чек і підтвердіть через admin endpoint."
        )
    else:
        details = (
            f"💳 ЧЕК ОТ КЛІЄНТА\nСалон: {cfg.get('name')}\nЗаявка: {appointment_id}\n"
            f"Клієнт: {sender}\nПеревірте оплату та підтвердіть через admin endpoint."
        )

    send_admin_telegram_album(cfg, details, [nails_photo_url, photo_url])
    send_admin_action_message(
        cfg,
        appointment_id,
        "Оберіть дію:",
        [
            ("✅ Передоплату підтверджено", f"bb:payment_ok:{appointment_id}"),
            ("❌ Передоплату не підтверджено", f"bb:payment_bad:{appointment_id}"),
        ],
    )

    try:
        when = f"{row[6]} {row[7]}" if row else ""
        legacy.instagram_send(
            cfg,
            sender,
            f"Дякуємо! Адміністратор підтвердить ваш запис{' на ' + when if when.strip() else ''} 🤍",
        )
    except Exception:
        LOGGER.exception("Failed to acknowledge receipt")


_telegram_update_offset = 0


def _admin_chat_id(cfg: dict) -> str:
    return str(cfg.get("telegram_chat_id") or config.ADMIN_CHAT_ID or "")


def _find_appointment_brand(appointment_id: int):
    row = appointment_row(appointment_id)
    if not row:
        return None
    return legacy.cfg_for(str(row[1])), row


def _answer_callback(callback_id: str, text: str) -> None:
    try:
        legacy.telegram_api(
            "answerCallbackQuery",
            {"callback_query_id": callback_id, "text": str(text)[:180]},
            token=config.TELEGRAM_BOT_TOKEN,
        )
    except Exception:
        LOGGER.exception("Telegram callback acknowledgement failed")



_TIME_SLOT_RE = re.compile(
    r"(\d{1,2})[./\-](\d{1,2})(?:[./\-](\d{2,4}))?\s+о?\s*(\d{1,2})[:\.](\d{2})",
    re.IGNORECASE,
)


def _handle_admin_text_message(admin_chat_id: str, text: str) -> None:
    try:
        with legacy.db() as conn:
            row = conn.execute(
                "SELECT id, appointment_id, brand, sender_id FROM admin_pending_replies"
                " WHERE admin_chat_id=? ORDER BY created_at DESC LIMIT 1",
                (admin_chat_id,),
            ).fetchone()
    except Exception:
        LOGGER.exception("Failed to query admin_pending_replies for chat %s", admin_chat_id)
        return

    if not row:
        return

    pending_id, appointment_id, brand, sender = row

    match = _TIME_SLOT_RE.search(text)
    if not match:
        try:
            legacy.telegram_api(
                "sendMessage",
                {"chat_id": admin_chat_id, "text": (
                    "Не вдалося розпізнати час 🤔\n"
                    "Вкажіть у форматі «ДД.ММ о ГГ:ХХ», наприклад: «15.11 о 14:00»"
                )},
                token=config.TELEGRAM_BOT_TOKEN,
            )
        except Exception:
            pass
        return

    day, month, year_raw, hour, minute = match.groups()
    from datetime import date as _date
    current_year = _date.today().year
    year = int(year_raw) if year_raw else current_year
    if year < 100:
        year += 2000
    try:
        slot_date = f"{int(day):02d}.{int(month):02d}.{year}"
        slot_time = f"{int(hour):02d}:{int(minute):02d}"
    except ValueError:
        LOGGER.warning("Unparseable time slot from admin: %r", text)
        return

    found = _find_appointment_brand(appointment_id)
    if not found:
        return
    cfg, appt_row = found
    (
        _id, _brand, _sender, _name, _phone, service_name,
        _orig_date, _orig_time, master_name, status, *_rest
    ) = appt_row

    if status != "waiting_admin_alternative":
        try:
            with legacy.db() as conn:
                conn.execute("DELETE FROM admin_pending_replies WHERE id=?", (pending_id,))
        except Exception:
            pass
        return

    master_line = f"Майстер: {master_name}\n" if master_name else ""
    client_msg = (
        f"На жаль, той час зайнятий \U0001f90d Але є вільний слот:\n\n"
        f"\U0001f4c5 {slot_date} о {slot_time}\n"
        f"{master_line}"
        f"\nВам підходить? Напишіть «Так» для підтвердження або «Ні», якщо хочете інший час."
    )
    try:
        legacy.instagram_send(cfg, sender, client_msg)
    except Exception:
        LOGGER.exception("Failed to send alternative time to client for appointment %s", appointment_id)
        try:
            legacy.telegram_api(
                "sendMessage",
                {"chat_id": admin_chat_id, "text": "\u274c Не вдалося надіслати клієнту. Спробуйте ще раз."},
                token=config.TELEGRAM_BOT_TOKEN,
            )
        except Exception:
            pass
        return

    update_appointment(appointment_id, status="waiting_client_confirmation",
                       appointment_date=slot_date, appointment_time=slot_time)
    strict_state_set(
        brand, sender,
        state=BotState.WAITING_CLIENT_TIME_CONFIRMATION.value,
        appointment_id=appointment_id,
        date=slot_date,
        time=slot_time,
    )

    try:
        with legacy.db() as conn:
            conn.execute("DELETE FROM admin_pending_replies WHERE id=?", (pending_id,))
    except Exception:
        LOGGER.exception("Failed to clear admin_pending_replies row %s", pending_id)

    try:
        legacy.telegram_api(
            "sendMessage",
            {"chat_id": admin_chat_id, "text": f"\u2705 Запропонований час {slot_date} о {slot_time} надіслано клієнту."},
            token=config.TELEGRAM_BOT_TOKEN,
        )
    except Exception:
        LOGGER.exception("Failed to confirm alternative time to admin")

def _handle_admin_callback(data: str) -> str:
    parts = str(data or "").split(":")
    if len(parts) != 3 or parts[0] != "bb":
        return "Невідома дія."
    action, raw_id = parts[1], parts[2]
    try:
        appointment_id = int(raw_id)
    except ValueError:
        return "Некоректний номер заявки."

    found = _find_appointment_brand(appointment_id)
    if not found:
        return "Заявку не знайдено."
    cfg, row = found
    (
        _id, brand, sender, _name, _phone, service_name, appt_date,
        appt_time, master_name, status, _paid, _receipt, _crm_id
    ) = row

    if action == "time_ok":
        if status != "pending_manual_confirmation":
            return "Цю заявку вже оброблено."
        try:
            legacy.instagram_send(
                cfg,
                sender,
                "\n".join([
                    f"Маю для вас час на {service_name.lower()} 🌷",
                    f"📅 {appt_date} о {appt_time}",
                    f"Майстер: {master_name}",
                    "",
                    "Підходить вам цей час? Напишіть «Так» для підтвердження або «Ні», якщо потрібно підібрати інший 🤍",
                ]),
            )
        except Exception:
            LOGGER.exception("Failed to ask client to confirm proposed time")
            return "Не вдалося надіслати клієнту час. Заявка залишилася на перевірці — спробуйте ще раз."

        # Only advance the lifecycle after Instagram accepted the outgoing message.
        # Otherwise the admin must be able to retry the same Telegram action.
        update_appointment(appointment_id, status="waiting_client_confirmation")
        strict_state_set(
            brand, sender,
            state=BotState.WAITING_CLIENT_TIME_CONFIRMATION.value,
            appointment_id=appointment_id,
        )
        return "Запропонований час надіслано клієнту на підтвердження."

    if action == "time_busy":
        if status != "pending_manual_confirmation":
            return "Цю заявку вже оброблено."
        admin_chat = _admin_chat_id(cfg)
        try:
            with legacy.db() as conn:
                conn.execute("DELETE FROM admin_pending_replies WHERE appointment_id=?", (appointment_id,))
                conn.execute(
                    "INSERT INTO admin_pending_replies(appointment_id, brand, sender_id, admin_chat_id) VALUES(?,?,?,?)",
                    (appointment_id, brand, sender, admin_chat),
                )
        except Exception:
            LOGGER.exception("Failed to save admin_pending_replies for appointment %s", appointment_id)
            return "Не вдалося зберегти стан очікування. Спробуйте ще раз."
        update_appointment(appointment_id, status="waiting_admin_alternative")
        try:
            legacy.telegram_api(
                "sendMessage",
                {"chat_id": admin_chat, "text": (
                    f"\u23f0 Час для заявки #{appointment_id} зайнятий.\n\n"
                    "Напишіть у цей чат вільний час у форматі «ДД.ММ о ГГ:ХХ» "
                    "(наприклад: «15.11 о 14:00»), і бот одразу запропонує його клієнту."
                )},
                token=config.TELEGRAM_BOT_TOKEN,
            )
        except Exception:
            LOGGER.exception("Failed to ask admin for alternative time")
            return "Статус оновлено, але не вдалося надіслати запит адміну."
        return "Час зайнятий. Напишіть у цей чат вільний слот — бот надішле його клієнту."

    if action == "payment_ok":
        if status != "receipt_pending_verification":
            return "Чек ще не очікує перевірки або вже оброблений."
        update_appointment(
            appointment_id,
            paid=1,
            status="confirmed",
            receipt_received=1,
        )
        strict_state_set(
            brand, sender,
            state=BotState.BOOKED_CONFIRMED.value,
            appointment_id=appointment_id,
            payment_confirmed=True,
            receipt_confirmed=True,
        )
        try:
            send_after_payment_confirmed(cfg, sender)
        except Exception:
            LOGGER.exception("Failed to send final payment confirmation")
        return "Передоплату підтверджено, запис завершено."


    if action == "payment_bad":
        if status != "receipt_pending_verification":
            return "Чек ще не очікує перевірки або вже оброблений."
        update_appointment(
            appointment_id,
            paid=0,
            status="booked_awaiting_payment",
            receipt_received=0,
        )
        strict_state_set(
            brand, sender,
            state=BotState.WAITING_PAYMENT.value,
            appointment_id=appointment_id,
            payment_confirmed=False,
            receipt_confirmed=False,
        )
        try:
            legacy.instagram_send(
                cfg,
                sender,
                "Не вдалося підтвердити передоплату.\nБудь ласка, надішліть актуальне фото чека.",
            )
        except Exception:
            LOGGER.exception("Failed to send payment rejection")
        return "Передоплату не підтверджено."

    if action == "payment_wait":
        return "Заявка очікує передоплату."

    return "Невідома дія."


def telegram_poll_once() -> None:
    global _telegram_update_offset
    token = config.TELEGRAM_BOT_TOKEN
    if not token:
        return
    try:
        response = None
        for attempt in range(2):
            try:
                response = requests.get(
                    f"https://api.telegram.org/bot{token}/getUpdates",
                    params={
                        "offset": _telegram_update_offset,
                        "timeout": 5,
                        "allowed_updates": ["callback_query", "message"],
                    },
                    timeout=10,
                )
            except requests.exceptions.RequestException as exc:
                if attempt == 0:
                    LOGGER.warning(
                        "Telegram getUpdates network error; retrying once: %s",
                        type(exc).__name__,
                    )
                    time.sleep(1)
                    continue
                LOGGER.warning(
                    "Telegram getUpdates unavailable after retry: %s",
                    type(exc).__name__,
                )
                return

            if response.status_code >= 500 and attempt == 0:
                LOGGER.warning(
                    "Telegram getUpdates returned HTTP %s; retrying once",
                    response.status_code,
                )
                time.sleep(1)
                continue
            if response.status_code >= 300:
                LOGGER.error("Telegram getUpdates failed: HTTP %s", response.status_code)
                return
            break

        if response is None:
            return
        payload = response.json()
        for update in payload.get("result") or []:
            _telegram_update_offset = max(
                _telegram_update_offset,
                int(update.get("update_id", 0)) + 1,
            )
            tg_message = update.get("message") or {}
            if tg_message and not update.get("callback_query"):
                tg_chat_id = str((tg_message.get("chat") or {}).get("id") or "")
                tg_text = str(tg_message.get("text") or "").strip()
                if tg_chat_id and tg_text:
                    _handle_admin_text_message(tg_chat_id, tg_text)
                continue

            callback = update.get("callback_query") or {}
            message = callback.get("message") or {}
            chat_id = str((message.get("chat") or {}).get("id") or "")
            data = str(callback.get("data") or "")
            if not chat_id or not data:
                continue
            if data == "bb:test:ok":
                if chat_id == str(config.ADMIN_CHAT_ID or "") and chat_id:
                    LOGGER.info("Telegram test callback received from admin")
                    _answer_callback(
                        str(callback.get("id") or ""),
                        "Тест успішний: callback отримано.",
                    )
                else:
                    _answer_callback(
                        str(callback.get("id") or ""),
                        "Доступ заборонено.",
                    )
                continue

            parts = data.split(":")
            authorized = False
            if len(parts) == 3 and parts[0] == "bb":
                try:
                    found = _find_appointment_brand(int(parts[2]))
                    authorized = bool(found and chat_id == _admin_chat_id(found[0]))
                except ValueError:
                    authorized = False
            if not authorized:
                _answer_callback(str(callback.get("id") or ""), "Доступ заборонено.")
                continue
            result = _handle_admin_callback(data)
            _answer_callback(str(callback.get("id") or ""), result)
    except Exception:
        LOGGER.exception("Telegram admin polling failed")


def webhook():
    verify_token = config.VERIFY_TOKEN
    if not verify_token:
        return "Webhook verification token is not configured", 503
    if request.args.get("hub.verify_token") != verify_token and request.method == "GET":
        return "Forbidden", 403
    if request.method == "GET":
        return request.args.get("hub.challenge", ""), 200

    raw_body = request.get_data()
    if REQUIRE_META_SIGNATURE:
        if not config.META_APP_SECRET:
            LOGGER.error("META_APP_SECRET is required but missing")
            return "Webhook signature secret is not configured", 503
        if not verify_meta_signature(raw_body, request.headers.get("X-Hub-Signature-256", "")):
            return "Forbidden", 403

    payload = request.get_json(silent=True) or {}
    for entry in payload.get("entry", []):
        entry_page_id = str(entry.get("id") or "")
        for event in entry.get("messaging", []):
            msg = event.get("message") or {}
            if msg.get("is_echo"):
                continue
            page_id = str((event.get("recipient") or {}).get("id") or entry_page_id)
            brand = legacy.brand_by_page(page_id)
            sender = str((event.get("sender") or {}).get("id") or "")
            if not brand or not sender:
                continue

            mid = f"{brand}:{msg.get('mid') or time.time_ns()}"
            if not legacy.mark_event(mid):
                continue

            audit_event(
                "meta_message_received",
                brand=brand,
                sender=sender,
                actor="meta",
                payload={
                    "message_id": mid,
                    "has_text": bool(msg.get("text")),
                    "has_attachments": bool(msg.get("attachments")),
                },
            )

            cfg = legacy.cfg_for(brand)
            current = legacy.state_get(brand, sender)
            text = (msg.get("text") or "").strip()
            photo_url = None
            has_image = False
            for attachment in msg.get("attachments") or []:
                if attachment.get("type") == "image":
                    has_image = True
                    photo_url = (attachment.get("payload") or {}).get("url")
                    break

            if has_image:
                text_suffix = "[клієнт надіслав фото]"
                if current.get("state") == BotState.WAITING_PAYMENT.value and current.get("appointment_id"):
                    _payment_receipt(brand, sender, int(current["appointment_id"]), photo_url)
                    text_suffix = "[клієнт надіслав чек передоплати]"
                else:
                    strict_state_set(brand, sender, photo=True, nails_photo_url=photo_url)
                text = f"{text} {text_suffix}".strip()

            if text:
                enqueue_message(brand, sender, text)

    return "OK", 200


legacy.app.view_functions["webhook"] = webhook


# ---------------------------------------------------------------------------
# Idempotent scheduler
# ---------------------------------------------------------------------------


def _claim_daily_job(job_key: str) -> bool:
    with legacy.db() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO scheduler_runs(job_key) VALUES(?)",
            (job_key,),
        )
        return cur.rowcount == 1


def daily_tasks() -> None:
    """Send reminders/re-engagement messages and mark them only after success.

    The scheduler can run hourly. Per-appointment flags are the idempotency
    guard, so a transient Meta failure remains retryable on the next run.
    """
    tz = ZoneInfo(config.LOCAL_TZ)
    today = datetime.now(tz).date()
    tomorrow = (today + timedelta(days=1)).isoformat()

    with legacy.db() as conn:
        reminder_rows = conn.execute(
            """
            SELECT id, brand, sender_id, appointment_time, service_name, master_name
            FROM appointments
            WHERE appointment_date=?
              AND status='confirmed'
              AND reminder_sent=0
            ORDER BY id
            """,
            (tomorrow,),
        ).fetchall()

        for appointment_id, brand, sender, tm, service, master in reminder_rows:
            cfg = legacy.cfg_for(brand)
            try:
                legacy.instagram_send(
                    cfg,
                    sender,
                    f"Нагадуємо про запис завтра о {tm} 💅\n{service}\nМайстер: {master}",
                )
            except Exception:
                LOGGER.exception("Reminder failed for %s; will retry", appointment_id)
            else:
                conn.execute(
                    "UPDATE appointments SET reminder_sent=1 WHERE id=?",
                    (appointment_id,),
                )

        for brand, cfg in config.BRANDS.items():
            if not cfg.get("enabled"):
                continue

            target = (
                today - timedelta(days=int(cfg.get("follow_up_days", 21)))
            ).isoformat()
            rows = conn.execute(
                """
                SELECT DISTINCT sender_id, name
                FROM appointments
                WHERE brand=?
                  AND appointment_date=?
                  AND status='confirmed'
                  AND reinvite_sent=0
                """,
                (brand, target),
            ).fetchall()

            for sender, name in rows:
                future = conn.execute(
                    """
                    SELECT 1 FROM appointments
                    WHERE brand=? AND sender_id=?
                      AND appointment_date > ?
                      AND status IN ('confirmed','booked_awaiting_payment')
                    LIMIT 1
                    """,
                    (brand, sender, today.isoformat()),
                ).fetchone()

                if future:
                    conn.execute(
                        """
                        UPDATE appointments
                        SET reinvite_sent=1
                        WHERE brand=? AND sender_id=? AND appointment_date=?
                        """,
                        (brand, sender, target),
                    )
                    continue

                try:
                    legacy.instagram_send(
                        cfg,
                        sender,
                        f"Привіт, {name or ''}! 👋 Минуло {cfg.get('follow_up_days',21)} днів. Запросити вас на наступну процедуру? ✨",
                    )
                except Exception:
                    LOGGER.exception("Retention message failed for %s; will retry", sender)
                else:
                    conn.execute(
                        """
                        UPDATE appointments
                        SET reinvite_sent=1
                        WHERE brand=? AND sender_id=? AND appointment_date=?
                        """,
                        (brand, sender, target),
                    )


legacy.daily_tasks = daily_tasks


# ---------------------------------------------------------------------------
# Admin API / onboarding
# ---------------------------------------------------------------------------


def admin_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        token = config.ADMIN_API_TOKEN
        if not token:
            return jsonify({"error": "ADMIN_API_TOKEN is not configured"}), 503
        provided = request.headers.get("X-Admin-Token", "")
        if not hmac.compare_digest(provided, token):
            return jsonify({"error": "unauthorized"}), 401
        return fn(*args, **kwargs)
    return wrapped


def export_appointments_csv():
    """Excel-compatible export of the canonical local appointment table."""
    brand = str(request.args.get("brand") or "").strip()
    if brand:
        legacy.cfg_for(brand)

    sql = """
        SELECT id, brand, sender_id, name, phone, service_name,
               appointment_date, appointment_time, master_name,
               status, paid, receipt_received, crm_visit_id, notes, created_at
        FROM appointments
    """
    params = []
    if brand:
        sql += " WHERE brand=?"
        params.append(brand)
    sql += " ORDER BY appointment_date DESC, appointment_time DESC, id DESC"

    with legacy.db() as conn:
        rows = conn.execute(sql, params).fetchall()

    output = io.StringIO()
    writer = csv.writer(output, dialect="excel")
    writer.writerow([
        "id", "brand", "instagram_id", "name", "phone", "service",
        "date", "time", "master", "status", "paid", "receipt_received",
        "crm_visit_id", "notes", "created_at",
    ])
    writer.writerows(rows)

    return legacy.app.response_class(
        output.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=beautybridge_appointments.csv"},
    )

def admin_config_v21():
    payload = {}
    for brand, cfg in config.BRANDS.items():
        adapter = adapter_for(cfg)
        requested = str(cfg.get("configured_crm_type") or cfg.get("crm_type") or "")
        payload[brand] = {
            "name": cfg.get("name"),
            "language": cfg.get("language"),
            "crm_required": True,
            "booking_mode": cfg.get("booking_mode"),
            "booking_backend": cfg.get("booking_backend"),
            "crm_type": requested or None,
            "adapter": adapter.name,
            "capabilities": sorted(adapter.capabilities),
            "services": len(cfg.get("services", {})),
            "masters": len(cfg.get("masters", {})),
            "priority_hours": cfg.get("priority_hours", []),
            "prepayment_required": bool(cfg.get("prepayment_required")),
            "follow_up_days": cfg.get("follow_up_days", 21),
            "instagram_connected": bool(cfg.get("page_id") and cfg.get("page_access_token")),
        }
    return jsonify(payload)


def onboarding_validate():
    data = request.get_json(silent=True) or {}
    errors = []
    client_id = str(data.get("id") or "").strip()
    name = str(data.get("name") or "").strip()
    booking_mode = str(data.get("booking_mode") or "").strip().lower()
    crm_type = str(data.get("crm_type") or "").strip().lower()
    language = str(data.get("language") or "uk").strip().lower()
    services = data.get("services") or {}
    masters = data.get("masters") or {}
    master_services = data.get("master_services") or {}

    if not re.fullmatch(r"[a-z0-9_-]{2,64}", client_id):
        errors.append("id must contain only a-z, 0-9, _ or -")
    if not name:
        errors.append("name is required")
    if booking_mode not in {"crm", "table"}:
        errors.append("booking_mode is required: 'crm' or 'table'")
    if booking_mode == "crm":
        if not crm_type:
            errors.append("crm_type is required when booking_mode=crm")
        elif crm_type in {"manual", "none", "home_master", "table"}:
            errors.append("booking_mode=crm requires a real CRM provider")
        elif crm_type not in KNOWN_CRM_TYPES:
            errors.append(f"unknown crm_type: {crm_type}")
    if language not in LANGUAGE_NAMES:
        errors.append(f"unsupported language: {language}")
    if not isinstance(services, dict):
        errors.append("services must be an object")
    if not isinstance(masters, dict):
        errors.append("masters must be an object")
    if not isinstance(master_services, dict):
        errors.append("master_services must be an object")

    normalized = {
        "id": client_id,
        "name": name,
        "booking_mode": booking_mode,
        "booking_backend": "crm" if booking_mode == "crm" else "table",
        "crm_required": True,
        "crm_type": crm_type or None,
        "automatic_booking": booking_mode == "crm" and crm_type in SUPPORTED_AUTO_CRM,
        "manual_fallback": booking_mode == "table",
        "crm_install_options": data.get("crm_install_options", ["altegio", "yclients", "easyweek", "custom_api"]),
        "message_if_no_crm": "Connect/install a supported CRM, or continue temporarily with table mode.",
    }
    return jsonify({"valid": not errors, "errors": errors, "normalized": normalized}), (400 if errors else 200)

def confirm_payment(appointment_id: int):
    row = appointment_row(appointment_id)
    if not row:
        return jsonify({"error": "appointment not found"}), 404
    _, brand, sender, _, _, service_name, appointment_date, appointment_time, master_name, status, _, _, _ = row
    cfg = legacy.cfg_for(brand)

    if status != "receipt_pending_verification":
        return jsonify({"error": "receipt is not pending verification"}), 409

    update_appointment(
        appointment_id,
        paid=1,
        status="confirmed",
        receipt_received=1,
    )
    audit_event(
        "payment_confirmed",
        brand=brand,
        sender=sender,
        appointment_id=appointment_id,
        actor="admin",
        payload={"status": "confirmed", "booking_backend": cfg.get("booking_backend")},
    )
    strict_state_set(
        brand,
        sender,
        state=BotState.BOOKED_CONFIRMED.value,
        appointment_id=appointment_id,
        payment_confirmed=True,
        receipt_confirmed=True,
    )
    send_after_payment_confirmed(cfg, sender)
    return jsonify({"ok": True, "appointment_id": appointment_id, "status": "confirmed"})


def confirm_manual_booking(appointment_id: int):
    row = appointment_row(appointment_id)
    if not row:
        return jsonify({"error": "appointment not found"}), 404
    _, brand, sender, _, _, service_name, appointment_date, appointment_time, master_name, status, _, _, _ = row
    if status != "pending_manual_confirmation":
        return jsonify({
            "error": "appointment is not awaiting admin time verification",
            "status": status,
        }), 409

    cfg = legacy.cfg_for(brand)
    audit_event(
        "manual_booking_time_offer_requested",
        brand=brand,
        sender=sender,
        appointment_id=appointment_id,
        actor="admin",
        payload={"status": status},
    )

    # Admin verification only proposes the time. The appointment is not
    # confirmed until the client explicitly accepts the proposal in Instagram.
    try:
        legacy.instagram_send(
            cfg,
            sender,
            "\n".join([
                f"Маю для вас час на {str(service_name or '').lower()} 🌷",
                f"📅 {appointment_date} о {appointment_time}",
                f"Майстер: {master_name}" if master_name else "",
                "",
                "Підходить вам цей час? Напишіть «Так» для підтвердження або «Ні», якщо потрібно підібрати інший 🤍",
            ]),
        )
    except Exception:
        LOGGER.exception("Failed to send manual time offer")
        return jsonify({"error": "could not deliver time offer; appointment remains pending"}), 502

    update_appointment(appointment_id, status="waiting_client_confirmation")
    strict_state_set(
        brand,
        sender,
        state=BotState.WAITING_CLIENT_TIME_CONFIRMATION.value,
        appointment_id=appointment_id,
    )
    return jsonify({
        "ok": True,
        "appointment_id": appointment_id,
        "status": "waiting_client_confirmation",
    })


legacy.app.add_url_rule(
    "/admin/config/v2",
    endpoint="admin_config_v21",
    view_func=admin_required(admin_config_v21),
    methods=["GET"],
)
legacy.app.add_url_rule(
    "/admin/appointments/export.csv",
    endpoint="export_appointments_csv_v21",
    view_func=admin_required(export_appointments_csv),
    methods=["GET"],
)
legacy.app.add_url_rule(
    "/admin/onboarding/validate",
    endpoint="onboarding_validate",
    view_func=admin_required(onboarding_validate),
    methods=["POST"],
)
def audit_events_endpoint():
    brand = str(request.args.get("brand") or "").strip() or None
    event_type = str(request.args.get("event_type") or "").strip() or None
    try:
        limit = int(request.args.get("limit") or 100)
    except ValueError:
        return jsonify({"error": "limit must be an integer"}), 400
    return jsonify({"events": list_audit_events(brand=brand, event_type=event_type, limit=limit)})


legacy.app.add_url_rule(
    "/admin/audit/events",
    endpoint="audit_events_endpoint",
    view_func=admin_required(audit_events_endpoint),
    methods=["GET"],
)
legacy.app.add_url_rule(
    "/admin/appointments/<int:appointment_id>/confirm-payment",
    endpoint="confirm_payment_v21",
    view_func=admin_required(confirm_payment),
    methods=["POST"],
)
legacy.app.add_url_rule(
    "/admin/appointments/<int:appointment_id>/confirm-booking",
    endpoint="confirm_booking_v21",
    view_func=admin_required(confirm_manual_booking),
    methods=["POST"],
)



def health_v21():
    return jsonify(
        {
            "status": "ok",
            "ai_configured": bool(legacy.ai),
            "brands": [key for key, cfg in config.BRANDS.items() if cfg.get("enabled")],
            "admin_configured": bool(config.ADMIN_API_TOKEN),
            "meta_signature_required": REQUIRE_META_SIGNATURE,
            "meta_signature_configured": bool(config.META_APP_SECRET),
            "queue_worker_enabled": RUN_QUEUE_WORKER,
        }
    )


legacy.app.view_functions["health"] = health_v21


if RUN_QUEUE_WORKER:
    worker = threading.Thread(target=queue_worker, name="beautybridge-queue", daemon=True)
    worker.start()


app = legacy.app

"""
BeautyBridge V2.1 runtime layer.

This module deliberately sits on top of the existing main.py instead of
replacing it. The existing Bookon/Instagram/AI implementation remains intact,
while this layer adds:
- explicit CRM adapter registry (unknown CRM never falls through to Bookon)
- generic Manual / Unsupported CRM modes
- server-side booking validation
- final slot re-check before CRM booking
- persistent SQLite inbound queue for multi-worker deployments
- database migrations for the old schema
- receipt -> admin verification -> confirmed payment flow
- admin endpoints for manual confirmation and payment confirmation
- tenant/onboarding validation
- dynamic language selection from tenant config
- DB-backed daily scheduler idempotency
- safer Meta/admin configuration requirements
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import threading
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
from flow_guard import guard_ai_reply
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

SUPPORTED_AUTO_CRM = {"bookon"}
KNOWN_CRM_TYPES = {
    "manual",
    "home_master",
    "none",
    "bookon",
    "altegio",
    "yclients",
    "easyweek",
    "google_calendar",
    "custom_api",
}


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


class BookonCRMAdapter(CRMAdapter):
    """Bookon is one isolated connector, never the universal default."""

    name = "bookon"
    capabilities = {"availability", "customer_lookup", "booking"}

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self._legacy = legacy.BookonAdapter(cfg)

    def _raw(self, service_id: str, date_str: str) -> dict:
        result = self._legacy._client().get_available_slots_sync(service_id, date_str)
        if not result.get("ok"):
            raise CRMError(result.get("message", "Bookon availability failed"))
        return result.get("data") or {}

    @staticmethod
    def _dt(raw: Any, tz: ZoneInfo) -> datetime:
        value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if value.tzinfo:
            return value.astimezone(tz)
        return value.replace(tzinfo=tz)

    def _all_slots(self, service_id: str, date_str: str) -> list[dict]:
        datetime.strptime(date_str, "%Y-%m-%d")
        local_tz = ZoneInfo(self.cfg.get("local_tz") or config.LOCAL_TZ)
        masters = self.cfg.get("masters", {})
        result = self._raw(service_id, date_str)
        lines: list[dict] = []
        for specialist_id, dates in result.items():
            if not isinstance(dates, dict):
                continue
            for day, blocks in dates.items():
                if day != date_str or not isinstance(blocks, list):
                    continue
                for block in blocks:
                    try:
                        start = self._dt(block["startTime"], local_tz)
                        end = self._dt(block["stopTime"], local_tz)
                    except (KeyError, TypeError, ValueError):
                        continue
                    lines.append(
                        {
                            "employee_id": str(specialist_id),
                            "master": masters.get(str(specialist_id), str(specialist_id)),
                            "date": day,
                            "time": start.strftime("%H:%M"),
                            "end": end.strftime("%H:%M"),
                            "start_iso": start.isoformat(),
                            "end_iso": end.isoformat(),
                        }
                    )
        return lines

    def get_available_slots(self, service_id: str, date_str: str) -> list[dict]:
        slots = self._all_slots(service_id, date_str)
        priority = self.cfg.get("priority_hours") or []

        def in_priority(slot: dict) -> bool:
            hour = slot["time"]
            for entry in priority:
                if isinstance(entry, str) and "-" in entry:
                    start, end = entry.split("-", 1)
                    if start <= hour < end:
                        return True
            return False

        slots.sort(key=lambda x: (0 if in_priority(x) else 1, x["time"], x["master"]))
        limit = max(1, int(self.cfg.get("booking_rules", {}).get("offer_slots_limit", 3)))
        return slots[:limit]

    def is_slot_available(self, service_id, employee_id, date_str, time_str) -> bool:
        service = self.cfg.get("services", {}).get(str(service_id), {})
        duration = int(service.get("duration", 60))
        try:
            wanted = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
            wanted_end = wanted + timedelta(minutes=duration)
        except ValueError:
            return False

        for slot in self._all_slots(service_id, date_str):
            if str(slot["employee_id"]) != str(employee_id):
                continue
            start = datetime.fromisoformat(slot["start_iso"])
            end = datetime.fromisoformat(slot["end_iso"])
            if start.replace(tzinfo=None) <= wanted <= wanted_end <= end.replace(tzinfo=None):
                return True
        return False

    def create_booking(self, employee_id, service_id, date_str, time_str, name, phone) -> str:
        result = self._legacy.book(employee_id, service_id, date_str, time_str, name, phone)
        if not result:
            return ""
        return str(result)


def adapter_for(cfg: dict) -> CRMAdapter:
    requested = str(cfg.get("crm_type") or cfg.get("crm", {}).get("type") or "manual").strip().lower()
    if requested in {"manual", "home_master", "none"}:
        return ManualCRMAdapter(cfg)
    if requested == "bookon":
        return BookonCRMAdapter(cfg)
    return UnsupportedCRMAdapter(cfg, requested)


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
    if masters and employee_id not in masters:
        return False, "Невідомий майстер. Обери майстра з доступного каталогу.", {}

    allowed = master_services.get(employee_id) or []
    if allowed and service_id not in allowed:
        return False, "Обраний майстер не виконує цю послугу.", {}

    if not _date_ok(date_str):
        return False, "Дата має бути коректною і не бути в минулому.", {}
    if not _time_ok(time_str):
        return False, "Час має бути у форматі HH:MM.", {}
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
        if not persisted or persisted != requested_value:
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
    text = f"Записую вас 🌷 Внесіть, будь ласка, передоплату {amount} грн як гарантію запису."
    if card:
        text += f"\nКартка: {card}"
    if owner:
        text += f" ({owner})"
    text += "\nПісля оплати надішліть квитанцію сюди."
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
                "description": "Отримує актуальні слоти з підключеної CRM. Не викликай повторно, коли дата і час уже вибрані.",
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
                    "required": ["service_id", "date_str", "time_str", "employee_id", "name", "phone"],
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
            "employee_id": state.get("employee_id"),
            "name": state.get("name"),
            "phone": state.get("phone"),
        }.items()
        if not value
    ]
    return f"""
Ти — AI-адміністратор {cfg.get('name')}. Відповідай коротко, природно, без роботизованих шаблонів. Основна мова цього салону: {language}. Якщо клієнт явно переходить на іншу мову — відповідай мовою клієнта.
Сьогодні: {datetime.now(ZoneInfo(cfg.get('local_tz') or config.LOCAL_TZ)).strftime('%Y-%m-%d')} ({datetime.now(ZoneInfo(cfg.get('local_tz') or config.LOCAL_TZ)).strftime('%d.%m.%Y')}).
У викликах функцій (date_str) використовуй ЛИШЕ формат РРРР-ММ-ДД (наприклад 2026-09-23), незалежно від того, як дату написав клієнт (\"23 вересня\", \"23.09\", \"завтра\" тощо) — переведи її у цей формат сам, орієнтуючись на сьогоднішню дату вище.

CRM_TYPE={cfg.get('crm_type')}; ADAPTER={crm.name}; CAPABILITIES={capabilities}
STATE={json.dumps(state, ensure_ascii=False)}
MISSING={missing}

ЖОРСТКІ ПРАВИЛА:
1. Не втрачай state. Якщо date/time вже збережені — не показуй слоти повторно тільки через те, що клієнт назвав ім'я або телефон.
2. Нові дані запам'ятовуй через remember_booking.
3. create_visit викликай тільки коли сервіс, дата, час, майстер, ім'я і телефон уже відомі.
4. Якщо у послуги requires_photo=true — спочатку отримай фото.
5. Не говори "успішно записала", поки tool не повернув SUCCESS або MANUAL_FALLBACK.
6. Якщо CRM не підтримується або немає API — збери заявку та передай її адміністратору. Ніколи не вигадуй вільні слоти.
7. Передоплату проси тільки після SUCCESS у CRM або після ручного підтвердження адміністратором.
8. Адресу, телефон салону і Wi-Fi не повідомляй до підтвердження оплати, коли block_address_if_not_paid=true.
9. Після вибору часу та до отримання імені/телефону не повертай клієнта назад до вибору слота.
10. Якщо клієнт питає ціну — користуйся прайсом нижче, не вигадуй іншу ціну.
11. Пріоритетні години салону: {cfg.get('priority_hours') or 'не задані'}.
12. Повторне запрошення після візиту: {cfg.get('follow_up_days', 21)} днів.

ПОСЛУГИ:
{services}

МАЙСТРИ:
{masters}

ПРАЙС:
{cfg.get('price_text', '')}

ПЕРЕДОПЛАТА: {cfg.get('prepayment_amount', 0)} грн.
"""


def sanitize_reply(cfg: dict, state: dict, reply: str) -> str:
    if not cfg.get("block_address_if_not_paid", True):
        return reply
    paid = bool(state.get("receipt_confirmed") or state.get("payment_confirmed"))
    if paid:
        return reply
    cleaned = reply
    for secret in (
        cfg.get("address"),
        cfg.get("phone"),
        cfg.get("wifi"),
        cfg.get("wifi_password"),
    ):
        if secret:
            cleaned = cleaned.replace(str(secret), "")
    return cleaned.strip()


_BOOKING_CLAIM_TERMINAL = {"SUCCESS", "MANUAL_FALLBACK"}

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


def handle_tool(brand: str, sender: str, cfg: dict, name: str, args: dict) -> str:
    state = legacy.state_get(brand, sender)

    if name == "remember_booking":
        updates = {}
        for key, arg_key in (
            ("service_id", "service_id"),
            ("date", "date_str"),
            ("time", "time_str"),
            ("employee_id", "employee_id"),
            ("name", "name"),
            ("phone", "phone"),
        ):
            value = args.get(arg_key)
            if value:
                updates[key] = str(value).strip()
        if updates:
            updates["state"] = BotState.COLLECTING.value
            strict_state_set(brand, sender, **updates)
        return json.dumps({"status": "REMEMBERED", **updates}, ensure_ascii=False)

    if name == "get_available_slots":
        date_str = str(args.get("date_str") or "")
        if not _date_ok(date_str):
            return json.dumps(
                {
                    "status": "INVALID_DATE_FORMAT",
                    "message": f"date_str must be YYYY-MM-DD and not in the past, got: {date_str!r}. Convert whatever the client wrote and call again.",
                },
                ensure_ascii=False,
            )
        adapter = adapter_for(cfg)
        if isinstance(adapter, (ManualCRMAdapter, UnsupportedCRMAdapter)):
            requested = cfg.get("crm_type") or "manual"
            return json.dumps(
                {
                    "status": "MANUAL_MODE",
                    "crm_type": requested,
                    "message": "Live availability is not available for this connector. Collect preferred date/time and create a manual request.",
                    "slots": [],
                },
                ensure_ascii=False,
            )
        try:
            slots = adapter.get_available_slots(str(args["service_id"]), str(args["date_str"]))
            return json.dumps({"status": "SLOTS", "slots": slots}, ensure_ascii=False)
        except Exception as exc:
            LOGGER.exception("Availability lookup failed for %s", brand)
            return json.dumps(
                {
                    "status": "CRM_ERROR",
                    "message": str(exc),
                    "fallback": "manual",
                },
                ensure_ascii=False,
            )

    if name == "create_visit":
        ok, message, cleaned = validate_booking(cfg, state, args)
        if not ok:
            return json.dumps({"status": "VALIDATION_ERROR", "message": message}, ensure_ascii=False)

        service_id = cleaned["service_id"]
        employee_id = cleaned["employee_id"]
        service_name = cfg.get("services", {}).get(service_id, {}).get("name", service_id)
        master_name = cfg.get("masters", {}).get(employee_id, employee_id or "не обрано")
        adapter = adapter_for(cfg)

        booking_key = booking_idempotency_key(
            brand,
            sender,
            service_id,
            employee_id,
            cleaned["date_str"],
            cleaned["time_str"],
            cleaned["name"],
            cleaned["phone"],
        )
        claim = claim_booking(brand, sender, booking_key)
        if not claim["claimed"]:
            if claim["status"] == "SUCCESS":
                return json.dumps(
                    {
                        "status": "SUCCESS",
                        "appointment_id": claim["appointment_id"],
                        "crm_visit_id": claim["crm_visit_id"],
                        "idempotent": True,
                    },
                    ensure_ascii=False,
                )
            if claim["status"] == "MANUAL_FALLBACK":
                return json.dumps(
                    {
                        "status": "MANUAL_FALLBACK",
                        "appointment_id": claim["appointment_id"],
                        "idempotent": True,
                    },
                    ensure_ascii=False,
                )
            if claim["status"] == "CRM_CREATED_PENDING_RECONCILIATION":
                return json.dumps(
                    {
                        "status": "CRM_CREATED_PENDING_RECONCILIATION",
                        "crm_visit_id": claim["crm_visit_id"],
                        "idempotent": True,
                    },
                    ensure_ascii=False,
                )
            if claim["status"] == "RECONCILIATION_REQUIRED":
                return json.dumps(
                    {
                        "status": "RECONCILIATION_REQUIRED",
                        "message": "Предыдущая попытка записи не завершилась корректно. Нужна проверка администратором перед повторной записью.",
                    },
                    ensure_ascii=False,
                )
            if claim["status"] == "IN_PROGRESS":
                return json.dumps(
                    {
                        "status": "BOOKING_IN_PROGRESS",
                        "message": "Ця заявка вже обробляється. Не створюй другий запис.",
                    },
                    ensure_ascii=False,
                )

        if isinstance(adapter, (ManualCRMAdapter, UnsupportedCRMAdapter)):
            reason = getattr(adapter, "requested_type", "manual")
            appt_id = legacy.create_local_appointment(
                brand,
                sender,
                name=cleaned["name"],
                phone=cleaned["phone"],
                service_id=service_id,
                service_name=service_name,
                date=cleaned["date_str"],
                time=cleaned["time_str"],
                employee_id=employee_id,
                master_name=master_name,
                crm_visit_id="",
                status="pending_manual_confirmation",
                notes=f"Manual/unsupported CRM mode: {reason}",
            )
            strict_state_set(
                brand,
                sender,
                state=BotState.WAITING_ADMIN_CONFIRMATION.value,
                appointment_id=appt_id,
                service_id=service_id,
                date=cleaned["date_str"],
                time=cleaned["time_str"],
                employee_id=employee_id,
                name=cleaned["name"],
                phone=cleaned["phone"],
            )
            legacy.telegram(
                cfg,
                "\n".join(
                    [
                        "📝 НОВА MANUAL ЗАЯВКА",
                        f"Салон: {cfg.get('name')}",
                        f"Клієнт: {cleaned['name']} ({cleaned['phone']})",
                        f"Послуга: {service_name}",
                        f"Дата/час: {cleaned['date_str']} {cleaned['time_str']}",
                        f"Майстер: {master_name}",
                        f"CRM: {reason}",
                        f"ID заявки: {appt_id}",
                        "Потрібне підтвердження адміністратором.",
                    ]
                ),
            )
            finalize_booking_claim(booking_key, "MANUAL_FALLBACK", appointment_id=appt_id)
            return json.dumps(
                {
                    "status": "MANUAL_FALLBACK",
                    "appointment_id": appt_id,
                    "service": service_name,
                    "master": master_name,
                },
                ensure_ascii=False,
            )

        if not adapter.is_slot_available(service_id, employee_id, cleaned["date_str"], cleaned["time_str"]):
            release_booking_claim(booking_key)
            return json.dumps(
                {
                    "status": "SLOT_NO_LONGER_AVAILABLE",
                    "message": "Цей час уже недоступний. Запропонуй отримати актуальні слоти ще раз.",
                },
                ensure_ascii=False,
            )

        try:
            crm_id = adapter.create_booking(
                employee_id,
                service_id,
                cleaned["date_str"],
                cleaned["time_str"],
                cleaned["name"],
                cleaned["phone"],
            )
            if not crm_id:
                raise CRMError("CRM booking returned no visit ID")

        except Exception as exc:
            LOGGER.exception("CRM booking failed for %s", brand)
            appt_id = legacy.create_local_appointment(
                brand,
                sender,
                name=cleaned["name"],
                phone=cleaned["phone"],
                service_id=service_id,
                service_name=service_name,
                date=cleaned["date_str"],
                time=cleaned["time_str"],
                employee_id=employee_id,
                master_name=master_name,
                crm_visit_id="",
                status="pending_manual_confirmation",
                notes=f"CRM booking error; verify CRM manually before retrying: {exc}",
            )
            finalize_booking_claim(booking_key, "MANUAL_FALLBACK", appointment_id=appt_id)
            strict_state_set(
                brand,
                sender,
                state=BotState.WAITING_ADMIN_CONFIRMATION.value,
                appointment_id=appt_id,
                service_id=service_id,
                date=cleaned["date_str"],
                time=cleaned["time_str"],
                employee_id=employee_id,
                name=cleaned["name"],
                phone=cleaned["phone"],
            )
            try:
                legacy.telegram(
                    cfg,
                    f"⚠️ CRM booking needs manual verification. Appointment {appt_id}. Error: {exc}",
                )
            except Exception:
                LOGGER.exception("Manual fallback Telegram notification failed")
            return json.dumps(
                {
                    "status": "MANUAL_FALLBACK",
                    "appointment_id": appt_id,
                    "service": service_name,
                    "master": master_name,
                },
                ensure_ascii=False,
            )

        # CRM has created the visit. Any failure after this point must NOT
        # cause a second CRM booking on retry. Mark the claim as a terminal
        # reconciliation state and ask the admin to verify local persistence.
        try:
            status = "booked_awaiting_payment" if cfg.get("prepayment_required") else "confirmed"
            appt_id = legacy.create_local_appointment(
                brand,
                sender,
                name=cleaned["name"],
                phone=cleaned["phone"],
                service_id=service_id,
                service_name=service_name,
                date=cleaned["date_str"],
                time=cleaned["time_str"],
                employee_id=employee_id,
                master_name=master_name,
                crm_visit_id=crm_id,
                status=status,
            )
        except Exception as exc:
            LOGGER.exception("Local appointment persistence failed after CRM success for %s", brand)
            finalize_booking_claim(
                booking_key,
                "CRM_CREATED_PENDING_RECONCILIATION",
                crm_visit_id=str(crm_id),
            )
            strict_state_set(
                brand,
                sender,
                state=BotState.WAITING_ADMIN_CONFIRMATION.value,
                service_id=service_id,
                date=cleaned["date_str"],
                time=cleaned["time_str"],
                employee_id=employee_id,
                name=cleaned["name"],
                phone=cleaned["phone"],
            )
            try:
                legacy.telegram(
                    cfg,
                    f"🚨 CRM created booking {crm_id}, but local persistence failed. Manual reconciliation required. Error: {exc}",
                )
            except Exception:
                LOGGER.exception("CRM reconciliation Telegram notification failed")
            return json.dumps(
                {
                    "status": "CRM_CREATED_PENDING_RECONCILIATION",
                    "crm_id": str(crm_id),
                    "service": service_name,
                    "master": master_name,
                },
                ensure_ascii=False,
            )

        finalize_booking_claim(
            booking_key,
            "SUCCESS",
            appointment_id=appt_id,
            crm_visit_id=str(crm_id),
        )
        strict_state_set(
            brand,
            sender,
            state=(BotState.WAITING_PAYMENT.value if cfg.get("prepayment_required") else BotState.BOOKED_CONFIRMED.value),
            appointment_id=appt_id,
            service_id=service_id,
            date=cleaned["date_str"],
            time=cleaned["time_str"],
            employee_id=employee_id,
            name=cleaned["name"],
            phone=cleaned["phone"],
        )
        try:
            legacy.telegram(
                cfg,
                "\n".join(
                    [
                        "✅ НОВИЙ ЗАПИС",
                        f"Салон: {cfg.get('name')}",
                        f"Клієнт: {cleaned['name']} ({cleaned['phone']})",
                        f"Послуга: {service_name}",
                        f"Дата/час: {cleaned['date_str']} {cleaned['time_str']}",
                        f"Майстер: {master_name}",
                        f"CRM ID: {crm_id}",
                        f"Локальний ID: {appt_id}",
                    ]
                ),
            )
        except Exception:
            LOGGER.exception("Successful booking Telegram notification failed")
        return json.dumps(
            {
                "status": "SUCCESS",
                "appointment_id": appt_id,
                "crm_id": str(crm_id),
                "service": service_name,
                "master": master_name,
                "payment_required": bool(cfg.get("prepayment_required")),
                "payment_instructions": payment_instruction(cfg) if cfg.get("prepayment_required") else "",
            },
            ensure_ascii=False,
        )

    return json.dumps({"status": "UNKNOWN_TOOL"}, ensure_ascii=False)


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
    if not legacy.ai:
        raise RuntimeError("OPENAI_API_KEY is not configured")
    cfg = legacy.cfg_for(brand)
    legacy.save_message(brand, sender, "user", text)
    state = legacy.state_get(brand, sender)
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

    current_state = legacy.state_get(brand, sender)
    reply = guard_ai_reply(cfg, current_state, reply, tool_results)
    reply = sanitize_reply(cfg, current_state, reply)
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
                try:
                    reply = process_with_ai(brand, sender, combined)
                    if reply:
                        legacy.instagram_send(legacy.cfg_for(brand), sender, reply)
                    with legacy.db() as conn:
                        marks = ",".join("?" for _ in items)
                        conn.execute(
                            f"UPDATE message_queue SET processed_at=? WHERE id IN ({marks})",
                            [time.time(), *[row_id for row_id, _ in items]],
                        )
                except Exception as exc:
                    LOGGER.exception("Queued message processing failed")
                    legacy.telegram(legacy.cfg_for(brand), f"⚠️ BeautyBridge error {brand}: {exc}")
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

    try:
        when = f"{row[6]} {row[7]}" if row else ""
        legacy.instagram_send(
            cfg,
            sender,
            f"Дякуємо! Адміністратор підтвердить ваш запис{' на ' + when if when.strip() else ''} 🤍",
        )
    except Exception:
        LOGGER.exception("Failed to acknowledge receipt")


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


def admin_config_v21():
    payload = {}
    for brand, cfg in config.BRANDS.items():
        adapter = adapter_for(cfg)
        requested = str(cfg.get("crm_type") or "manual")
        payload[brand] = {
            "name": cfg.get("name"),
            "language": cfg.get("language"),
            "crm_type": requested,
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
    crm_type = str(data.get("crm_type") or "manual").strip().lower()
    language = str(data.get("language") or "uk").strip().lower()
    services = data.get("services") or {}
    masters = data.get("masters") or {}
    master_services = data.get("master_services") or {}

    if not re.fullmatch(r"[a-z0-9_-]{2,64}", client_id):
        errors.append("id must contain only a-z, 0-9, _ or -")
    if not name:
        errors.append("name is required")
    if crm_type not in KNOWN_CRM_TYPES:
        errors.append(f"unknown crm_type: {crm_type}; it will use manual fallback")
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
        "crm_type": crm_type,
        "language": language,
        "services_count": len(services) if isinstance(services, dict) else 0,
        "masters_count": len(masters) if isinstance(masters, dict) else 0,
        "automatic_booking": crm_type in SUPPORTED_AUTO_CRM,
        "manual_fallback": True,
    }
    return jsonify({"valid": not errors, "errors": errors, "normalized": normalized}), (400 if errors else 200)


def confirm_payment(appointment_id: int):
    row = appointment_row(appointment_id)
    if not row:
        return jsonify({"error": "appointment not found"}), 404
    _, brand, sender, _, _, _, _, _, _, _, _, _, _ = row
    cfg = legacy.cfg_for(brand)
    update_appointment(appointment_id, paid=1, status="confirmed", receipt_received=1)
    audit_event(
        "payment_confirmed",
        brand=brand,
        sender=sender,
        appointment_id=appointment_id,
        actor="admin",
        payload={"status": "confirmed"},
    )
    strict_state_set(brand, sender, state=BotState.BOOKED_CONFIRMED.value, payment_confirmed=True, receipt_confirmed=True)
    send_after_payment_confirmed(cfg, sender)
    return jsonify({"ok": True, "appointment_id": appointment_id, "status": "confirmed"})


def confirm_manual_booking(appointment_id: int):
    row = appointment_row(appointment_id)
    if not row:
        return jsonify({"error": "appointment not found"}), 404
    _, brand, sender, _, _, service_name, appointment_date, appointment_time, master_name, _, _, _, _ = row
    cfg = legacy.cfg_for(brand)
    audit_event(
        "manual_booking_confirmation_requested",
        brand=brand,
        sender=sender,
        appointment_id=appointment_id,
        actor="admin",
        payload={"prepayment_required": bool(cfg.get("prepayment_required"))},
    )
    if cfg.get("prepayment_required"):
        update_appointment(appointment_id, status="booked_awaiting_payment")
        strict_state_set(brand, sender, state=BotState.WAITING_PAYMENT.value, appointment_id=appointment_id)
        try:
            legacy.instagram_send(
                cfg,
                f"✅ Запис підтверджено адміністратором.\n{service_name}\n{appointment_date} о {appointment_time}\nМайстер: {master_name}\n\n{payment_instruction(cfg)}",
            )
        except Exception:
            LOGGER.exception("Failed to send manual booking confirmation")
    else:
        update_appointment(appointment_id, status="confirmed")
        strict_state_set(brand, sender, state=BotState.BOOKED_CONFIRMED.value, appointment_id=appointment_id)
        try:
            legacy.instagram_send(
                cfg,
                sender,
                f"✅ Запис підтверджено!\n{service_name}\n{appointment_date} о {appointment_time}\nМайстер: {master_name}",
            )
        except Exception:
            LOGGER.exception("Failed to send manual confirmation")
    return jsonify({"ok": True, "appointment_id": appointment_id})


legacy.app.add_url_rule(
    "/admin/config/v2",
    endpoint="admin_config_v21",
    view_func=admin_required(admin_config_v21),
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

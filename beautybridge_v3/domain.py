"""Domain enums and validation shared by the V3 services."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import re
from datetime import date, datetime


class BookingStatus(StrEnum):
    DRAFT = "draft"
    PENDING_MANUAL_CONFIRMATION = "pending_manual_confirmation"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    ALTERNATIVE_PROPOSED = "alternative_proposed"


class ConversationStep(StrEnum):
    COLLECTING = "collecting"
    WAITING_ADMIN_CONFIRMATION = "waiting_admin_confirmation"
    COMPLETE = "complete"


@dataclass(frozen=True)
class BookingDetails:
    service_id: str = ""
    requested_date: str = ""
    preferred_time: str = ""
    master_id: str = ""
    client_name: str = ""
    client_phone: str = ""
    photo_url: str = ""
    notes: str = ""

    def missing_fields(self, photo_required: bool = False) -> list[str]:
        required = ["service_id", "requested_date", "preferred_time", "client_name", "client_phone"]
        if photo_required:
            required.append("photo_url")
        return [key for key in required if not getattr(self, key).strip()]


def validate_date(value: str) -> bool:
    try:
        parsed = date.fromisoformat(str(value))
        return parsed >= date.today()
    except (TypeError, ValueError):
        return False


def normalize_phone(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    plus = raw.startswith("+")
    digits = re.sub(r"\D", "", raw)
    if not 7 <= len(digits) <= 15:
        return ""
    return ("+" if plus else "") + digits


def validate_time(value: str) -> bool:
    try:
        datetime.strptime(str(value), "%H:%M")
        return True
    except (TypeError, ValueError):
        return False

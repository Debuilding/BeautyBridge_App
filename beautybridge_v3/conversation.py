"""Deterministic booking capture; natural-language interpretation is pluggable."""
from __future__ import annotations
from dataclasses import asdict
from typing import Any
from .domain import BookingDetails, normalize_phone, validate_date, validate_time

FIELD_ALIASES = {
    "service_id": ("service_id", "service"),
    "requested_date": ("requested_date", "date", "date_str"),
    "preferred_time": ("preferred_time", "time", "time_str"),
    "master_id": ("master_id", "employee_id"),
    "client_name": ("client_name", "name"),
    "client_phone": ("client_phone", "phone"),
    "photo_url": ("photo_url", "nails_photo_url"),
    "notes": ("notes",),
}

class ConversationEngine:
    def merge_extracted(self, current: dict[str, Any], extracted: dict[str, Any]) -> dict[str, Any]:
        values = {key: str(current.get(key) or "") for key in asdict(BookingDetails()).keys()}
        for target, aliases in FIELD_ALIASES.items():
            for alias in aliases:
                value = extracted.get(alias)
                if value is not None and str(value).strip():
                    values[target] = str(value).strip()
                    break
        if values["client_phone"]:
            normalized = normalize_phone(values["client_phone"])
            values["client_phone"] = normalized
        if values["requested_date"] and not validate_date(values["requested_date"]):
            values["requested_date"] = ""
        if values["preferred_time"] and not validate_time(values["preferred_time"]):
            values["preferred_time"] = ""
        return values

    def missing_fields(self, details: dict[str, Any], *, photo_required: bool = False) -> list[str]:
        return BookingDetails(**{k: str(details.get(k) or "") for k in asdict(BookingDetails()).keys()}).missing_fields(photo_required)

    def next_prompt_key(self, details: dict[str, Any], *, photo_required: bool = False) -> str:
        missing = self.missing_fields(details, photo_required=photo_required)
        return missing[0] if missing else "ready_for_confirmation"

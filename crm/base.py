from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


class CRMError(RuntimeError):
    """Expected CRM/integration error that can safely fall back to manual flow."""


@dataclass(frozen=True)
class CRMCapabilities:
    availability: bool = False
    booking: bool = False
    cancellation: bool = False
    rescheduling: bool = False
    customer_lookup: bool = False
    customer_write: bool = False
    services_sync: bool = False
    masters_sync: bool = False


@dataclass
class Slot:
    employee_id: str
    date: str
    start: str
    end: str
    employee_name: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "employee_id": self.employee_id,
            "master": self.employee_name or self.employee_id,
            "date": self.date,
            "time": self.start,
            "end": self.end,
            "raw": self.raw,
        }


@dataclass
class BookingRequest:
    employee_id: str
    service_id: str
    date: str
    time: str
    name: str
    phone: str
    sender_id: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class BookingResult:
    ok: bool
    status: str
    crm_id: str = ""
    message: str = ""
    raw: Any = None


class CRMAdapter:
    """Provider-neutral contract used by BeautyBridge core."""

    type_name = "unsupported"
    capabilities = CRMCapabilities()

    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg

    def get_available_slots(self, service_id: str, date_str: str) -> List[Slot]:
        raise CRMError(f"{self.type_name} does not provide availability")

    def check_slot(self, request: BookingRequest) -> bool:
        """Final availability check immediately before booking."""
        if not self.capabilities.availability:
            return True
        for slot in self.get_available_slots(request.service_id, request.date):
            if slot.employee_id != str(request.employee_id):
                continue
            try:
                start = datetime.strptime(f"{slot.date} {slot.start}", "%Y-%m-%d %H:%M")
                end = datetime.strptime(f"{slot.date} {slot.end}", "%Y-%m-%d %H:%M")
                chosen = datetime.strptime(f"{request.date} {request.time}", "%Y-%m-%d %H:%M")
            except ValueError:
                continue
            duration_minutes = int(self.cfg.get("services", {}).get(str(request.service_id), {}).get("duration", 60))
            chosen_end = chosen.fromtimestamp(chosen.timestamp() + duration_minutes * 60)
            if start <= chosen and chosen_end <= end:
                return True
        return False

    def create_booking(self, request: BookingRequest) -> BookingResult:
        raise CRMError(f"{self.type_name} does not provide booking")

    def cancel_booking(self, crm_id: str) -> BookingResult:
        raise CRMError(f"{self.type_name} does not provide cancellation")

    def reschedule_booking(self, crm_id: str, request: BookingRequest) -> BookingResult:
        raise CRMError(f"{self.type_name} does not provide rescheduling")

    def get_services(self) -> List[Dict[str, Any]]:
        return []

    def get_masters(self) -> List[Dict[str, Any]]:
        return []

    def healthcheck(self) -> Dict[str, Any]:
        return {"ok": True, "type": self.type_name, "capabilities": self.capabilities.__dict__}

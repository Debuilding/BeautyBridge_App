from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

from .base import BookingRequest, BookingResult, CRMAdapter, CRMCapabilities, CRMError, Slot


class BookonAdapter(CRMAdapter):
    """Bookon private connector.

    Bookon is intentionally isolated from the universal core because this
    integration relies on private/internal endpoints rather than a documented
    public API. The rest of BeautyBridge only sees the generic CRMAdapter API.
    """

    type_name = "bookon"
    capabilities = CRMCapabilities(
        availability=True,
        booking=True,
        customer_lookup=True,
        customer_write=True,
    )

    def __init__(self, cfg: Dict[str, Any]):
        super().__init__(cfg)
        self.crm = cfg.get("crm", {})
        self.local_tz = ZoneInfo(cfg.get("local_tz", "Europe/Kyiv"))

    def _client(self):
        from bocrm_playwright import BOCRMManualAdapter

        return BOCRMManualAdapter(
            email=self.crm.get("email", ""),
            password=self.crm.get("password", ""),
            branch_id=self.crm.get("branch_id", ""),
            storage_state_path=self.crm.get("storage_state"),
        )

    def _parse_slot(self, specialist_id: str, day: str, block: Dict[str, Any]) -> Slot | None:
        try:
            start = datetime.fromisoformat(str(block["startTime"]).replace("Z", "+00:00"))
            end = datetime.fromisoformat(str(block["stopTime"]).replace("Z", "+00:00"))
            if start.tzinfo:
                start = start.astimezone(self.local_tz)
            if end.tzinfo:
                end = end.astimezone(self.local_tz)
            masters = self.cfg.get("masters", {})
            return Slot(
                employee_id=str(specialist_id),
                employee_name=masters.get(str(specialist_id), str(specialist_id)),
                date=day,
                start=start.strftime("%H:%M"),
                end=end.strftime("%H:%M"),
                raw={"startTime": str(block["startTime"]), "stopTime": str(block["stopTime"])},
            )
        except (KeyError, TypeError, ValueError):
            return None

    def get_available_slots(self, service_id: str, date_str: str) -> List[Slot]:
        try:
            datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError as exc:
            raise CRMError("Invalid date format; expected YYYY-MM-DD") from exc

        result = self._client().get_available_slots_sync(str(service_id), date_str)
        if not result.get("ok"):
            raise CRMError(result.get("message", "Bookon availability failed"))

        slots: List[Slot] = []
        for specialist_id, dates in (result.get("data") or {}).items():
            if not isinstance(dates, dict):
                continue
            for day, blocks in dates.items():
                if not isinstance(blocks, list):
                    continue
                for block in blocks:
                    slot = self._parse_slot(str(specialist_id), str(day), block)
                    if slot:
                        slots.append(slot)

        def sort_key(slot: Slot):
            hour = int(slot.start.split(":")[0])
            morning_priority = 0 if 10 <= hour < 12 else 1
            return morning_priority, slot.start, slot.employee_name

        slots.sort(key=sort_key)
        limit = max(1, int(self.cfg.get("booking_rules", {}).get("offer_slots_limit", 3)))
        return slots[:limit]

    def check_slot(self, request: BookingRequest) -> bool:
        """Re-query Bookon immediately before booking to reduce race conditions."""
        try:
            raw = self._client().get_available_slots_sync(str(request.service_id), request.date)
            if not raw.get("ok"):
                return False
            duration = int(self.cfg.get("services", {}).get(str(request.service_id), {}).get("duration", 60))
            chosen = datetime.strptime(f"{request.date} {request.time}", "%Y-%m-%d %H:%M")
            chosen_end = chosen + timedelta(minutes=duration)
            for specialist_id, dates in (raw.get("data") or {}).items():
                if str(specialist_id) != str(request.employee_id):
                    continue
                blocks = (dates or {}).get(request.date, []) if isinstance(dates, dict) else []
                for block in blocks:
                    slot = self._parse_slot(str(specialist_id), request.date, block)
                    if not slot:
                        continue
                    start = datetime.strptime(f"{slot.date} {slot.start}", "%Y-%m-%d %H:%M")
                    end = datetime.strptime(f"{slot.date} {slot.end}", "%Y-%m-%d %H:%M")
                    if start <= chosen and chosen_end <= end:
                        return True
            return False
        except Exception:
            logging.exception("Bookon final slot check failed")
            return False

    def create_booking(self, request: BookingRequest) -> BookingResult:
        if not self.check_slot(request):
            return BookingResult(
                ok=False,
                status="slot_unavailable",
                message="The selected time is no longer available.",
            )

        result = self._client().create_visit_sync(
            specialist_id=request.employee_id,
            service_id=request.service_id,
            date_str=request.date,
            time_str=request.time,
            client_name=request.name,
            client_phone=request.phone,
        )
        if not result.get("ok"):
            raise CRMError(result.get("message", "Bookon booking failed"))
        return BookingResult(
            ok=True,
            status="booked",
            crm_id=str(result.get("crm_id") or ""),
            raw=result,
        )

    def healthcheck(self) -> Dict[str, Any]:
        configured = bool(self.crm.get("branch_id") and (self.crm.get("email") or self.crm.get("storage_state")))
        return {
            "ok": configured,
            "type": self.type_name,
            "configured": configured,
            "private_api": True,
            "capabilities": self.capabilities.__dict__,
        }

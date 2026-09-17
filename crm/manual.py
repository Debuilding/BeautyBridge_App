from __future__ import annotations

from typing import Any, Dict, List

from .base import BookingRequest, BookingResult, CRMAdapter, CRMCapabilities, CRMError, Slot


class ManualAdapter(CRMAdapter):
    """Adapter for salons/masters without a machine-readable CRM.

    Manual mode deliberately never fabricates availability. The core stores
    the request locally and notifies the salon admin for confirmation.
    """

    type_name = "manual"
    capabilities = CRMCapabilities(booking=False, availability=False)

    def get_available_slots(self, service_id: str, date_str: str) -> List[Slot]:
        return []

    def create_booking(self, request: BookingRequest) -> BookingResult:
        return BookingResult(
            ok=True,
            status="manual_confirmation_required",
            message="The appointment must be confirmed by the salon administrator.",
        )


class UnsupportedAdapter(ManualAdapter):
    """Safe fallback when a CRM type is unknown or not implemented yet."""

    type_name = "unsupported"

    def __init__(self, cfg: Dict[str, Any], requested_type: str = ""):
        super().__init__(cfg)
        self.requested_type = requested_type or "unknown"

    def create_booking(self, request: BookingRequest) -> BookingResult:
        return BookingResult(
            ok=True,
            status="manual_confirmation_required",
            message=(
                f"CRM type '{self.requested_type}' is not implemented yet. "
                "The request will be handled manually instead of being sent to the wrong CRM."
            ),
        )

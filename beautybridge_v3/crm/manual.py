from __future__ import annotations
from .base import BookingResult

class ManualAdapter:
    """Safe manual mode: records a request but never claims availability."""
    type_name = "manual"

    def create_booking(self, request: dict) -> BookingResult:
        return BookingResult(ok=True, status="pending_manual_confirmation", message="Request stored for administrator confirmation")

class UnsupportedAdapter:
    def __init__(self, requested_type: str):
        self.type_name = "unsupported"
        self.requested_type = requested_type

    def create_booking(self, request: dict) -> BookingResult:
        return BookingResult(ok=False, status="unsupported_crm", message=f"CRM '{self.requested_type}' has no implemented adapter")

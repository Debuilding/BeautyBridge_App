from __future__ import annotations
from dataclasses import dataclass
from typing import Protocol

@dataclass(frozen=True)
class BookingResult:
    ok: bool
    status: str
    message: str = ""
    external_id: str = ""

class CRMAdapter(Protocol):
    type_name: str
    def create_booking(self, request: dict) -> BookingResult: ...

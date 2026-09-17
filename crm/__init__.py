"""BeautyBridge CRM adapter layer.

The core application talks to CRM systems only through these adapters.
Each adapter translates its provider-specific API into the same internal
booking model, so adding a new CRM does not require rewriting the bot.
"""

from .base import BookingRequest, BookingResult, CRMAdapter, CRMCapabilities, CRMError, Slot
from .registry import available_crm_types, get_crm_adapter

__all__ = [
    "BookingRequest",
    "BookingResult",
    "CRMAdapter",
    "CRMCapabilities",
    "CRMError",
    "Slot",
    "available_crm_types",
    "get_crm_adapter",
]

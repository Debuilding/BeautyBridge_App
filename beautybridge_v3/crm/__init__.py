"""CRM adapter contracts; adapters must be explicitly registered."""
from .base import BookingResult, CRMAdapter
from .manual import ManualAdapter, UnsupportedAdapter
from .registry import get_adapter

__all__ = ["BookingResult", "CRMAdapter", "ManualAdapter", "UnsupportedAdapter", "get_adapter"]

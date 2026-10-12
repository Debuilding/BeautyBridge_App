from __future__ import annotations
from .manual import ManualAdapter, UnsupportedAdapter

# Only adapters proven to exist belong in this registry. Bookon is deliberately
# manual-only and must never be queried for availability by V3.
_ADAPTERS = {"manual": ManualAdapter, "table": ManualAdapter, "home_master": ManualAdapter, "none": ManualAdapter, "bookon": ManualAdapter, "book_on": ManualAdapter, "binotel_bookon": ManualAdapter}

def get_adapter(crm_type: str):
    key = str(crm_type or "manual").strip().lower()
    adapter = _ADAPTERS.get(key)
    return adapter() if adapter else UnsupportedAdapter(key)

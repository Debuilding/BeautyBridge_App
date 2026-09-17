from __future__ import annotations

from typing import Any, Dict, List

from .base import CRMAdapter
from .bookon import BookonAdapter
from .manual import ManualAdapter, UnsupportedAdapter


ADAPTERS = {
    "manual": ManualAdapter,
    "home_master": ManualAdapter,
    "none": ManualAdapter,
    "bookon": BookonAdapter,
}


# These names are intentionally visible to onboarding even before the
# provider adapter exists. An unimplemented provider is routed to a safe
# manual fallback rather than accidentally being treated as Bookon.
KNOWN_CRM_TYPES = [
    "manual",
    "bookon",
    "altegio",
    "yclients",
    "easyweek",
    "google_calendar",
]


def available_crm_types() -> List[Dict[str, Any]]:
    result = []
    for name in KNOWN_CRM_TYPES:
        implemented = name in ADAPTERS
        result.append({
            "type": name,
            "implemented": implemented,
            "mode": "automatic" if implemented and name != "manual" else "manual_fallback",
        })
    return result


def get_crm_adapter(cfg: Dict[str, Any]) -> CRMAdapter:
    crm_cfg = cfg.get("crm", {}) or {}
    requested = str(cfg.get("crm_type") or crm_cfg.get("type") or "manual").strip().lower()
    adapter_cls = ADAPTERS.get(requested)
    if adapter_cls is None:
        return UnsupportedAdapter(cfg, requested_type=requested)
    return adapter_cls(cfg)

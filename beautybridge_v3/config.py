"""Safe environment configuration and tenant-scoped business settings."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class TenantConfig:
    tenant_id: str
    name: str
    language: str = "uk"
    timezone: str = "Europe/Kyiv"
    crm_type: str = "manual"
    services: tuple[dict[str, Any], ...] = ()
    masters: tuple[dict[str, Any], ...] = ()
    business_facts: str = ""
    admin_chat_id: str = ""
    enabled: bool = True

    @classmethod
    def from_dict(cls, tenant_id: str, raw: dict[str, Any]) -> "TenantConfig":
        return cls(
            tenant_id=tenant_id,
            name=str(raw.get("name") or tenant_id),
            language=str(raw.get("language") or "uk").lower(),
            timezone=str(raw.get("timezone") or "Europe/Kyiv"),
            crm_type=str(raw.get("crm_type") or "manual").lower(),
            services=tuple(x for x in raw.get("services", []) if isinstance(x, dict)),
            masters=tuple(x for x in raw.get("masters", []) if isinstance(x, dict)),
            business_facts=str(raw.get("business_facts") or ""),
            admin_chat_id=str(raw.get("admin_chat_id") or ""),
            enabled=bool(raw.get("enabled", True)),
        )


@dataclass(frozen=True)
class Settings:
    database_path: str = "/tmp/beautybridge-v3.sqlite3"
    meta_verify_token: str = ""
    meta_app_secret: str = ""
    telegram_bot_token: str = ""
    openai_api_key: str = ""
    require_meta_signature: bool = True
    tenants: dict[str, TenantConfig] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "Settings":
        raw_tenants = json.loads(os.getenv("BBV3_TENANTS_JSON", "{}"))
        if not isinstance(raw_tenants, dict):
            raise ValueError("BBV3_TENANTS_JSON must be a JSON object keyed by tenant id")
        tenants = {
            str(key): TenantConfig.from_dict(str(key), value)
            for key, value in raw_tenants.items()
            if isinstance(value, dict)
        }
        require_signature = os.getenv("BBV3_REQUIRE_META_SIGNATURE", "true").strip().lower() not in {"0", "false", "no", "off"}
        return cls(
            database_path=os.getenv("BBV3_DATABASE_PATH", "/tmp/beautybridge-v3.sqlite3"),
            meta_verify_token=os.getenv("BBV3_META_VERIFY_TOKEN", ""),
            meta_app_secret=os.getenv("BBV3_META_APP_SECRET", ""),
            telegram_bot_token=os.getenv("BBV3_TELEGRAM_BOT_TOKEN", ""),
            openai_api_key=os.getenv("BBV3_OPENAI_API_KEY", ""),
            require_meta_signature=require_signature,
            tenants=tenants,
        )

    def tenant(self, tenant_id: str) -> TenantConfig:
        tenant = self.tenants.get(str(tenant_id))
        if tenant is None or not tenant.enabled:
            raise KeyError("Unknown or disabled tenant")
        return tenant

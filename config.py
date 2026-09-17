import json
import os
from copy import deepcopy
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
# Real client configuration is local/secret data. Never default to a checked-in
# file containing live salon credentials or addresses.
CLIENTS_FILE = Path(os.getenv("CLIENTS_FILE", BASE_DIR / "clients.local.json"))


def env_bool(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on", "y"}


def env_prefix(client_id):
    return "".join(ch if ch.isalnum() else "_" for ch in str(client_id)).upper()


def load_client_blueprints():
    if not CLIENTS_FILE.exists():
        # An empty registry is valid for a fresh deployment. The onboarding
        # endpoint can validate a new tenant without requiring a dummy tenant.
        return {}
    try:
        data = json.loads(CLIENTS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot load client config: {CLIENTS_FILE}: {exc}") from exc


TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "")
LOCAL_TZ = os.getenv("LOCAL_TZ", "Europe/Kyiv")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")
# Production should always provide explicit secrets. The old permissive defaults
# made accidental public deployments easier to spoof.
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "")
ADMIN_API_TOKEN = os.getenv("ADMIN_API_TOKEN", "")
DB_PATH = os.getenv("DB_PATH", "data/beautybridge.db")
META_APP_SECRET = os.getenv("META_APP_SECRET", "")
RUN_SCHEDULER = env_bool("RUN_SCHEDULER", True)
SCHEDULER_INTERVAL_SECONDS = max(300, int(os.getenv("SCHEDULER_INTERVAL_SECONDS", "3600")))
DEBOUNCE_SECONDS = max(0.2, float(os.getenv("DEBOUNCE_SECONDS", "1.0")))
STATE_TTL_HOURS = max(1, int(os.getenv("STATE_TTL_HOURS", "48")))


def build_brands():
    brands = {}
    for client_id, item in load_client_blueprints().items():
        item = deepcopy(item or {})
        prefix = env_prefix(client_id)
        crm_type = str(os.getenv(f"{prefix}_CRM_TYPE", item.get("crm_type", "manual"))).strip().lower()
        language = str(os.getenv(f"{prefix}_LANGUAGE", item.get("language", "uk"))).strip().lower() or "uk"

        brands[client_id] = {
            "id": client_id,
            "enabled": env_bool(f"{prefix}_ENABLED", bool(item.get("enabled", True))),
            "name": item.get("name", client_id),
            "city": item.get("city", ""),
            "language": language,
            "local_tz": os.getenv(f"{prefix}_LOCAL_TZ", item.get("local_tz", LOCAL_TZ)),
            "page_id": os.getenv(f"{prefix}_PAGE_ID", ""),
            "page_access_token": os.getenv(f"{prefix}_PAGE_ACCESS_TOKEN", ""),
            "telegram_chat_id": os.getenv(f"{prefix}_ADMIN_CHAT_ID", ADMIN_CHAT_ID),
            "address": os.getenv(f"{prefix}_ADDRESS", item.get("address", "")),
            "phone": os.getenv(f"{prefix}_PHONE", item.get("phone", "")),
            "wifi": os.getenv(f"{prefix}_WIFI", item.get("wifi", "")),
            "wifi_password": os.getenv(f"{prefix}_WIFI_PASSWORD", item.get("wifi_password", "")),
            "prepayment_required": env_bool(
                f"{prefix}_PREPAYMENT_REQUIRED", bool(item.get("prepayment_required", False))
            ),
            "prepayment_amount": int(
                os.getenv(f"{prefix}_PREPAYMENT_AMOUNT", str(item.get("prepayment_amount", 0)))
            ),
            "auto_confirm_payment": env_bool(
                f"{prefix}_AUTO_CONFIRM_PAYMENT", bool(item.get("auto_confirm_payment", False))
            ),
            "card_number": os.getenv(f"{prefix}_CARD_NUMBER", ""),
            "card_name": os.getenv(f"{prefix}_CARD_NAME", ""),
            "block_address_if_not_paid": env_bool(
                f"{prefix}_BLOCK_ADDRESS_IF_NOT_PAID", bool(item.get("block_address_if_not_paid", True))
            ),
            "priority_hours": item.get("priority_hours", []),
            "follow_up_days": int(item.get("follow_up_days", 21)),
            "services": item.get("services", {}),
            "masters": item.get("masters", {}),
            "master_services": item.get("master_services", {}),
            "price_text": item.get("price_text", "Послуги та ціни ще не налаштовані."),
            "booking_rules": item.get("booking_rules", {}),
            "crm_type": crm_type,
            "crm": {
                "type": crm_type,
                "widget_id": os.getenv(f"{prefix}_WIDGET_ID", ""),
                "branch_id": os.getenv(f"{prefix}_BRANCH_ID", ""),
                "session_token": os.getenv(f"{prefix}_BOOKON_SESSION", ""),
                "email": os.getenv(f"{prefix}_BOCRM_EMAIL", ""),
                "password": os.getenv(f"{prefix}_BOCRM_PASSWORD", ""),
                "storage_state": os.getenv(
                    f"{prefix}_BOOKON_STORAGE_STATE", f"data/bookon/{client_id}_storage.json"
                ),
                # Generic provider fields are optional and can be consumed by
                # future adapters without changing the tenant schema again.
                "api_base_url": os.getenv(f"{prefix}_CRM_API_BASE_URL", item.get("crm", {}).get("api_base_url", "")),
                "api_key": os.getenv(f"{prefix}_CRM_API_KEY", ""),
                "api_secret": os.getenv(f"{prefix}_CRM_API_SECRET", ""),
                "account_id": os.getenv(f"{prefix}_CRM_ACCOUNT_ID", item.get("crm", {}).get("account_id", "")),
            },
        }
    return brands


BRANDS = build_brands()
BRAND_MASTERS = {brand: cfg.get("masters", {}) for brand, cfg in BRANDS.items()}
BRAND_SERVICES = {brand: cfg.get("services", {}) for brand, cfg in BRANDS.items()}
BRAND_MASTER_SERVICES = {brand: cfg.get("master_services", {}) for brand, cfg in BRANDS.items()}
BRAND_PRICE_TEXT = {brand: cfg.get("price_text", "") for brand, cfg in BRANDS.items()}

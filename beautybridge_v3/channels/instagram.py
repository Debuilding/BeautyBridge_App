"""Meta webhook verification, signature validation and event normalization."""
from __future__ import annotations
import hashlib
import hmac
from typing import Any


def verify_signature(raw_body: bytes, signature_header: str, app_secret: str) -> bool:
    if not app_secret or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def normalize_events(payload: dict[str, Any]) -> list[dict[str, str]]:
    """Extract text messages from Meta messaging payloads; ignore echoes/non-text.

    Tenant/page resolution is intentionally performed by the application using
    the receiving page ID, not by trusting arbitrary tenant IDs in payloads.
    """
    events: list[dict[str, str]] = []
    for entry in payload.get("entry", []) or []:
        page_id = str(entry.get("id") or "")
        for item in entry.get("messaging", []) or []:
            message = item.get("message") or {}
            sender_id = str((item.get("sender") or {}).get("id") or "")
            message_id = str(message.get("mid") or item.get("timestamp") or "")
            text = str(message.get("text") or "").strip()
            if not page_id or not sender_id or not message_id or not text or message.get("is_echo"):
                continue
            events.append({"page_id": page_id, "sender_id": sender_id, "event_id": message_id, "text": text})
    return events

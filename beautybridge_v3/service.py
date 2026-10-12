"""Application service coordinating tenant config, state, requests and notifications."""
from __future__ import annotations
import json
from typing import Any
from .config import Settings
from .conversation import ConversationEngine
from .crm import get_adapter
from .domain import BookingStatus
from .notifications.telegram import TelegramNotifier
from .storage import Storage

class BeautyBridgeService:
    def __init__(self, settings: Settings, storage: Storage, notifier: TelegramNotifier | None = None):
        self.settings = settings
        self.storage = storage
        self.notifier = notifier or TelegramNotifier(settings.telegram_bot_token)
        self.conversation = ConversationEngine()

    def receive_message(self, tenant_id: str, sender_id: str, text: str, event_id: str, extracted: dict[str, Any] | None = None) -> dict[str, Any]:
        """Durably enqueue an inbound message; the worker owns AI and booking side effects."""
        self.settings.tenant(tenant_id)
        payload = {"text": str(text)[:8000], "extracted": extracted or {}}
        inserted = self.storage.enqueue_once(event_id, tenant_id, sender_id, payload)
        return {"status": "queued" if inserted else "duplicate_ignored"}

    def admin_set_status(self, request_id: int, status: str, note: str = "") -> bool:
        request = self.storage.get_booking_request(request_id)
        if request is None:
            return False
        changed = self.storage.set_booking_status(request_id, status, note)
        if changed:
            tenant = self.settings.tenant(request["tenant_id"])
            if status == BookingStatus.CONFIRMED.value:
                text = "Ваш запис підтверджено салоном. Дякуємо!"
            elif status == BookingStatus.ALTERNATIVE_PROPOSED.value:
                text = note or "Салон пропонує узгодити інший час. Напишіть, будь ласка, який час вам підійде."
            else:
                text = note or "На жаль, салон не зміг підтвердити цей час. Напишіть, будь ласка, щоб узгодити інший варіант."
            # Sending to the customer is deliberately delegated to the channel
            # adapter; this method does not invent a recipient mapping.
            state = self.storage.get_conversation(request["tenant_id"], request["sender_id"])
            state.update({"step": "complete" if status == "confirmed" else "collecting", "last_booking_status": status, "last_admin_message": text})
            self.storage.save_conversation(request["tenant_id"], request["sender_id"], state)
        return changed

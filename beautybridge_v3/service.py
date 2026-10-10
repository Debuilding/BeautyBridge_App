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
        tenant = self.settings.tenant(tenant_id)
        payload = {"text": str(text)[:8000], "extracted": extracted or {}}
        inserted = self.storage.enqueue_once(event_id, tenant_id, sender_id, payload)
        if not inserted:
            return {"status": "duplicate_ignored"}
        state = self.storage.get_conversation(tenant_id, sender_id)
        details = self.conversation.merge_extracted(state.get("details", {}), payload["extracted"])
        state.update({"details": details, "last_event_id": event_id})
        missing = self.conversation.missing_fields(details)
        if missing:
            self.storage.save_conversation(tenant_id, sender_id, state)
            self.storage.finish_event(event_id)
            return {"status": "collecting", "next_field": missing[0]}

        adapter = get_adapter(tenant.crm_type)
        result = adapter.create_booking(details)
        if result.status != BookingStatus.PENDING_MANUAL_CONFIRMATION.value:
            self.storage.finish_event(event_id, error=result.message or result.status)
            return {"status": result.status}
        request_id = self.storage.create_booking_request(tenant_id, sender_id, details, BookingStatus.PENDING_MANUAL_CONFIRMATION.value)
        state["step"] = "waiting_admin_confirmation"
        state["booking_request_id"] = request_id
        self.storage.save_conversation(tenant_id, sender_id, state)
        message = (
            f"Нова заявка #{request_id} — {tenant.name}\\n"
            f"Послуга: {details.get('service_id','')}\\n"
            f"Дата: {details.get('requested_date','')}\\n"
            f"Бажаний час: {details.get('preferred_time','')}\\n"
            f"Майстер: {details.get('master_id') or 'не вказано'}\\n"
            f"Ім’я: {details.get('client_name','')}\\n"
            f"Телефон: {details.get('client_phone','')}\\n"
            "Статус: очікує ручного підтвердження; доступність не перевірена."
        )
        self.notifier.send(tenant.admin_chat_id, message)
        self.storage.finish_event(event_id)
        return {"status": BookingStatus.PENDING_MANUAL_CONFIRMATION.value, "request_id": request_id}

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
            self.storage.save_conversation(request["tenant_id"], request["sender_id"], {"step": "complete" if status == "confirmed" else "collecting", "last_booking_status": status, "last_admin_message": text})
        return changed

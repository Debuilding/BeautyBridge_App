"""Single-process queue worker for the isolated V3 staging branch."""
from __future__ import annotations
import json
import logging
import os
import time
from .ai import SalonAI
from .channels.instagram_sender import InstagramSender
from .config import Settings
from .service import BeautyBridgeService
from .storage import Storage

LOGGER = logging.getLogger(__name__)


def _json_env(name: str) -> dict:
    value = json.loads(os.getenv(name, "{}"))
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a JSON object")
    return value


def run_forever(poll_seconds: float = 0.75) -> None:
    settings = Settings.from_env()
    storage = Storage(settings.database_path)
    service = BeautyBridgeService(settings, storage)
    ai = SalonAI(settings.openai_api_key, os.getenv("BBV3_OPENAI_MODEL", "gpt-4o-mini"))
    page_tokens = {str(k): str(v) for k, v in _json_env("BBV3_INSTAGRAM_TOKENS_JSON").items()}
    page_to_tenant = {str(getattr(tenant, "page_id", "")): tenant_id for tenant_id, tenant in settings.tenants.items() if tenant.enabled and getattr(tenant, "page_id", "")}
    while True:
        event = storage.next_event()
        if event is None:
            time.sleep(max(0.1, poll_seconds))
            continue
        try:
            tenant_id = event["tenant_id"]
            tenant = settings.tenant(tenant_id)
            payload = event["payload"]
            state = storage.get_conversation(tenant_id, event["sender_id"])
            if state.get("step") == "waiting_admin_confirmation" and state.get("booking_request_id"):
                reply = "Ваш запит уже очікує підтвердження салону. Будь ласка, дочекайтеся відповіді — час поки не підтверджений."
                page_id = str(payload.get("page_id") or "")
                if page_id:
                    InstagramSender(page_tokens.get(tenant_id, "")).send_text(event["sender_id"], reply)
                storage.finish_event(event["event_id"])
                continue
            known = state.get("details", {})
            from .conversation import ConversationEngine
            engine = ConversationEngine()
            next_field = engine.next_prompt_key(known)
            result = ai.respond(client_text=payload.get("text", ""), tenant=tenant, known_details=known, next_field=next_field)
            details = engine.merge_extracted(known, result.get("extracted", {}))
            missing = engine.missing_fields(details)
            state.update({"details": details, "last_event_id": event["event_id"]})
            text_norm = str(payload.get("text", "")).strip().casefold()
            confirmed = text_norm in {"так", "так, підтверджую", "підтверджую", "підтверджую запис", "yes", "yes, confirm", "confirm", "да", "подтверждаю"}
            if missing:
                state["step"] = "collecting"
                storage.save_conversation(tenant_id, event["sender_id"], state)
                reply = result.get("reply") or ai._fallback(missing[0])
            elif state.get("step") != "awaiting_client_confirmation" or not confirmed:
                state["step"] = "awaiting_client_confirmation"
                storage.save_conversation(tenant_id, event["sender_id"], state)
                reply = (
                    "Перевірте, будь ласка, ваш запит:\\n"
                    f"Послуга: {details.get('service_id','')}\\n"
                    f"Дата: {details.get('requested_date','')}\\n"
                    f"Бажаний час: {details.get('preferred_time','')}\\n"
                    f"Майстер: {details.get('master_id') or 'не вказано'}\\n"
                    f"Ім’я: {details.get('client_name','')}\\n"
                    f"Телефон: {details.get('client_phone','')}\\n"
                    "Надіслати цей запит салону? Відповідайте «Так» або напишіть, що потрібно змінити."
                )
            else:
                request_id = storage.create_booking_request(tenant_id, event["sender_id"], details, "pending_manual_confirmation", source_event_id=event["event_id"])
                state.update({"step": "waiting_admin_confirmation", "booking_request_id": request_id})
                storage.save_conversation(tenant_id, event["sender_id"], state)
                admin_text = (f"Нова заявка #{request_id} — {tenant.name}\\nПослуга: {details.get('service_id','')}\\nДата: {details.get('requested_date','')}\\nБажаний час: {details.get('preferred_time','')}\\nМайстер: {details.get('master_id') or 'не вказано'}\\nІм’я: {details.get('client_name','')}\\nТелефон: {details.get('client_phone','')}\\nСтатус: очікує ручного підтвердження; доступність не перевірена.")
                notified = service.notifier.send(tenant.admin_chat_id, admin_text)
                if notified:
                    reply = "Дякую! Ваш запит надіслано салону на підтвердження. Час ще не підтверджений — салон узгодить його з вами окремо."
                else:
                    reply = "Дякую! Ваш запит збережено, але підтвердження від салону ще немає. Не вважайте час заброньованим, доки салон не підтвердить його."
            page_id = str(payload.get("page_id") or "")
            if page_id:
                token = page_tokens.get(tenant_id, "")
                InstagramSender(token).send_text(event["sender_id"], reply)
            storage.finish_event(event["event_id"])
        except Exception as exc:
            LOGGER.exception("V3 event processing failed for event id %s", event.get("event_id", "unknown"))
            storage.finish_event(event["event_id"], error=str(exc))

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_forever()

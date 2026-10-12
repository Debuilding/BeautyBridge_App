"""AI-assisted language understanding; Python remains the business-rule authority."""
from __future__ import annotations
import json
from typing import Any

SYSTEM_RULES = """You are the short, friendly virtual salon administrator. Reply in the client's language; Ukrainian is the default. Never claim that a time is available, that a booking is confirmed, or that a CRM was checked unless the server explicitly supplies that fact. For manual booking, explain that this is a request awaiting salon confirmation. Do not invent services, prices, master names, opening hours, policies, or availability. Extract only details explicitly stated by the client. Dates must be ISO YYYY-MM-DD only when unambiguous; times must be HH:MM only when unambiguous. Return JSON with keys reply (string) and extracted (object containing only service_id, requested_date, preferred_time, master_id, client_name, client_phone, photo_url, notes)."""

class SalonAI:
    def __init__(self, api_key: str, model: str = "gpt-4o-mini"):
        self.api_key = api_key
        self.model = model

    def respond(self, *, client_text: str, tenant: Any, known_details: dict[str, Any], next_field: str = "") -> dict[str, Any]:
        if not self.api_key:
            return {"reply": self._fallback(next_field), "extracted": {}}
        from openai import OpenAI
        client = OpenAI(api_key=self.api_key)
        context = {
            "salon_name": tenant.name,
            "language_default": tenant.language,
            "timezone": tenant.timezone,
            "services": list(tenant.services),
            "masters": list(tenant.masters),
            "business_facts": tenant.business_facts,
            "already_collected": known_details,
            "next_required_field": next_field,
            "booking_mode": "manual_confirmation",
        }
        response = client.chat.completions.create(
            model=self.model,
            temperature=0.2,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM_RULES},
                {"role": "user", "content": "Context JSON:\\n" + json.dumps(context, ensure_ascii=False) + "\\nClient message:\\n" + client_text[:4000]},
            ],
            timeout=20,
        )
        raw = response.choices[0].message.content or "{}"
        try:
            result = json.loads(raw)
        except json.JSONDecodeError:
            return {"reply": self._fallback(next_field), "extracted": {}}
        extracted = result.get("extracted") if isinstance(result.get("extracted"), dict) else {}
        allowed = {"service_id", "requested_date", "preferred_time", "master_id", "client_name", "client_phone", "photo_url", "notes"}
        return {"reply": str(result.get("reply") or self._fallback(next_field))[:1800], "extracted": {k: v for k, v in extracted.items() if k in allowed and isinstance(v, (str, int, float))}}

    @staticmethod
    def _fallback(field: str) -> str:
        prompts = {
            "service_id": "Підкажіть, будь ласка, яку послугу бажаєте записати?",
            "requested_date": "На яку дату вам зручно?",
            "preferred_time": "Який час вам бажаний?",
            "master_id": "До якого майстра бажаєте записатися? Можна також залишити вибір салону.",
            "client_name": "Як до вас звертатися?",
            "client_phone": "Залиште, будь ласка, номер телефону для узгодження запису.",
            "photo_url": "Надішліть, будь ласка, фото, якщо воно потрібне для вибору послуги.",
        }
        return prompts.get(field, "Допоможу оформити запит на запис. Підкажіть, будь ласка, послугу, бажану дату та час.")

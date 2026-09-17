"""Small post-bootstrap fixes and provider wiring for BeautyBridge V2.1."""

import universal_runtime as runtime
from crm import BookingRequest, get_crm_adapter
from crm.manual import ManualAdapter
from states import BotState


class RegistryAdapterShim:
    """Expose the package-level CRM contract through the legacy runtime API.

    universal_runtime is intentionally kept intact for compatibility, but all
    real provider selection now goes through crm.registry. That means an
    unknown CRM can never silently become Bookon, and future adapters can be
    added without rewriting booking logic.
    """

    def __init__(self, inner):
        self.inner = inner
        self.name = getattr(inner, "type_name", getattr(inner, "name", "unknown"))
        caps = getattr(inner, "capabilities", None)
        if hasattr(caps, "__dict__"):
            self.capabilities = {
                key for key, enabled in caps.__dict__.items() if bool(enabled)
            }
        else:
            self.capabilities = set(caps or set())

    def get_available_slots(self, service_id, date_str):
        slots = self.inner.get_available_slots(str(service_id), str(date_str))
        result = []
        for slot in slots:
            if hasattr(slot, "as_dict"):
                result.append(slot.as_dict())
            elif isinstance(slot, dict):
                result.append(slot)
            else:
                result.append(
                    {
                        "employee_id": str(getattr(slot, "employee_id", "")),
                        "master": getattr(slot, "employee_name", "") or getattr(slot, "employee_id", ""),
                        "date": getattr(slot, "date", ""),
                        "time": getattr(slot, "start", ""),
                        "end": getattr(slot, "end", ""),
                    }
                )
        return result

    def is_slot_available(self, service_id, employee_id, date_str, time_str):
        request = BookingRequest(
            employee_id=str(employee_id),
            service_id=str(service_id),
            date=str(date_str),
            time=str(time_str),
            name="",
            phone="",
        )
        return bool(self.inner.check_slot(request))

    def create_booking(self, employee_id, service_id, date_str, time_str, name, phone):
        request = BookingRequest(
            employee_id=str(employee_id),
            service_id=str(service_id),
            date=str(date_str),
            time=str(time_str),
            name=str(name),
            phone=str(phone),
        )
        result = self.inner.create_booking(request)
        if not result.ok:
            raise RuntimeError(result.message or result.status or "CRM booking failed")
        return str(result.crm_id or "")


def adapter_for_v21(cfg):
    requested = str(
        cfg.get("crm_type") or cfg.get("crm", {}).get("type") or "manual"
    ).strip().lower()

    # Keep the legacy runtime's explicit manual branch so its existing state,
    # Telegram and local appointment flow continues to work unchanged.
    if requested in {"manual", "home_master", "none"}:
        return runtime.ManualCRMAdapter(cfg)

    provider = get_crm_adapter(cfg)
    provider_name = getattr(provider, "type_name", "unsupported")
    if provider_name == "unsupported":
        return runtime.UnsupportedCRMAdapter(cfg, requested)
    return RegistryAdapterShim(provider)


runtime.adapter_for = adapter_for_v21


def confirm_manual_booking(appointment_id: int):
    row = runtime.appointment_row(appointment_id)
    if not row:
        return runtime.jsonify({"error": "appointment not found"}), 404

    _, brand, sender, _, _, service_name, appointment_date, appointment_time, master_name, _, _, _, _ = row
    cfg = runtime.legacy.cfg_for(brand)

    if cfg.get("prepayment_required"):
        runtime.update_appointment(appointment_id, status="booked_awaiting_payment")
        runtime.strict_state_set(
            brand,
            sender,
            state=BotState.WAITING_PAYMENT.value,
            appointment_id=appointment_id,
        )
        try:
            runtime.legacy.instagram_send(
                cfg,
                sender,
                "\n".join(
                    [
                        "✅ Запис підтверджено адміністратором.",
                        f"{service_name}",
                        f"{appointment_date} о {appointment_time}",
                        f"Майстер: {master_name}",
                        "",
                        runtime.payment_instruction(cfg),
                    ]
                ),
            )
        except Exception:
            runtime.LOGGER.exception("Failed to send manual booking confirmation")
    else:
        runtime.update_appointment(appointment_id, status="confirmed")
        runtime.strict_state_set(
            brand,
            sender,
            state=BotState.BOOKED_CONFIRMED.value,
            appointment_id=appointment_id,
        )
        try:
            runtime.legacy.instagram_send(
                cfg,
                sender,
                f"✅ Запис підтверджено!\n{service_name}\n{appointment_date} о {appointment_time}\nМайстер: {master_name}",
            )
        except Exception:
            runtime.LOGGER.exception("Failed to send manual confirmation")

    return runtime.jsonify({"ok": True, "appointment_id": appointment_id})


runtime.confirm_manual_booking = confirm_manual_booking
runtime.app.view_functions["confirm_booking_v21"] = runtime.admin_required(confirm_manual_booking)

app = runtime.app

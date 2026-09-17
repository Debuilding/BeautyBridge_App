"""Small post-bootstrap fixes kept separate so universal_runtime stays readable."""

import universal_runtime as runtime
from states import BotState


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

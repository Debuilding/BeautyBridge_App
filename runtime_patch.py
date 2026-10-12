"""Compatibility bootstrap for the manual Instagram -> Telegram booking flow."""

import universal_runtime as runtime

# Keep one canonical confirmation endpoint; booking availability and creation
# are handled only by the local request + Telegram admin workflow.
runtime.app.view_functions["confirm_booking_v21"] = runtime.admin_required(
    runtime.confirm_manual_booking
)

app = runtime.app

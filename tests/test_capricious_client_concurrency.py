"""
Simulates a "capricious client": someone who, in effect, tries to book a
haircut, cancel an old visit, and ask about availability all at once.

The bot currently exposes only 3 tools to the AI (remember_booking,
get_available_slots, create_visit) - there is no cancel/discount tool wired
up yet, even though crm.base.CRMAdapter defines a cancel_booking() contract.
This test exercises that contract directly against a deliberately slow CRM
adapter to check two things end-to-end tests can't safely check in CI:

1. Concurrent CRM calls for the same "conversation" don't hang each other
   (no deadlock/lock contention across create_booking/get_available_slots).
2. Calling the not-yet-implemented cancel_booking() fails predictably and
   fast (a clean CRMError), instead of hanging or raising something the
   rest of the app doesn't know how to handle.

This does not hit any real CRM, Telegram, or OpenAI API - it's a pure
concurrency/contract test against the manual adapter.
"""

import threading
import time

from crm.base import BookingRequest, CRMError
from crm.registry import get_crm_adapter

FAKE_CFG = {
    "crm_type": "manual",
    "crm": {"type": "manual"},
    "services": {"543063": {"name": "Стрижка", "duration": 60}},
}


class SlowAdapter:
    """Wraps a real adapter with artificial latency, simulating a CRM under load."""

    def __init__(self, inner, delay=0.3):
        self.inner = inner
        self.delay = delay

    def create_booking(self, request):
        time.sleep(self.delay)
        return self.inner.create_booking(request)

    def cancel_booking(self, crm_id):
        time.sleep(self.delay)
        return self.inner.cancel_booking(crm_id)

    def get_available_slots(self, service_id, date_str):
        time.sleep(self.delay)
        return self.inner.get_available_slots(service_id, date_str)


def test_capricious_client_concurrent_requests_do_not_hang():
    adapter = SlowAdapter(get_crm_adapter(FAKE_CFG), delay=0.3)
    results, errors = {}, {}

    def do_booking():
        try:
            req = BookingRequest(
                employee_id="36645",
                service_id="543063",
                date="2026-09-19",
                time="12:00",
                name="Капризна Клієнтка",
                phone="0501234567",
            )
            results["booking"] = adapter.create_booking(req)
        except Exception as e:  # noqa: BLE001 - capturing for assertion below
            errors["booking"] = e

    def do_cancel():
        try:
            results["cancel"] = adapter.cancel_booking("old-visit-123")
        except Exception as e:  # noqa: BLE001
            errors["cancel"] = e

    def do_slots():
        try:
            results["slots"] = adapter.get_available_slots("543063", "2026-09-19")
        except Exception as e:  # noqa: BLE001
            errors["slots"] = e

    threads = [threading.Thread(target=fn) for fn in (do_booking, do_cancel, do_slots)]
    start = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    elapsed = time.time() - start

    assert elapsed < 5, f"Concurrent CRM calls took too long: {elapsed:.2f}s (possible hang)"
    assert not any(t.is_alive() for t in threads), "A thread is still running - hung request"

    # Cancellation isn't implemented by any adapter yet - it must fail loud
    # and fast (CRMError), not hang or raise something unexpected.
    assert "cancel" in errors, "cancel_booking should have raised, not succeeded silently"
    assert isinstance(errors["cancel"], CRMError)

    # Booking and slot lookup must still complete successfully and
    # independently of the failed cancellation attempt.
    assert "booking" in results and results["booking"].ok
    assert "slots" in results

"""Dedicated BeautyBridge background worker process.

Run this separately from Gunicorn/Flask. It owns the queue consumer and the
hourly reminder/retention scheduler so web workers can scale horizontally
without duplicating background jobs.
"""

from __future__ import annotations

import os
import time

os.environ.setdefault("BEAUTYBRIDGE_ROLE", "worker")
os.environ.setdefault("RUN_QUEUE_WORKER", "true")

import universal_runtime as runtime  # noqa: E402


POLL_INTERVAL_SECONDS = 5
SCHEDULE_INTERVAL_SECONDS = 60 * 60


def run_forever() -> None:
    # Run scheduled tasks once at startup, then use a monotonic deadline so
    # Telegram polling frequency does not accidentally control scheduler cadence.
    try:
        runtime.daily_tasks()
    except Exception:
        runtime.LOGGER.exception("Initial background scheduler iteration failed")

    next_scheduled_run = time.monotonic() + SCHEDULE_INTERVAL_SECONDS

    while True:
        try:
            runtime.telegram_poll_once()
        except Exception:
            runtime.LOGGER.exception("Telegram admin polling iteration failed")

        try:
            retried = runtime.retry_pending_admin_notifications()
            if retried:
                runtime.LOGGER.info("Retried delivery of %s pending Telegram booking notification(s)", retried)
        except Exception:
            runtime.LOGGER.exception("Pending Telegram booking notification retry failed")

        now = time.monotonic()
        if now >= next_scheduled_run:
            try:
                runtime.daily_tasks()
            except Exception:
                runtime.LOGGER.exception("Background scheduler iteration failed")
            # Avoid a burst of catch-up runs after a slow or failed scheduled task.
            next_scheduled_run = time.monotonic() + SCHEDULE_INTERVAL_SECONDS

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    run_forever()

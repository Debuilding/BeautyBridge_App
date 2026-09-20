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


def run_forever() -> None:
    # Run once immediately after startup, then once every hour. Per-record
    # idempotency flags make repeated/restarted worker runs safe.
    while True:
        try:
            runtime.daily_tasks()
        except Exception:
            runtime.LOGGER.exception("Background scheduler iteration failed")
        time.sleep(3600)


if __name__ == "__main__":
    run_forever()

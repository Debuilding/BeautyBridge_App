"""Process-role helpers for BeautyBridge background workers.

The web process must never start queue/scheduler threads implicitly. A dedicated
worker process is the only role allowed to run background jobs.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

WEB_ROLE = "web"
WORKER_ROLE = "worker"


def process_role(env: Mapping[str, str] | None = None) -> str:
    source = env if env is not None else os.environ
    role = str(source.get("BEAUTYBRIDGE_ROLE", WEB_ROLE)).strip().lower()
    return role if role in {WEB_ROLE, WORKER_ROLE} else WEB_ROLE


def background_enabled(name: str, env: Mapping[str, str] | None = None) -> bool:
    source = env if env is not None else os.environ
    if process_role(source) != WORKER_ROLE:
        return False

    value = source.get(name)
    if value is None:
        return True

    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}

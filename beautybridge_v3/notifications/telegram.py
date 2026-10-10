"""Telegram Bot API client with bounded retries and no secret logging."""
from __future__ import annotations
import logging
import time
import requests

LOGGER = logging.getLogger(__name__)

class TelegramNotifier:
    def __init__(self, token: str, *, attempts: int = 3, timeout: float = 10):
        self.token = token
        self.attempts = max(1, int(attempts))
        self.timeout = timeout

    def send(self, chat_id: str, text: str) -> bool:
        if not self.token or not chat_id:
            LOGGER.warning("Telegram notification skipped: configuration missing")
            return False
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        for attempt in range(1, self.attempts + 1):
            try:
                response = requests.post(url, json={"chat_id": chat_id, "text": str(text)[:4000]}, timeout=self.timeout)
                if response.ok:
                    return True
                transient = response.status_code == 429 or response.status_code >= 500
                LOGGER.warning("Telegram notification failed: HTTP %s", response.status_code)
                if not transient or attempt == self.attempts:
                    return False
            except requests.RequestException:
                LOGGER.warning("Telegram notification network error (attempt %s/%s)", attempt, self.attempts)
                if attempt == self.attempts:
                    return False
            time.sleep(min(0.5 * attempt, 2.0))
        return False

"""Outbound Instagram messaging via Meta Graph API."""
from __future__ import annotations
import requests

class InstagramSender:
    def __init__(self, page_access_token: str, *, api_version: str = "v21.0", timeout: float = 15):
        self.page_access_token = page_access_token
        self.api_version = api_version
        self.timeout = timeout

    def send_text(self, recipient_id: str, text: str) -> bool:
        if not self.page_access_token or not recipient_id or not text:
            return False
        response = requests.post(
            f"https://graph.facebook.com/{self.api_version}/me/messages",
            params={"access_token": self.page_access_token},
            json={"recipient": {"id": str(recipient_id)}, "message": {"text": str(text)[:2000]}},
            timeout=self.timeout,
        )
        if not response.ok:
            # Never log access tokens or the full request URL (the token is a query parameter).
            raise RuntimeError(f"Instagram send failed with HTTP {response.status_code}")
        return True

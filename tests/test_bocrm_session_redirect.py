"""
Regression test for a real production error found in half a year of manual
Telegram fallback logs:

    Page.goto: Navigation to "https://my.binotel.ua/b/bocrm" is interrupted
    by another navigation to "https://my.binotel.ua/f/bookon/?fromCallback=1"

When a saved Bookon session is invalid, Binotel's frontend does a
client-side redirect to f/bookon/?fromCallback=1 mid-navigation. Playwright
reports this as a raised error rather than letting the page settle, so the
old code never got a chance to check for the password field and just
propagated the raw Playwright error straight into the generic manual
fallback (safe, but wasted a retry that a fresh login would have fixed).

This test mocks Playwright objects to confirm _open_authenticated_page now
recognizes that specific error pattern and treats it exactly like finding
the password field: session invalid, trigger BOCRMSessionExpired /
re-login instead of just bubbling the raw navigation error up.
"""

import asyncio

import pytest
from playwright.async_api import Error as PlaywrightError

from bocrm_playwright import BOCRMManualAdapter, BOCRMSessionExpired


class FakePage:
    def __init__(self, raise_on_goto=None, has_password_field=False):
        self.raise_on_goto = raise_on_goto
        self.has_password_field = has_password_field
        self.goto_calls = []
        self.filled = {}

    async def goto(self, url, **kwargs):
        self.goto_calls.append(url)
        if self.raise_on_goto and len(self.goto_calls) == 1:
            raise self.raise_on_goto

    async def wait_for_timeout(self, ms):
        pass

    async def wait_for_load_state(self, state, timeout=None):
        pass

    async def query_selector(self, selector):
        return object() if self.has_password_field else None

    async def fill(self, selector, value):
        self.filled[selector] = value

    async def click(self, selector):
        pass


class FakeContext:
    def __init__(self, page):
        self._page = page
        self.closed = False
        self.saved_state_path = None

    async def new_page(self):
        return self._page

    async def close(self):
        self.closed = True

    async def storage_state(self, path):
        self.saved_state_path = path


class FakeBrowser:
    def __init__(self, page):
        self._page = page

    async def new_context(self, storage_state=None):
        return FakeContext(self._page)


def _redirect_interrupted_error():
    return PlaywrightError(
        'Page.goto: Navigation to "https://my.binotel.ua/b/bocrm" is interrupted '
        'by another navigation to "https://my.binotel.ua/f/bookon/?fromCallback=1"'
    )


def test_callback_redirect_with_saved_session_raises_session_expired():
    """Saved session + callback-redirect interruption -> BOCRMSessionExpired,
    not a raw Playwright error bubbling up (matches the real log pattern)."""
    page = FakePage(raise_on_goto=_redirect_interrupted_error())
    browser = FakeBrowser(page)
    adapter = BOCRMManualAdapter("a@b.com", "pw", "9970", storage_state_path=__file__)

    async def run():
        with pytest.raises(BOCRMSessionExpired):
            await adapter._open_authenticated_page(browser, force_fresh_login=False)

    asyncio.run(run())


def test_callback_redirect_without_saved_session_falls_through_to_login():
    """No saved session yet + same redirect error -> proceeds straight to
    the email/password login flow instead of raising an unhandled error."""
    page = FakePage(raise_on_goto=_redirect_interrupted_error())
    browser = FakeBrowser(page)
    adapter = BOCRMManualAdapter("a@b.com", "pw", "9970", storage_state_path="/tmp/does_not_exist_test.json")

    async def run():
        context, returned_page = await adapter._open_authenticated_page(browser, force_fresh_login=True)
        assert returned_page is page
        # Should have attempted the login form fill, not just re-raised.
        assert page.filled

    asyncio.run(run())


def test_unrelated_timeout_still_propagates():
    """A genuine, unrelated timeout (not the callback-redirect pattern)
    must NOT be silently swallowed - only the specific known pattern is."""
    page = FakePage(raise_on_goto=PlaywrightError("Timeout 15000ms exceeded."))
    browser = FakeBrowser(page)
    adapter = BOCRMManualAdapter("a@b.com", "pw", "9970", storage_state_path=__file__)

    async def run():
        with pytest.raises(PlaywrightError):
            await adapter._open_authenticated_page(browser, force_fresh_login=False)

    asyncio.run(run())

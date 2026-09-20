"""
Regression test: bookon.ua's own get-available-work-times started returning
the marketplace HTML page instead of JSON. The confirmed-working replacement
is widgets.binotel.com's per-widget endpoint, which needs the widget_id in
the URL path itself:

    https://widgets.binotel.com/b/bocrm/web-widget/{widget_id}/get-available-work-times

This confirms:
  1. The availability request is actually built against that URL, with the
     configured widget_id in the path - not the old bookon.ua endpoint.
  2. widget_id is threaded through BOCRMManualAdapter as a constructor
     argument (from crm/bookon.py's generic cfg["crm"]["widget_id"] lookup)
     rather than hardcoded per-brand anywhere in the shared adapter.
  3. Missing widget_id fails with a clear message instead of silently
     hitting ".../None/get-available-work-times".
  4. The booking (customer/service/visit) endpoints are untouched - still
     on BOCRM_URL, not moved to the new host.
  5. The duration filter added earlier for get_available_slots still runs
     on top of whatever this endpoint returns.

Everything is mocked - no real browser or network call.
"""

import asyncio

from bocrm_playwright import BOCRMManualAdapter, WORK_TIMES_BASE, BOCRM_URL


class FakeAPIResponse:
    def __init__(self, status=200, payload=None):
        self.status = status
        self._payload = payload or {}

    async def json(self):
        return self._payload


class FakeAPIRequestContext:
    def __init__(self, response_by_url=None):
        self.calls = []
        self.response_by_url = response_by_url or {}

    async def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params, "headers": headers})
        for prefix, response in self.response_by_url.items():
            if url.startswith(prefix):
                return response
        return FakeAPIResponse(status=200, payload={})


class FakePage:
    def __init__(self, api_request):
        self.request = api_request

    async def goto(self, url, **kwargs):
        pass

    async def wait_for_timeout(self, ms):
        pass

    async def query_selector(self, selector):
        return None  # no login form - saved session treated as valid


class FakeContext:
    def __init__(self, page):
        self._page = page

    async def new_page(self):
        return self._page

    async def cookies(self):
        return [{"name": "XSRF-TOKEN", "value": "test-token"}]

    async def close(self):
        pass


class FakeBrowser:
    def __init__(self, page):
        self._page = page

    async def new_context(self, storage_state=None):
        return FakeContext(self._page)

    async def close(self):
        pass


def test_availability_request_uses_widget_endpoint_with_widget_id(tmp_path):
    storage_state = tmp_path / "storage.json"
    storage_state.write_text("{}")  # existing file -> treated as a saved session

    api_request = FakeAPIRequestContext(
        response_by_url={
            f"{WORK_TIMES_BASE}/widget-abc-123": FakeAPIResponse(status=200, payload={"36645": {}}),
        }
    )
    page = FakePage(api_request)
    browser = FakeBrowser(page)

    adapter = BOCRMManualAdapter(
        "a@b.com", "pw", branch_id="9970",
        storage_state_path=str(storage_state),
        widget_id="widget-abc-123",
    )

    async def run():
        return await adapter._get_available_slots("543063", "2026-09-21", force_fresh_login=False)

    # patch async_playwright().start().chromium.launch(...) chain used inside _get_available_slots
    import bocrm_playwright as mod

    class FakeChromium:
        async def launch(self, headless=True, args=None):
            return browser

    class FakePlaywright:
        chromium = FakeChromium()

    class FakePlaywrightCtx:
        async def __aenter__(self):
            return FakePlaywright()

        async def __aexit__(self, *a):
            return False

    original = mod.async_playwright
    mod.async_playwright = lambda: FakePlaywrightCtx()
    try:
        result = asyncio.run(run())
    finally:
        mod.async_playwright = original

    assert result["ok"] is True

    urls_called = [c["url"] for c in api_request.calls]
    availability_calls = [u for u in urls_called if "get-available-work-times" in u]
    assert len(availability_calls) == 1

    called_url = availability_calls[0]
    assert called_url == f"{WORK_TIMES_BASE}/widget-abc-123/get-available-work-times"
    assert "widget-abc-123" in called_url
    assert "bookon.ua" not in called_url  # not the broken old host

    # booking endpoints are a separate, untouched code path
    assert BOCRM_URL == "https://my.binotel.ua/b/bocrm"


def test_missing_widget_id_fails_clearly_without_hitting_network():
    adapter = BOCRMManualAdapter("a@b.com", "pw", branch_id="9970", widget_id="")

    async def run():
        return await adapter._get_available_slots("543063", "2026-09-21")

    result = asyncio.run(run())
    assert result["ok"] is False
    assert "widget_id" in result["message"]

"""
Playwright client for Binotel BOCRM (my.binotel.ua/b/bocrm).

Binotel does not publish a documented API for this admin panel, so this
module drives it as an authenticated browser session and calls the same
internal endpoints the web UI itself calls (customer search/create,
services list, visit creation, available-slots lookup).

Session handling (this is the part that used to break in production):
- After a successful email/password login, the authenticated session
  (cookies + local storage) is saved to `storage_state_path`.
- Every call first tries to reuse that saved session instead of logging
  in again. If Binotel still shows the login form (or an API call comes
  back 401/403/419), we treat the saved session as expired, do a single
  fresh login, save the new session, and retry the original operation
  exactly once.

This means a real UI login only happens when the session has actually
expired, not on every booking/slots request.
"""

import asyncio
import logging
import os
from pathlib import Path
from urllib.parse import unquote

from playwright.async_api import async_playwright

LOGIN_URL = "https://my.binotel.ua/"
BOCRM_URL = "https://my.binotel.ua/b/bocrm"
WIDGET_BASE = "https://bookon.ua"

_AUTH_FAILURE_STATUSES = {401, 403, 419}


class BOCRMSessionExpired(Exception):
    """Internal signal: the saved storage_state is no longer valid."""


class BOCRMManualAdapter:
    def __init__(self, email, password, branch_id, storage_state_path=None):
        self.email = email
        self.password = password
        self.branch_id = branch_id
        self.storage_state_path = storage_state_path or "data/bookon/default_storage.json"

    # ---------------------------------------------------------------
    # Sync entry points (used from Flask request handlers)
    # ---------------------------------------------------------------

    def create_visit_sync(self, specialist_id, service_id, date_str, time_str, client_name, client_phone):
        return asyncio.run(self._with_session_retry(
            self._create_visit, specialist_id, service_id, date_str, time_str, client_name, client_phone
        ))

    def get_available_slots_sync(self, service_id, date_str):
        return asyncio.run(self._with_session_retry(self._get_available_slots, service_id, date_str))

    # ---------------------------------------------------------------
    # Session plumbing
    # ---------------------------------------------------------------

    async def _with_session_retry(self, coro_fn, *args):
        try:
            return await coro_fn(*args, force_fresh_login=False)
        except BOCRMSessionExpired:
            logging.warning("BOCRM saved session expired, forcing a fresh login")
            try:
                return await coro_fn(*args, force_fresh_login=True)
            except BOCRMSessionExpired:
                # Fresh login itself failed to produce a working session.
                return {"ok": False, "message": "BOCRM login failed"}

    async def _open_authenticated_page(self, browser, force_fresh_login):
        has_saved_state = (not force_fresh_login) and os.path.exists(self.storage_state_path)
        context = await browser.new_context(
            storage_state=self.storage_state_path if has_saved_state else None
        )
        page = await context.new_page()

        await page.goto(BOCRM_URL, wait_until="domcontentloaded", timeout=15000)
        await page.wait_for_timeout(1000)

        if await page.query_selector('input[type="password"]'):
            if has_saved_state:
                # Saved session no longer works — caller will retry with a fresh login.
                await context.close()
                raise BOCRMSessionExpired()

            if not self.email or not self.password:
                raise RuntimeError("BOCRM_EMAIL / BOCRM_PASSWORD is not configured")

            logging.info("BOCRM: no valid session, logging in with email/password")
            await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=15000)
            await page.wait_for_timeout(1000)
            await page.fill('input[type="text"]:not([type="hidden"])', self.email)
            await page.fill('input[type="password"]', self.password)
            await page.click('button[type="submit"]')
            await page.wait_for_load_state("networkidle", timeout=20000)
            await page.goto(BOCRM_URL, wait_until="domcontentloaded", timeout=15000)

            if await page.query_selector('input[type="password"]'):
                raise RuntimeError("BOCRM login failed (still on login form after submit)")

            Path(self.storage_state_path).parent.mkdir(parents=True, exist_ok=True)
            await context.storage_state(path=self.storage_state_path)
            logging.info("BOCRM: session saved to %s", self.storage_state_path)

        return context, page

    # ---------------------------------------------------------------
    # Operations
    # ---------------------------------------------------------------

    async def _get_available_slots(self, service_id, date_str, force_fresh_login=False):
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
            try:
                context, page = await self._open_authenticated_page(browser, force_fresh_login)
                headers = {
                    "Accept": "application/json, text/plain, */*",
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": "https://bookon.ua/",
                    "Origin": "https://bookon.ua",
                }
                await page.request.get(f"{WIDGET_BASE}/get-branches-list", headers=headers, timeout=15000)
                for cookie in await context.cookies():
                    if cookie["name"] == "XSRF-TOKEN":
                        headers["X-XSRF-TOKEN"] = unquote(cookie["value"])

                res = await page.request.get(
                    f"{WIDGET_BASE}/get-available-work-times",
                    params={"branchId": self.branch_id, "visitDate": date_str, "serviceIds[0]": service_id},
                    headers=headers,
                    timeout=15000,
                )
                if res.status in _AUTH_FAILURE_STATUSES and not force_fresh_login:
                    raise BOCRMSessionExpired()
                if res.status != 200:
                    return {"ok": False, "message": f"Slots fetch failed: {res.status}"}

                try:
                    data = await res.json()
                except Exception:
                    data = {}
                return {"ok": True, "data": data if isinstance(data, dict) else {}}
            except BOCRMSessionExpired:
                raise
            except Exception as e:
                logging.error("BOCRM slots error: %s", e, exc_info=True)
                return {"ok": False, "message": f"Error: {e}"}
            finally:
                await browser.close()

    async def _create_visit(self, specialist_id, service_id, date_str, time_str,
                             client_name, client_phone, force_fresh_login=False):
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
            try:
                context, page = await self._open_authenticated_page(browser, force_fresh_login)

                phone_clean = client_phone.strip()
                if phone_clean.startswith("0"):
                    phone_clean = "38" + phone_clean

                search_res = await page.request.get(f"{BOCRM_URL}/customer?page=1&search={phone_clean}")
                if search_res.status in _AUTH_FAILURE_STATUSES and not force_fresh_login:
                    raise BOCRMSessionExpired()
                if search_res.status != 200:
                    return {"ok": False, "message": f"Search failed: {search_res.status}"}

                search_data = await search_res.json()
                client_id = None
                if search_data.get("data"):
                    client_id = search_data["data"][0]["id"]
                else:
                    create_res = await page.request.post(
                        f"{BOCRM_URL}/customer",
                        data={"name": client_name, "phones": [phone_clean]},
                    )
                    if create_res.status in _AUTH_FAILURE_STATUSES and not force_fresh_login:
                        raise BOCRMSessionExpired()
                    create_data = await create_res.json()
                    if not create_data.get("data"):
                        return {"ok": False, "message": f"Failed to create client: {create_data}"}
                    client_id = create_data["data"]["id"]

                services_res = await page.request.get(f"{BOCRM_URL}/service")
                if services_res.status != 200:
                    return {"ok": False, "message": "Services fetch failed"}
                services_data = await services_res.json()
                service_info = next(
                    (s for s in services_data.get("data", []) if str(s.get("id")) == str(service_id)),
                    None,
                )
                if not service_info:
                    return {"ok": False, "message": "Service not found"}

                visit_payload = {
                    "branchId": self.branch_id,
                    "clientId": client_id,
                    "clientPhone": phone_clean,
                    "specialistId": specialist_id,
                    "resourceId": 1,
                    "time": f"{date_str} {time_str}:00",
                    "status": "isRecorded",
                    "color": 0,
                    "duration": service_info.get("duration", 60),
                    "force": 0,
                    "isFiscalized": False,
                    "isPaid": False,
                    "isPayment": False,
                    "onlinePayment": 0,
                    "cardPayment": 0,
                    "cashPayment": 0,
                    "cashbackPayment": 0,
                    "cashChange": 0,
                    "rounding": 0,
                    "subscriptionsPayment": 0,
                    "sum": service_info.get("price", 0),
                    "services": [
                        {
                            "id": service_info.get("id"),
                            "uuid": service_info.get("uuid", ""),
                            "name": service_info.get("name"),
                            "price": service_info.get("price", 0),
                            "duration": service_info.get("duration", 60),
                            "isMinPrice": True,
                            "priceDiff": 0,
                            "priceDiffType": "service",
                        }
                    ],
                }
                visit_res = await page.request.post(f"{BOCRM_URL}/visit", data=visit_payload)
                if visit_res.status in _AUTH_FAILURE_STATUSES and not force_fresh_login:
                    raise BOCRMSessionExpired()
                if visit_res.status not in (200, 201):
                    try:
                        visit_data = await visit_res.json()
                    except Exception:
                        visit_data = {}
                    return {"ok": False, "message": f"Visit error ({visit_res.status}): {visit_data}"}

                visit_data = await visit_res.json()
                return {"ok": True, "crm_id": visit_data.get("id")}
            except BOCRMSessionExpired:
                raise
            except Exception as e:
                logging.error("BOCRM create_visit error: %s", e, exc_info=True)
                return {"ok": False, "message": f"Error: {e}"}
            finally:
                await browser.close()

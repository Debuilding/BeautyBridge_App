"""Separate Flask entry point for BeautyBridge V3 validation and staging."""
from __future__ import annotations
import logging
import hmac
import json
import os
from flask import Flask, jsonify, request
from beautybridge_v3.channels.instagram import normalize_events, verify_signature
from beautybridge_v3.channels.instagram_sender import InstagramSender
from beautybridge_v3.config import Settings
from beautybridge_v3.notifications.telegram import TelegramNotifier
from beautybridge_v3.service import BeautyBridgeService
from beautybridge_v3.storage import Storage

settings = Settings.from_env()
storage = Storage(settings.database_path)
service = BeautyBridgeService(settings, storage, TelegramNotifier(settings.telegram_bot_token))
app = Flask("beautybridge_v3")
logging.basicConfig(level=logging.INFO)


def tenant_for_page(page_id: str):
    for tenant_id, tenant in settings.tenants.items():
        if tenant.enabled and str(getattr(tenant, "page_id", "")) == str(page_id):
            return tenant_id
    return None


@app.post("/admin/booking-status")
def admin_booking_status():
    expected = settings.admin_api_token
    supplied = request.headers.get("Authorization", "")
    if not expected or not hmac.compare_digest(supplied, "Bearer " + expected):
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"ok": False, "error": "invalid_payload"}), 400
    try:
        request_id = int(body.get("request_id"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "invalid_request_id"}), 400
    status = str(body.get("status") or "")
    if status not in {"confirmed", "rejected", "alternative_proposed"}:
        return jsonify({"ok": False, "error": "invalid_status"}), 400
    booking = storage.get_booking_request(request_id)
    if booking is None:
        return jsonify({"ok": False, "error": "not_found"}), 404
    changed = service.admin_set_status(request_id, status, str(body.get("note") or ""))
    if not changed:
        return jsonify({"ok": False, "error": "request_not_pending"}), 409
    state = storage.get_conversation(booking["tenant_id"], booking["sender_id"])
    customer_message = str(state.get("last_admin_message") or "")
    try:
        token_map = json.loads(os.getenv("BBV3_INSTAGRAM_TOKENS_JSON", "{}"))
    except json.JSONDecodeError:
        token_map = {}
    token = str(token_map.get(booking["tenant_id"], "")) if isinstance(token_map, dict) else ""
    delivery = "not_configured"
    if token and customer_message:
        try:
            InstagramSender(token).send_text(booking["sender_id"], customer_message)
            delivery = "sent"
        except Exception:
            app.logger.exception("Admin status saved but customer notification delivery failed")
            delivery = "failed"
    return jsonify({"ok": True, "request_id": request_id, "status": status, "customer_notification": delivery}), 200

@app.get("/health")
def health():
    return jsonify({"ok": True, "service": "beautybridge-v3", "mode": "isolated"})

@app.get("/webhooks/instagram")
def verify_instagram_webhook():
    mode = request.args.get("hub.mode", "")
    token = request.args.get("hub.verify_token", "")
    challenge = request.args.get("hub.challenge", "")
    if mode == "subscribe" and settings.meta_verify_token and token == settings.meta_verify_token:
        return challenge, 200
    return "Forbidden", 403

@app.post("/webhooks/instagram")
def receive_instagram_webhook():
    raw = request.get_data(cache=True)
    if settings.require_meta_signature and not verify_signature(raw, request.headers.get("X-Hub-Signature-256", ""), settings.meta_app_secret):
        return jsonify({"ok": False, "error": "invalid_signature"}), 401
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "invalid_payload"}), 400
    accepted = 0
    ignored = 0
    for event in normalize_events(payload):
        tenant_id = tenant_for_page(event["page_id"])
        if not tenant_id:
            ignored += 1
            continue
        # Initial stage accepts events into durable storage. Live AI/channel
        # responses are enabled only after the extraction and reply adapter are
        # contract-tested against the actual Meta app configuration.
        inserted = storage.enqueue_once(event["event_id"], tenant_id, event["sender_id"], {"text": event["text"], "page_id": event["page_id"], "extracted": {}})
        accepted += int(inserted)
        ignored += int(not inserted)
    return jsonify({"ok": True, "accepted": accepted, "ignored": ignored}), 200

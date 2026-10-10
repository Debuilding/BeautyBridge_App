# BeautyBridge V3 staging configuration

V3 is isolated from the existing production entry point. Do not change the current service's start command or point the live Meta webhook to this entry point until staging tests pass.

## Required environment variables

- `BBV3_DATABASE_PATH`: durable SQLite path on a persistent disk, for example `/var/lib/beautybridge-v3/data.sqlite3`.
- `BBV3_TENANTS_JSON`: tenant business settings (example below). Never put access tokens in this JSON.
- `BBV3_INSTAGRAM_TOKENS_JSON`: JSON object mapping tenant IDs to Instagram/Page access tokens. Keep this value in the host's secret environment settings.
- `BBV3_META_VERIFY_TOKEN`: random webhook verification value.
- `BBV3_META_APP_SECRET`: Meta app secret used to validate `X-Hub-Signature-256`.
- `BBV3_OPENAI_API_KEY`: OpenAI API key.
- `BBV3_TELEGRAM_BOT_TOKEN`: Telegram bot token.
- `BBV3_ADMIN_API_TOKEN`: long random token for the admin status endpoint.
- `BBV3_REQUIRE_META_SIGNATURE`: keep `true` for any shared/staging/production endpoint.
- Optional: `BBV3_OPENAI_MODEL` (default `gpt-4o-mini`).

Example tenant configuration with fake values only:

```json
{
  "rozmary": {
    "name": "Rozmary",
    "page_id": "META_PAGE_ID",
    "language": "uk",
    "timezone": "Europe/Kyiv",
    "crm_type": "manual",
    "admin_chat_id": "TELEGRAM_ADMIN_CHAT_ID",
    "enabled": true,
    "services": [
      {"id": "manicure", "name": "Манікюр", "price": 850, "photo_required": true}
    ],
    "masters": [
      {"id": "master-1", "name": "Майстер 1"}
    ],
    "business_facts": "Add approved salon address, schedule, service details and policies here."
  }
}
```

Example token map (put the actual value only in the hosting provider's secret environment settings):

```json
{
  "rozmary": "INSTAGRAM_PAGE_ACCESS_TOKEN"
}
```

## Separate processes

Web process:

```bash
gunicorn v3_app:app --bind 0.0.0.0:$PORT --workers 2 --timeout 120
```

Queue worker:

```bash
python -m beautybridge_v3.worker
```

The web process only validates and stores inbound events. The worker processes the queue and sends replies. Both processes must use the same durable database path. Do not use ephemeral storage for a staging or live deployment.

## Admin booking status

The token-protected `POST /admin/booking-status` accepts JSON such as:

```json
{"request_id": 123, "status": "confirmed"}
```

Send header `Authorization: Bearer <BBV3_ADMIN_API_TOKEN>`. Allowed statuses are `confirmed`, `rejected`, and `alternative_proposed`. Only a request currently marked `pending_manual_confirmation` can transition. Keep the admin token private.

## Important current limits

- Bookon is manual-only; V3 does not query Bookon availability.
- The V3 AI layer currently uses the OpenAI chat completions API. The server validates date/time and configured service/master choices.
- Verify real Meta payloads and photo URL lifetime in a test Instagram account before relying on image intake.
- The admin status API currently changes the database state before attempting the Instagram customer notification. Delivery retry/outbox handling must be completed before production.
- SQLite is suitable for initial isolated testing; multi-instance/high-volume deployment needs a shared transactional database and an explicit queue/outbox design.

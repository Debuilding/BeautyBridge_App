# BeautyBridge V3 — Python-only architecture

This branch introduces an isolated V3 implementation alongside the existing production code. It does not replace the current entry point, database, integrations, or deployment configuration.

## Principles

- Python only for orchestration and business logic. No n8n, Make, or workflow-builder dependency.
- Tenant-scoped configuration and data.
- AI interprets language; deterministic Python code owns state transitions, validation, booking status, and external side effects.
- Rozmary/Bookon uses manual confirmation. The application must not query Bookon availability or claim a booking is confirmed before an authorized admin confirms it.
- Webhook idempotency, durable inbound queue, audit events, bounded retries, and explicit statuses.
- Secrets are read from environment variables and never committed.

## Initial V3 modules

- `beautybridge_v3/config.py`: environment and tenant configuration.
- `beautybridge_v3/storage.py`: SQLite persistence, schema migrations, queue, deduplication, audit trail.
- `beautybridge_v3/conversation.py`: controlled booking state machine and request capture.
- `beautybridge_v3/crm/`: explicit adapter registry and manual fallback.
- `beautybridge_v3/channels/instagram.py`: Meta webhook parsing and signature verification.
- `beautybridge_v3/notifications/telegram.py`: Telegram admin notifications.
- `beautybridge_v3/service.py`: coordinates channel, conversation, booking, and notifications.
- `v3_app.py`: separate Flask entry point for isolated validation.
- `tests/test_v3_architecture.py`: safety and persistence tests.

## Status lifecycle

`draft -> pending_manual_confirmation -> confirmed | rejected | alternative_proposed`

Only the admin-confirmation path may move a request to `confirmed`. A request submitted by a client is never a confirmed appointment.

## Local test

```bash
python -m pip install -r requirements.txt
pytest -q tests/test_v3_architecture.py
```

For an isolated local run, set `BBV3_DATABASE_PATH=/tmp/beautybridge-v3.sqlite3` and run `flask --app v3_app run`. Configure Meta and Telegram environment variables before connecting live channels.

## Safe rollout plan

1. Keep this branch isolated; do not change the production branch or server service.
2. Run unit tests and static checks.
3. Add contract tests for the current Instagram webhook payload and Telegram admin flow.
4. Run a staging webhook with a separate test page/bot and verify duplicate-event handling.
5. Compare V3 behaviour against existing production tests and inspect DB migration/backup strategy.
6. Only after explicit approval, plan a separate staging deployment. Production cutover is a later, reversible step.

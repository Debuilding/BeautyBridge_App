# BeautyBridge App v2

Universal AI receptionist for beauty businesses.

## Product architecture

- Instagram/Meta channel per client
- Universal CRM adapter layer
- Bookon private adapter isolated from the core
- Manual/Home Master mode for professionals without a CRM
- SQLite as MVP source of truth
- Telegram notifications and manual fallback
- Configuration-driven clients instead of hard-coding a single salon
- Landing page positioned as a universal product, not a Bookon-only bot

## Home Master mode

A home-based master does not need a CRM. BeautyBridge collects the service, date, preferred time, name and phone, stores the request locally and notifies the master in Telegram. Appointments can be exported as CSV and opened directly in Excel. A ready XLSX template is included under `data/appointments_template.xlsx`.

Excel is an operator/export format, not the primary database. This prevents data loss when the file is closed, moved or edited.

## Bookon

Bookon is isolated in `crm/bookon.py`. The adapter uses a persistent Playwright storage state and can re-authenticate when the web session expires. This is a private/internal integration and may break if Binotel changes its internal frontend/API. The rest of BeautyBridge does not depend on those implementation details.

## Adding clients

Client configuration is stored in `clients.json`; secrets belong in environment variables. The next SaaS step is a web onboarding panel so an operator can connect Instagram, choose CRM/manual mode and configure services, masters, payments, language and booking rules without editing Python.

## Run

```bash
pip install -r requirements.txt
playwright install chromium
gunicorn -w 1 -b 0.0.0.0:5000 main:app
```

For Railway, use the included Dockerfile so Chromium is installed during image build.

## Production roadmap

1. PostgreSQL for multi-tenant production
2. Web onboarding/admin panel
3. OAuth/token connection flows where providers support them
4. CRM capability discovery
5. Background job queue for reminders and retention
6. Monitoring and per-client health checks
7. More CRM adapters

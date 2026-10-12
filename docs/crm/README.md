# CRM integration documentation registry

BeautyBridge currently runs every tenant in manual booking mode. No CRM endpoint is called unless a dedicated adapter is implemented, reviewed, and explicitly enabled.

## Per-client documentation

Each tenant has a reserved documentation path:

`docs/crm/<client_id>/API_DOCUMENTATION.md`

The path can be overridden in that tenant's `crm.api_documentation_path`; an official vendor documentation URL belongs in `crm.api_documentation_url`. Keep credentials and private tokens out of these files.

## Onboarding a CRM-enabled client

1. Create the client's API_DOCUMENTATION.md from TEMPLATE/API_DOCUMENTATION.md.
2. Record the official API docs URL, auth method, tenant/account identifiers, API version, rate limits, and verified endpoints.
3. Confirm availability lookup, booking creation, cancellation/rescheduling, service/master mapping, idempotency, and webhook behavior separately.
4. Implement a provider-specific adapter under `crm/adapters/<provider>.py` using `crm/base.py`'s contract.
5. Add isolated mocked tests for each capability and explicit adapter registration.
6. Enable that adapter for that client only after review and tests pass.

Do not copy credentials into documentation, fixtures, logs, or source code. Use environment variables or a secret manager. A documentation URL alone never enables a CRM.

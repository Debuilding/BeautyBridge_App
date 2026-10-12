# Provider-specific CRM adapters

This directory is reserved for future provider adapters. The current production path is manual-only.

Each adapter must:
- implement the provider-neutral contract in `crm/base.py`;
- declare truthful capabilities (availability, booking, cancellation, rescheduling, etc.);
- use only endpoints verified against the tenant's official API documentation;
- perform a final availability check immediately before booking when supported;
- implement idempotency and typed error handling;
- never fall back to another vendor's adapter;
- include isolated tests with mocked HTTP responses;
- be explicitly enabled per tenant only after review.

Do not add a vendor adapter just because a documentation URL exists. Unknown or unimplemented CRM types must remain safely manual/unsupported.

# CRM API integration brief — TEMPLATE

> Copy this file to `docs/crm/<client_id>/API_DOCUMENTATION.md`. Do not put secrets here.

## Tenant and vendor
- Client ID:
- CRM/vendor:
- Official API documentation URL:
- API version / last verified date:
- Vendor support contact:

## Authentication
- Method (OAuth2/API key/session/etc.):
- Token endpoint / auth flow:
- Required scopes/permissions:
- Token expiry and refresh:
- Secret environment-variable names (names only, never values):

## Tenant identifiers
- Account / organization ID:
- Location / branch ID:
- Time zone:
- API base URL (official/documented):

## Required endpoint inventory
| Capability | HTTP method + path | Required fields | Response fields | Pagination/rate limits | Verified |
|---|---|---|---|---|---|
| List services | | | | | No |
| List masters/employees | | | | | No |
| Get availability | | | | | No |
| Final slot re-check | | | | | No |
| Create booking | | | | | No |
| Find booking / idempotency lookup | | | | | No |
| Cancel booking | | | | | No |
| Reschedule booking | | | | | No |
| Webhook / booking status | | | | | No |

## Mapping
- BeautyBridge service ID → CRM service ID:
- BeautyBridge master ID → CRM employee ID:
- Status mapping:
- Duration / buffer rules:
- Currency and price handling:

## Reliability and safety
- Idempotency support:
- Duplicate-booking behavior:
- Conflict response for unavailable slot:
- Retryable vs permanent errors:
- Rate limits and Retry-After behavior:
- Webhook signature verification:
- PII / data-retention constraints:

## Test evidence
- Mocked unit tests:
- Sandbox account tested:
- Availability re-check verified:
- Duplicate request verified:
- Failure/retry scenarios verified:
- Reviewer and date:

## Enablement decision
- Adapter module:
- Capabilities approved:
- Client-specific enablement flag:
- Review notes:
- Status: NOT IMPLEMENTED / IN REVIEW / APPROVED

# Shared Instagram account routing

BeautyBridge can route one Instagram Direct account to multiple salon locations. Set the non-secret environment variable:

```text
SHARED_INSTAGRAM_BRANDS=rozmary,space
```

The values are tenant IDs from `CLIENTS_CONFIG_JSON` or `clients.local.json`, separated by commas. At least one tenant in the list must have its existing `PAGE_ID` and `PAGE_ACCESS_TOKEN` configured. Other listed tenants with missing Instagram credentials inherit those values only when the listed tenants do not contain conflicting page IDs or tokens.

For the Rozmary/Space setup, the bot asks a new customer to choose a location, stores the choice in SQLite independently from expiring chat state, and routes later messages to the selected tenant. Customers can switch by naming the other location or asking to change location. Booking requests and admin notifications continue to use the selected tenant's configuration.

The setting is intentionally opt-in so unrelated tenants are not silently connected to the same Instagram account. It contains no credentials and should be configured in the deployment environment, not committed as a token.

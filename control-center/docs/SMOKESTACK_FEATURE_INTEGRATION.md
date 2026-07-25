# SmokeStack ↔ PFS Feature-Check Integration Contract

**Do not implement yet.** This documents how SmokeStack (ERP #1), or any future ERP, will consume the PFS feature-management plane in a later milestone. In Milestone 1, SmokeStack business logic is unchanged and the evaluation endpoint is guarded by the PFS operator token so it is testable from the Control Center only.

## Principle

PFS is the **control plane**; SmokeStack is the **data plane**. SmokeStack asks PFS "may this context use this feature?" and enforces the answer server-side. PFS never sees SmokeStack customer transactional data; SmokeStack never trusts a client-provided role, plan, or owner flag.

## Endpoint

`POST https://pfs-control-center.onrender.com/api/internal/feature-check`

Request body:

```json
{
  "feature_key": "inventory.batch_repair",
  "erp_product_id": "smokestack",
  "actor_type": "customer",             // customer | internal | platform_owner
  "environment": "production",          // development | staging | production
  "customer_ref": "1",                  // the SmokeStack company_id / tenant ref
  "user_id": "u_123",
  "role": "admin",                      // SmokeStack role, mapped to PFS vocabulary
  "license_plan": "pro",                // SmokeStack plan, mapped to PFS vocabulary
  "support_session_ref": null            // required for owner/internal tools
}
```

Response:

```json
{ "allow": true, "reason": "granted", "visibility": "customer",
  "feature_key": "inventory.batch_repair", "erp_product_id": "smokestack" }
```

`reason` is a stable string for logging (e.g. `role_not_permitted`, `license_not_entitled`, `feature_disabled`, `owner_only:session_expired`). Treat any non-`allow` as **deny**.

## Exact integration steps (later milestone)

1. **Auth swap.** Replace the `require_internal` operator-token guard on `/api/internal/feature-check` with per-ERP **service-token** verification (signed, rotating). Keep the operator-token path for the Control Center console's Access Preview. The Platform-Owner status must continue to come only from a server-validated token / support session — never a client value.

2. **Client helper.** Add to SmokeStack a single function:

   ```python
   def feature_enabled(key, *, customer_ref, user_id=None, role=None,
                       license_plan=None, environment="production",
                       actor_type="customer", support_session_ref=None) -> bool:
       # POST to /api/internal/feature-check with the service token.
       # On timeout / non-2xx / network error -> return False (FAIL CLOSED).
   ```

3. **Fail closed.** Any error, timeout, or malformed response denies. A missing/invalid rule already denies on the PFS side; the client must not "open" on error.

4. **Short cache.** Cache `(key, context) -> (allow, expires_at)` for ~30–60 s to bound load; honor a short TTL so flag flips propagate quickly. Never cache across `customer_ref` or `role`.

5. **Server-side enforcement.** Gate SmokeStack routes, API handlers, and menu/nav items on `feature_enabled(...)`. Return **HTTP 403** on deny. Never rely on frontend hiding; a customer who guesses an internal URL must be refused by the server.

6. **Vocabulary mapping.** Maintain a small map from SmokeStack roles → PFS `role_requirements` values and SmokeStack plans → PFS `license_plan_requirements` values, so the three gates line up.

7. **Owner tools.** When rendering Platform-Owner Mode inside SmokeStack (System Diagnostics, Feature Flags, License Overrides, etc.), pass `actor_type=platform_owner` and the active PFS `support_session_ref`. PFS enforces that the session is live, non-revoked, non-expired, and scoped to `smokestack`.

## Three gates (must all pass for customer-facing access)

`Feature released (default_state OR allowlist OR rollout)` **AND** `license_plan ∈ license_plan_requirements (if any)` **AND** `role ∈ role_requirements (if any)`. A `customer_denylist` entry overrides all of the above.

## Non-goals for Milestone 1

- No SmokeStack code changes.
- No service tokens issued yet.
- No customer data leaves SmokeStack; PFS stores flag metadata only.

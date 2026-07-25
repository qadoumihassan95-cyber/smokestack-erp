# PFS Feature Management & Internal Tools — Milestone 1 Deliverables

Status: implemented in the PFS Control Center on branch `feature-flags`. **SmokeStack business logic is unchanged.** Not deployed to Production — validate on Development first.

---

## 1. Architecture summary

The Control Center now owns a first-class **feature-flag + access-control plane**. Every governed feature or internal tool is represented by a `FeatureFlag` row (metadata only — no secrets). Access is decided **server-side, deny-by-default** by a single pure function, `evaluate_feature()`, exposed to ERPs through `POST /api/internal/feature-check`.

Two protected namespaces were added, both behind `require_internal` (a validated PFS operator whose `platform_role ∈ {owner, operator, internal}`):

- `/api/platform/*` — owner-only **management** (create / list / update / archive flags, read audit).
- `/api/internal/*` — the **evaluation contract** an ERP calls at runtime.

Platform-Owner status is never read from a query parameter, header, cookie, or any client value — only from the server-validated operator identity, and (for elevated visibility levels) a live PFS **support session**. The frontend Features console is a thin management/preview surface; it grants nothing on its own.

Five visibility levels model the audience:

| Level | Who can reach it | Requires support session |
|---|---|---|
| `customer` | Customers, subject to the three gates | no |
| `experimental` | Hidden; owner/internal (session) or explicit allowlist / rollout | for owner/internal path |
| `internal_team` | Internal team only | yes |
| `platform_owner_only` | Platform Owner only | yes |
| `disabled` | Nobody — hard off even if the route/key is known | n/a |

The **three gates** for customer-facing access are enforced in order and are all deny-by-default: *feature released* (default_state OR allowlist OR rollout) **AND** *license entitlement* (`license_plan_requirements`) **AND** *role permission* (`role_requirements`). A denylist entry overrides everything.

---

## 2. Data-model changes (`models.py`)

Two additive tables, metadata only. `erp_product_id = NULL` means "applies to all ERP products".

**`feature_flags`** — `id, key, name, description, erp_product_id(FK,null), module, visibility, default_state, environment_scope, customer_allowlist, customer_denylist, user_allowlist, role_requirements, license_plan_requirements, rollout_percentage, start_date, expiry_date, status(active|archived), created_by, updated_by, created_at, updated_at`.

**`feature_flag_audit`** — `id, feature_flag_id(FK), feature_key, erp_product_id, actor_operator_id, actor_type, customer_ref, environment, action, before_state(JSON), after_state(JSON), reason, at`.

No existing table or column was modified. No secrets, passwords, tokens, or customer transactional data are stored.

---

## 3. API contract

Management (owner-only, `require_internal`, 401 without token / 403 for wrong tier):

- `GET  /api/platform/products/{pid}/feature-flags?q=&visibility=` → list (product-specific + global).
- `GET  /api/platform/feature-flags/{id}` → one flag.
- `POST /api/platform/feature-flags` → create (`201`; `409` duplicate key in scope; `422` bad visibility/rollout).
- `PATCH /api/platform/feature-flags/{id}` → partial update; writes a diffed audit row (`feature_enabled` / `feature_disabled` / `feature_archived` / `feature_restored` / `allowlist_changed` / `feature_updated`).
- `GET  /api/platform/feature-flags/{id}/audit` → up to 200 newest audit rows with before/after/reason.

Evaluation contract (called by an ERP):

- `POST /api/internal/feature-check`
  Request: `{feature_key, erp_product_id, actor_type, environment, customer_ref, user_id, role, license_plan, support_session_ref}`.
  Response: `{allow: bool, reason: str, visibility, feature_key, erp_product_id}`.
  Sensitive decisions are audited: `owner_tool_opened`, `experimental_accessed`, and `unauthorized_access_denied` (customer denied an owner-only/internal/disabled feature).

Deny reasons are stable strings for ERP-side logging: `unknown_feature`, `flag_archived`, `not_started`, `expired`, `environment_not_in_scope`, `feature_disabled`, `owner_only:*`, `internal_only:*`, `customer_denylisted`, `role_not_permitted`, `license_not_entitled`, `experimental_not_enabled`, `not_targeted`. Session sub-reasons: `no_session`, `session_not_found`, `session_revoked`, `session_expired`, `session_wrong_erp`.

---

## 4. Authorization matrix

| Visibility | customer actor | internal actor (valid session) | platform_owner (valid session) | notes |
|---|---|---|---|---|
| `customer` | ALLOW iff 3 gates pass | same as customer | same as customer | denylist overrides |
| `experimental` | ALLOW iff allowlist/rollout | ALLOW | ALLOW | hidden by default |
| `internal_team` | **DENY** (`internal_only`) | ALLOW | ALLOW | needs session |
| `platform_owner_only` | **DENY** (`owner_only`) | DENY | ALLOW | owner + session only |
| `disabled` | **DENY** | **DENY** | **DENY** | hard off |

Session validity is required for every elevated ALLOW: an expired, revoked, missing, or wrong-ERP session denies. A customer manually calling the internal API with a known key still gets `allow:false` (and the attempt is audited); a customer calling the `/api/platform/*` namespace with no operator token gets **HTTP 401**, and a non-internal operator gets **HTTP 403** — never relying on UI hiding.

---

## 5. UI changes (`index.html`)

A new **Features** tab was added to the 12-module ERP workspace nav (between Reports and Licenses; flag icon). The page provides:

- A data table of flags with visibility badges (Customer / Owner Only / Internal / Experimental / Disabled), state (On / Targeted / Off / Archived), environment scope, a targeting summary, and last-updated.
- Search by key/name and per-visibility filter chips with counts.
- One-click enable/disable for customer/experimental flags.
- A create/edit modal covering every field (visibility, default state, rollout %, environment scope, allow/deny lists, role & license requirements, start/expiry, audited reason).
- A detail drawer with full metadata, **audit history**, and an **Access Preview** that calls the real `/api/internal/feature-check` so the owner can confirm exactly what a given actor/customer/role/plan/environment would see. The preview surfaces the true server decision — it cannot reveal an owner-only or disabled feature to a customer context.

The UI never decides access; it only manages metadata and previews the backend's decision.

---

## 6. Migration

`migrations/versions/0002_feature_flags.py`, `down_revision = 0001_init`. Additive and inspector-guarded: on a fresh DB (where `0001` already ran `metadata.create_all`) the create is skipped; on the existing Production DB the two tables are created. Fully reversible (`downgrade` drops both if present). Verified `upgrade head → downgrade 0001_init → upgrade head` on SQLite: 14 → 12 → 14 tables, flags present/absent/present as expected.

---

## 7. Tests

`tests/test_control_center.py` grew from 41 to **65 passing tests** (SQLite, hermetic). New coverage:

- Namespace is deny-by-default: 401 without a token on both `/api/platform/*` and `/api/internal/*`; `require_internal` returns 403 for a non-internal operator tier.
- **Customers cannot access owner-only or internal features** (both blocked at the namespace and denied by evaluation).
- **Disabled features are inaccessible even with a known route/key** — denied for customers *and* for a platform owner holding a valid session.
- **Expired and revoked support sessions** deny elevated access; wrong-ERP session denied.
- Three-gate customer access (role AND license AND flag), denylist override, not-targeted default deny, allowlist and 100 % rollout grants.
- Environment scoping, expiry windows, archived flags.
- CRUD writes diffed before/after audit with reason; sensitive access decisions (`owner_tool_opened`, `unauthorized_access_denied`) are audited.
- Global (all-products) flag applies when no product-specific flag exists.

Also validated: `ruff` clean; `node --check` on the extracted SPA JS; a jsdom smoke test of the Features page (render, filter, search, drawer, preview deny path, new-flag form).

---

## 8. Rollout / rollback plan

**Rollout (Development first):** merge `feature-flags` → deploy to the Development Control Center → run `alembic upgrade head` (already folded into the start command) → smoke test the Features tab and `/api/internal/feature-check`. Promote to Production only after sign-off; migration `0002` is additive and safe to run against the live DB with no downtime.

**Rollback:** the feature is inert until flags are created and an ERP starts calling the evaluation endpoint, so a code revert is sufficient in practice. If a schema rollback is required, `alembic downgrade 0001_init` drops the two new tables and leaves all pre-existing tables untouched. Because SmokeStack is unchanged, there is no data-plane rollback to coordinate.

---

## 9. Future SmokeStack integration steps (exact)

See `docs/SMOKESTACK_FEATURE_INTEGRATION.md` for the full contract. Summary of the exact steps, to be done in a **later** milestone (not now):

1. Issue SmokeStack a per-ERP signed **service token** and swap the `require_internal` guard on `/api/internal/feature-check` for service-token verification (operator-token path stays for the console).
2. Add a thin SmokeStack client `feature_enabled(key, *, customer_ref, user_id, role, license_plan, environment)` that POSTs to `/api/internal/feature-check` and **fails closed** (deny) on timeout/error.
3. Cache decisions briefly (e.g. 30–60 s) keyed by the full context; respect a short TTL so flips propagate quickly.
4. Gate SmokeStack routes/menu items server-side on the returned `allow`; return HTTP 403 on deny — never hide in the frontend only.
5. Map SmokeStack roles and license plans to the `role_requirements` / `license_plan_requirements` vocabularies.
6. For owner tools, pass the active PFS `support_session_ref` and `actor_type=platform_owner`.

---

## 10. Verification checklist (all passing)

- [x] `ruff check` clean
- [x] 65/65 pytest pass (SQLite)
- [x] `alembic upgrade head → downgrade 0001_init → upgrade head` reversible
- [x] `node --check` on SPA JS
- [x] jsdom Features-page smoke test
- [x] SmokeStack untouched; not deployed to Production

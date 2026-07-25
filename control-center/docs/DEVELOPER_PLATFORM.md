# PFS Internal Development Platform — Developer Mode

Status: implemented in the PFS Control Center on branch `feature-flags`, building on the Feature Management milestone. **SmokeStack business logic is unchanged.** Not deployed to Production — validate on Development first.

Goal: let the Platform Owner build, preview, test, gradually release, and instantly roll back ERP functionality **without customers ever seeing unfinished work**.

---

## 1. Platform Owner Mode (server-enforced)

Developer Mode is gated by a new dependency, `require_owner`, which admits **only** an operator whose validated `platform_role == "owner"`. Everyone else gets **HTTP 403**; a customer (who carries no operator token at all) is stopped at **401** before that. Owner status is read only from the signed operator token — never from frontend state, a query parameter, a header, or any client value. The Developer Tools nav is merely *hidden* for non-owners; the security is entirely in the backend.

`GET /api/platform/dev/context` returns the developer context (owner identity, `feature_profile: platform_owner`, the environment list, the lifecycle stages, and the tool catalog). It is the server's confirmation that Developer Mode is available.

---

## 2. Developer Tools (owner-only, inside every ERP workspace)

A `Developer Tools` section appears in the ERP workspace sidebar for the owner only, containing: Feature Manager, Release Manager, Rollout Manager, Beta Features, Experimental Modules, System Diagnostics, Database Inspector, API Explorer, Integration Status, Background Jobs, Debug Console, Support Tools, AI Playground.

The flag-driven tools are fully functional on PFS metadata. The tools that need live ERP data expose all PFS-side metadata and clearly mark ERP-fed parts as integration-pending (two-plane architecture): Database Inspector returns table names, row **counts**, and column **names** only — it never issues `SELECT *` and never returns row data; System Diagnostics reports service health and record counts; API Explorer enumerates the route surface; Debug Console shows recent platform actions and feature-evaluation decisions; Integration Status and Background Jobs show platform state with ERP feeds marked pending; AI Playground is a labelled placeholder.

---

## 3. Feature lifecycle & release pipeline

`FeatureFlag` gained a `lifecycle_stage`: `development → internal_testing → staging → pilot → production → deprecated → removed`. Stage is **independent of targeting** — advancing a feature does not by itself expose it; visibility, license, role, allowlist, rollout and environment still govern access. `POST /api/platform/feature-flags/{id}/stage` moves a feature and audits `feature_promoted` / `feature_demoted`. Release Manager renders the pipeline as a board; Rollout Manager adjusts `rollout_percentage` for gradual exposure.

The safe-testing workflow needs **no redeploy** to expose a finished feature: create it owner-only → preview and test → widen to internal → pilot customer → flip to production/everyone, all by editing flag metadata.

---

## 4. Owner Preview + environments

`DevPreviewSession` backs the **Developer Mode Enabled** banner shown when the owner opens an ERP (Customer, Environment, Feature Profile: Platform Owner, with an environment switcher and Exit Session). Endpoints: `POST /api/platform/preview/start`, `POST /api/platform/preview/{id}/environment`, `POST /api/platform/preview/{id}/end` — audited as `preview_started`, `preview_environment_switched`, `preview_ended`.

Three environments (development / staging / production) are supported as an **evaluation/preview context**: switching changes what the owner previews and what `feature-check` evaluates against. It does not spin up separate ERP instances (none exist yet). **Hard rule, enforced server-side:** in `POST /api/internal/feature-check`, any non-elevated actor (i.e. a customer) is forced to `environment = production` regardless of what the client requests. Customers can never leave Production, and can never reach a development/staging-scoped feature.

---

## 5. Rollback

`POST /api/platform/feature-flags/{id}/rollback` restores a flag to the state captured **before its most recent change** (from the immutable audit trail) and records `feature_rolledback`. This is the "roll back instantly" path — one call reverts targeting, visibility, stage, schedule, and state together.

---

## 6. Audit

Every sensitive action is recorded with before/after/reason: `feature_created`, `feature_enabled`, `feature_disabled`, `feature_promoted`, `feature_demoted`, `allowlist_changed`, `feature_archived`, `feature_restored`, `feature_rolledback`, plus evaluation-time `owner_tool_opened`, `experimental_accessed`, `unauthorized_access_denied`, and the preview lifecycle (`preview_started`, `preview_environment_switched`, `preview_ended`) in the platform audit log.

---

## 7. Customer experience (guarantees)

Customers never see hidden features, Developer Tools, Beta modules, or internal routes; cannot call owner-only endpoints (401/403); cannot modify flags; and are always evaluated in Production. All of this is enforced by the backend, not by hiding in the UI.

---

## 8. Data-model & migration

`feature_flags.lifecycle_stage` (default `development`) and the new `dev_preview_sessions` table. Migration `0003_dev_platform` (`down_revision = 0002_feature_flags`) is additive and inspector-guarded (safe on fresh + existing DBs), and fully reversible — verified `upgrade head → downgrade 0002 → upgrade head` (15 → 14 → 15 tables; `lifecycle_stage` present/absent/present).

---

## 9. Tests & validation

Backend suite grew to **77 passing tests** (SQLite). New Developer-Platform coverage: `require_owner` denies internal/operator/customer with 403 and admits the owner; every `/api/platform/dev/*` and `/preview/*` route is 401 without a token; dev context reports tools/profile/environments/stages; the customer-always-forced-to-Production rule; preview start/switch/end are audited; promote/demote audited; invalid lifecycle rejected (422); rollback restores prior state; DB Inspector is metadata-only; API Explorer lists namespaced routes; diagnostics/debug/jobs/integrations are owner-only. Also: `ruff` clean; `node --check` on the SPA; a jsdom smoke test of Developer Mode (owner nav shown / hidden for non-owner, preview banner, release board, rollout slider, diagnostics, DB inspector, API explorer, debug console, tool dispatcher).

---

## 10. Rollout / rollback & SmokeStack

Ship on `feature-flags`; deploy to the Development Control Center; `alembic upgrade head` (additive, no downtime). Promote to Production only after sign-off. Code revert is sufficient to disable the feature; `alembic downgrade 0002_feature_flags` removes the new column/table if a schema rollback is needed. SmokeStack is untouched; the only future integration point is the already-documented `feature-check` contract (`docs/SMOKESTACK_FEATURE_INTEGRATION.md`).

# PFS Control Center — Milestone 1.1 (Accountant model)

The **Control Plane** of the PFS platform: the one place the Platform Owner signs in to run
**many ERP products**, each with **many customers**. Modelled after **QuickBooks Accountant +
App Store Connect + GitHub Organizations** — not an infrastructure dashboard. A **separate
service** with its **own backend, database, auth realm, CI and deployment** (ADR-021). It stores
**only platform metadata** — never a customer's ERP business data.

## Product model
```
PFS  →  ERP Product  →  Customers  →  Open ERP / Support Session
```
ERP workspace tabs: **Overview · Customers · Support Sessions · Versions · Updates · Licenses ·
Health · Audit · Settings**. `Runtime` remains a backend technical entity — it is *not* a
primary navigation destination.

## First-class objects
- **ERP Product** — an externally-built ERP, **registered** (not created) into PFS.
- **Customer** — a reference to a company running an ERP (the ERP owns the authoritative record).
- **License** — per-customer entitlement (plan, status, dates, seat/branch limits). Metadata only,
  no billing. Statuses: trial · active · suspended · expired · cancelled.
- **Support Session** ("Open ERP") — short-lived, capability-scoped, auditable, revocable grant.
  **Never uses a customer password.** ERP-side consumption is deferred, so a new session is
  `pending_erp_integration`: recorded + audited, and the registered ERP URL opens in a new tab.
- **Version** (Release) — immutable published build; only Master Production may publish (ADR-028).
- **Update** (Deployment) — assigning/rolling a version to customers; metadata-only this milestone.

## Deliberate Milestone-1.1 boundaries
No automatic deployments, no Master-runtime provisioning, no access to customer transactional data,
no ERP-side support-session consumption, no per-customer heartbeat (customer health is *inherited
from the runtime* and last-sync is explicitly *not yet integrated* — never fabricated), no
customer preview environments, no billing/marketplace/AI. It never modifies SmokeStack, TD-002,
PR #1, or starts B-C.

## Run locally
```
cd control-center
pip install -r requirements.txt -r requirements-dev.txt
export JWT_SECRET=dev SEED_PASSWORD=owner-dev SEED_EMAIL=owner@pfs.local
uvicorn main:app --reload            # UI at http://localhost:8000/  (sign in as OP-owner)
pytest -q                            # tests (SQLite)
alembic upgrade head                 # apply schema to $DATABASE_URL (Postgres in prod)
```

## Environment variables
| var | purpose |
|-----|---------|
| `DATABASE_URL` | isolated Postgres (Render-generated) |
| `JWT_SECRET` | operator-realm signing secret (its own; never an ERP's) |
| `ENVIRONMENT` | `production` \| `development` |
| `SEED_EMAIL` | Platform Owner email (dashboard secret) |
| `SEED_PASSWORD` | Platform Owner password (dashboard secret) |
| `CONTROL_CENTER_BASE_URL` | live service URL (set after first deploy) |
| `SUPPORT_SESSION_MINUTES` | default support-session lifetime (30) |

## Deploy (operator step — see runbook)
Provision `render.control.yaml` (New → Blueprint): its own Postgres `pfs-control-db` + web
service `pfs-control-center`, secrets set in the dashboard. `autoDeploy: false` so it never
deploys on unrelated pushes. Independent of every ERP's build/deploy (ADR-023).

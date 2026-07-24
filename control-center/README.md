# PFS Control Center — Milestone 1 (Foundation)

The **Control Plane** of the PFS platform: the central system the Platform Owner uses to model
and observe the fleet of ERP products. A **separate service** with its **own backend, database,
auth realm and deployment** (ADR-021). It stores **only fleet/platform metadata** — never a
customer's ERP business data.

## What this milestone does
- ERP Product + Master Environment registries (two-lane lifecycle, ADR-028).
- Runtime registry with **read-only** health polling of existing ERP runtimes.
- CustomerRef + CustomerDeployment metadata (the ERP still owns the authoritative customer record).
- Release registry — only **Master Production** may publish; the current SmokeStack prod build is
  registered once as an **Imported Legacy Release** (bootstrap exception).
- Registers the existing **SmokeStack production** as a Customer-Production runtime + **Company #1**
  reference + Imported Legacy Release (metadata only; SmokeStack is untouched).
- Platform audit trail; read-only owner dashboard.

## What it deliberately does NOT do (Milestone-1 boundaries)
No automatic deployments, no Master-runtime provisioning, no access to customer transactional data,
no ERP-side Enter-Session consumption, no customer preview environments, no engine extraction,
no billing/marketplace/AI. It never modifies SmokeStack, TD-002, PR #1, or starts B-C.

## Run locally
```
cd control-center
pip install -r requirements.txt -r requirements-dev.txt
export JWT_SECRET=dev SEED_PASSWORD=owner-dev
uvicorn main:app --reload            # dashboard at http://localhost:8000/  (sign in as OP-owner)
pytest -q                            # tests (SQLite)
alembic upgrade head                 # apply schema to $DATABASE_URL (Postgres in prod)
```

## Deploy (later operator step)
Provision `render.control.yaml` (New → Blueprint): its own Postgres + web service, `SEED_PASSWORD`
set in the dashboard. `autoDeploy: false` so it never deploys on unrelated pushes. Independent of
every ERP's build/deploy (ADR-023).

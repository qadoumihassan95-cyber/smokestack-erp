# SmokeStack ERP — Backend (FastAPI + PostgreSQL)

Production-grade backend that turns the browser-only ERP into a backend-driven
system: PostgreSQL persistence, secure JWT REST APIs, and the exact role +
branch permission model the UI already uses. Built to serve the web app, the
Telegram worker, the AI assistant, and future mobile apps from one API.

## Run locally
```bash
cd backend
pip install -r requirements.txt
uvicorn app.main:app --reload         # uses SQLite by default (./smokestack.db)
# open http://127.0.0.1:8000/docs      (interactive OpenAPI)
pytest -q                             # 11 test groups, all endpoints
```
Seeded users (password `demo1234`): `U-owner U-admin U-bm U-inv U-acct U-cash U-emp`.

## Deploy to Render (API + Postgres)
`render.yaml` is a Blueprint that provisions a **managed PostgreSQL** and a
**Python web service** for the API (separate from your static web app).
1. *New → Blueprint* → your repo → it creates `smokestack-db` + `smokestack-api`.
2. `DATABASE_URL` is injected from the database; `JWT_SECRET` is auto-generated.
3. First boot runs `create_all` + seeds. Set `SEED_ON_START=false` afterwards.
4. Point the frontend at the API: `SS_API.base = 'https://smokestack-api.onrender.com'`
   and set `CORS_ORIGINS` to your web app's URL.

*(Provisioning the Render database and clicking deploy are actions in your Render
account — the code is ready; those steps are yours.)*

## Modules & endpoints (32)
- **auth**: `POST /api/auth/login`, `GET /api/auth/me`
- **core/reports**: `GET /api/branches`, `/api/reports/dashboard`, `/api/reports/daily`, `/api/audit`
- **inventory**: products (list/search/create), `/barcode/{code}`, `receive`, `adjust`, `movements`, **`asof`** (ledger-based history)
- **ledger**: sales, expenses, purchases (list/create; purchases raise approvals)
- **hr**: employees (list/create/deactivate), `payroll`, `payroll/finalize`
- **partners**: customers, suppliers (+ statements)
- **workflow**: transfers, approvals (approve/reject), clock in/out
- **telegram**: `link/issue`, `link/verify`, `session/{tg_id}` (for the bot worker)

## Security
JWT bearer auth (`python-jose`), bcrypt password hashing (`passlib`), a
`require(*perms)` dependency on every protected route, per-branch scoping
(`assert_branch`), failed-login + full action **audit logging** to `audit_log`,
and CORS locked to configured origins.

## Scale
Postgres with pooled connections (`pool_size=10, max_overflow=20, pool_pre_ping`),
indexes on hot paths (movements by sku/branch/time, ledger by branch/date,
barcode, audit by time). The immutable `movements` ledger makes history + as-of
correct and cache-friendly at millions of rows (add daily snapshots later if a
single branch's ledger gets very deep — the query already uses the newest row
≤ date, so it stays fast with the composite index).

## Ready for
- **Telegram**: the worker's `postgres` adapter + `/api/telegram/*` linking endpoints.
- **AI assistant**: pure JSON + OpenAPI at `/docs` → easy tool/function calling.
- **Mobile**: stateless JWT + CORS → native iOS/Android clients hit the same API.

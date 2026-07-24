# PFS Engineering Pipeline

This is the reference for how PFS is built, tested, validated, deployed, and recovered.
It is the document `.github/workflows/ci.yml` points to when it says advisories are "tracked
here with a remediation plan." It complements the four constitutional documents:
**Architecture v1.0**, **Governance v1.0**, **Vision & Roadmap v1.0**, and the
**Engineering Readiness Report**.

> Scope: the backend service (`backend/`) and its Telegram worker. Nothing here changes
> product behavior — it governs *how* changes reach production safely.

---

## 1. Continuous Integration (`.github/workflows/ci.yml`)

Every push and pull request to `main` runs a **fail-fast** pipeline. All required jobs must
pass the single gate check **`ci-passed`** before a change is considered mergeable/deployable.

| Job | What it does | Why it exists |
|-----|--------------|---------------|
| **lint-static** | `ruff check` (bug rules, blocking) + `ruff format --check` (advisory) + `bandit -ll` (medium+ security, blocking) | Catch real defects and insecure patterns before merge |
| **deps-audit** | `pip-audit -r requirements.txt --strict` with a tracked ignore-list (§4) | Fail on *new* dependency vulnerabilities; track known ones with a remediation plan |
| **unit-tests** | Full `pytest` suite on SQLite | Fast, broad behavioral coverage on every push |
| **migration-pg** | On a **real PostgreSQL 16** service container: `alembic upgrade head` (mirrors prod preDeploy), assert exactly one head, run tenant-isolation / raw-SQL-scoping / fail-closed integration tests | Exercise Postgres-specific migration + isolation behavior SQLite can't reproduce |
| **build-artifact** | Install, import-smoke (`import app.main`), and package a versioned source tarball | Prove the app builds and boots without DB side effects; produce a deployable artifact |
| **ci-passed** | Gate job; `needs` all five above | The single status check to protect the branch / gate deploys on |

**Concurrency.** Runs are grouped per ref with `cancel-in-progress: true`, so a newer push
supersedes an in-flight run (older runs show as *cancelled*, which is expected).

**Determinism.** The suite is order-independent: test modules set their own temp DB, use
distinct tenant ids to avoid shared-engine pollution, and dispose pooled connections between
modules (`tests/conftest.py`). `pythonpath = ["."]` in `pyproject.toml` makes `import app...`
resolve in any environment (local shell, CI runner, fresh checkout).

---

## 2. Dependencies: runtime vs. test

- **`requirements.txt`** — the production web runtime only. Keep it minimal.
- **`requirements-dev.txt`** — test/CI-only dependencies. It pulls `pytest` and, because the
  backend test suite imports the Telegram worker (`tests/test_tg_confirm.py`), the worker's own
  pin file via `-r telegram_worker/requirements.txt` (single source of truth, no drift). This is
  also what supplies `httpx`, required by `starlette.testclient.TestClient`.

**Rule:** never add a test-only package to `requirements.txt`. New runtime dependencies require
a need, a license check, and a clean `pip-audit`.

---

## 3. Local reproduction (`backend/Makefile`)

Contributors reproduce the CI gate locally before pushing:

```
make install     # runtime + dev dependencies
make lint        # ruff check (blocking) + ruff format --check (advisory)
make security    # bandit medium+
make audit       # pip-audit with the tracked ignore-list (mirrors CI exactly)
make test        # full pytest suite (SQLite)
make heads       # assert exactly one Alembic head
make migrate     # alembic upgrade head against $DATABASE_URL
make ci          # lint + security + audit + test + heads  (the local gate)
```

Reproducing `make ci` green locally should mean a green pipeline on push.

---

## 4. Tracked dependency advisories (remediation plan)

`deps-audit` fails on any advisory **not** on the ignore-list, so the list can only shrink. Each
entry has an owner action; the list mirrors the `--ignore-vuln` flags in `ci.yml`.

| Advisory | Package | Plan | Register ref |
|----------|---------|------|--------------|
| PYSEC-2024-232, PYSEC-2024-233 | `python-jose` | **Upgrade to 3.4.0** (clears both); revalidate JWT auth on staging first | Governance TD-012 (P1) |
| PYSEC-2025-185 / 2026-* (starlette, ecdsa, transitive) | fastapi/jose deps | Bump `fastapi`/`starlette`; drop `ecdsa` exposure with the jose upgrade | Governance TD-012 (P1) |

**Policy:** adding a `--ignore-vuln` requires a row here and a Debt-Register entry with a revisit
trigger. Removing dependencies from the ignore-list (by fixing them) is always welcome.

> The jose/starlette bump is **P1** and touches the authentication runtime — it is a
> **stop-condition** change under Governance and must be rehearsed on staging + explicitly
> approved before it ships. It is *not* done silently in a routine PR.

---

## 5. Staging (`backend/render.staging.yaml`)

A Render Blueprint mirrors production exactly (PostgreSQL 16, same build / `preDeploy alembic
upgrade head` / start / health flow, same worker), with an isolated DB, seed enabled, and
distinct names/URLs. **Policy: every migration runs on staging first.** Provisioning is a
one-time operator action (New → Blueprint) and requires a **staging** Telegram bot token — never
production's. Until provisioned, "staging-first" is policy, not an enforced gate (Governance
TD-013, P1).

---

## 6. Deployment & rollback (`backend/scripts/deploy_safety.py`, `.github/workflows/deploy.yml`)

**Model:** migrate-then-serve. Render `preDeploy` runs `alembic upgrade head`; the app only
serves if migrations succeed and `/api/health` reports `status ok`, `database ok`, and the
expected build SHA.

`deploy_safety.py` is one reusable tool with three subcommands:

- **`preflight`** — fails unless there is exactly one Alembic head and every migration defines a
  real `downgrade()`; prints the backup / PITR / staging-rehearsal / CI checklist.
- **`verify <url> [sha]`** — post-deploy: health 200, `status==ok`, `database==ok`, build matches;
  non-zero exit = rollback signal.
- **`report <url>`** — emits a markdown deployment report to stdout / `$GITHUB_STEP_SUMMARY`.

`deploy.yml` is a **gated, manual-dispatch** workflow: preflight → Render deploy hook → wait →
verify → report. It is inert until `RENDER_DEPLOY_HOOK_*` / `*_URL` secrets are set; recommended
companion setting is Render "Auto-Deploy: after CI checks pass."

**Rollback:** every migration ships a real, rehearsed `downgrade()` (up→down→up on staging).
Expand-and-contract guarantees a coexistence state so a bad *expand* rolls back without data
loss; a failed post-deploy verify is treated as a rollback signal; a fresh restorable backup +
confirmed PITR window are preflight prerequisites.

---

## 7. Adding to the pipeline (checklist)

- New required job? Add it to `ci-passed.needs` and document it in §1.
- New runtime dep? `requirements.txt` + license + `pip-audit` clean. Test-only? `requirements-dev.txt`.
- New advisory surfaced? Fix it, or add a `--ignore-vuln` **and** a §4 row + Debt-Register entry.
- New migration? Expand-and-contract, single head, real `downgrade()`, staging-first (Governance §2.7 / §5.1).
- Turning `ruff format` from advisory to blocking is a one-time formatted-baseline PR (Governance TD-014).

"""Phase-1 security remediation — regression suite.

Pins the SECURE behavior for every audited finding and the full RBAC / branch /
field-visibility / session-revocation / throttling / seeding matrix. Each test
asserts the fixed contract; the companion reproduction cases (which asserted the
vulnerable behavior) were removed once these passed.

Findings covered:
  SS-H-001  explicit ?branch= can never bypass the caller's allowed branches
  SS-H-006  object-level branch/tenant authorization on ID routes (IDOR/BOLA)
  SS-H-002  cost/COGS/profit/margin/payroll/valuation omitted from unentitled roles
  SS-H-011  one password policy for every path + JWT/session revocation
  SS-H-007  DB-backed throttling on login + Telegram link verification
  SS-C-003  production manifests never seed default-credential accounts
"""
import os
import re
import tempfile

_DB = os.path.join(tempfile.gettempdir(), f"ss_phase1_{os.getpid()}.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["SEED_ON_START"] = "true"
os.environ["JWT_SECRET"] = "phase1-security-secret-long-enough"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402
from app import permissions as P  # noqa: E402
from app.config import settings, _bool  # noqa: E402

client = TestClient(app)


@pytest.fixture(scope="module", autouse=True)
def _boot():
    with TestClient(app):
        yield


# A distinct client IP per call keeps normal auth out of the throttle counters;
# the throttling tests pin their own fixed IP to trip the limiter deliberately.
_ipn = [0]


def _login(uid, pw, ip=None):
    _ipn[0] += 1
    xff = ip or f"10.1.{_ipn[0] // 250 % 250}.{_ipn[0] % 250}"
    return client.post("/api/auth/login", data={"username": uid, "password": pw},
                       headers={"X-Forwarded-For": xff})


def _tok(uid, pw="demo1234"):
    r = _login(uid, pw)
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["access_token"]}


def _mk(name, role="cashier", branches=("Store A",), pw="Str0ng#Pass1"):
    body = {"name": name, "role": role, "branches": list(branches),
            "password_mode": "manual", "password": pw, "confirm_password": pw,
            "must_change_password": False}
    r = client.post("/api/users", headers=_tok("U-owner"), json=body)
    assert r.status_code == 201, r.text
    return r.json()["username"], pw


# ============================================================ A · SS-H-001
def test_A_cashier_explicit_foreign_branch_is_forbidden_not_swapped():
    h = _tok("U-cash")                                  # scoped to Store A only
    assert client.get("/api/sales?branch=Store B", headers=h).status_code == 403
    assert client.get("/api/inventory/products?branch=Store B", headers=h).status_code == 403
    assert client.get("/api/employees?branch=Store B", headers=h).status_code == 403
    assert client.get("/api/expenses?branch=Store C", headers=h).status_code == 403


def test_A_cashier_own_branch_and_all_are_allowed():
    h = _tok("U-cash")
    assert client.get("/api/sales?branch=Store A", headers=h).status_code == 200
    assert client.get("/api/sales?branch=all", headers=h).status_code == 200
    # empty / whitespace resolve to the caller's own allowance, never a foreign branch
    assert client.get("/api/sales?branch=", headers=h).status_code == 200


def test_A_unknown_or_malformed_branch_fails_closed():
    h = _tok("U-cash")
    for bad in ("Store%20Z", "Nope", "Store A;DROP", "store a"):
        assert client.get(f"/api/sales?branch={bad}", headers=h).status_code == 403, bad


def test_A_branch_manager_scope_boundary():
    h = _tok("U-bm")                                    # Store A + Store B
    assert client.get("/api/sales?branch=Store B", headers=h).status_code == 200
    assert client.get("/api/sales?branch=Store C", headers=h).status_code == 403


def test_A_global_roles_reach_every_branch():
    for uid in ("U-owner", "U-admin", "U-acct"):
        h = _tok(uid)
        assert client.get("/api/sales?branch=Store C", headers=h).status_code == 200, uid


# ============================================================ B · SS-H-006 (IDOR/BOLA)
def test_B_manager_cannot_edit_employee_in_unheld_branch():
    h = _tok("U-bm")                                    # holds A+B, NOT C
    # EMP-1003 lives in Store C — editing it must 403 even though the manager
    # holds edit_employee (branch scope, not permission, is the gate).
    r = client.put("/api/employees/EMP-1003", headers=h, json={"title": "Hijacked"})
    assert r.status_code == 403


def test_B_replacement_branch_cannot_launder_authorization():
    """The historic hole: supplying a branch the caller DOES hold used to satisfy
    the check even when the record lived in a branch they do not hold."""
    h = _tok("U-bm")
    r = client.put("/api/employees/EMP-1003", headers=h,
                   json={"branch": "Store A", "title": "Pulled"})
    assert r.status_code == 403                          # current branch (C) still enforced


def test_B_manager_can_edit_in_scope_employee():
    h = _tok("U-bm")
    r = client.put("/api/employees/EMP-1001", headers=h, json={"title": "Lead"})
    assert r.status_code == 200 and r.json()["title"] == "Lead"


def test_B_move_to_unheld_destination_is_forbidden():
    h = _tok("U-bm")                                     # EMP-1001 is Store A (in scope)
    r = client.put("/api/employees/EMP-1001", headers=h, json={"branch": "Store C"})
    assert r.status_code == 403                          # destination Store C not held


# ============================================================ C · SS-H-002 (field visibility)
def test_C_cashier_dashboard_omits_sensitive_totals():
    d = client.get("/api/reports/dashboard?branch=Store A", headers=_tok("U-cash")).json()
    for leaked in ("profit_today", "cogs_today", "payroll_today", "costs_today",
                   "inventory_cost", "potential_profit"):
        assert leaked not in d, leaked
    assert "sales_today" in d and "expenses_today" in d   # transaction totals stay


def test_C_owner_dashboard_includes_sensitive_totals():
    d = client.get("/api/reports/dashboard?branch=Store A", headers=_tok("U-owner")).json()
    for f in ("profit_today", "cogs_today", "payroll_today", "inventory_cost"):
        assert f in d, f


def test_C_cashier_employees_have_no_salary():
    rows = client.get("/api/employees?branch=Store A", headers=_tok("U-cash")).json()
    assert rows and all("salary" not in e for e in rows)
    owner_rows = client.get("/api/employees?branch=Store A", headers=_tok("U-owner")).json()
    assert any("salary" in e for e in owner_rows)


def test_C_cashier_products_have_no_cost_but_keep_price():
    rows = client.get("/api/inventory/products?branch=Store A", headers=_tok("U-cash")).json()
    assert rows and all("cost" not in p for p in rows)
    assert all("price" in p for p in rows)                # retail price is not sensitive
    owner_rows = client.get("/api/inventory/products?branch=Store A", headers=_tok("U-owner")).json()
    assert any("cost" in p for p in owner_rows)


def test_C_cashier_analytics_omits_profit_and_cost_series():
    a = client.get("/api/reports/analytics?branch=Store A", headers=_tok("U-cash")).json()
    assert "profit_trend" not in a and "costs_trend" not in a
    assert all("profit" not in b for b in a.get("branch_comparison", []))


def test_C_no_misleading_zero_values_are_substituted():
    d = client.get("/api/reports/dashboard?branch=Store A", headers=_tok("U-cash")).json()
    assert "profit_today" not in d                        # omitted, not set to 0


# ============================================================ D · SS-H-011 (password policy)
def test_D_change_password_rejects_weak_and_short():
    un, pw = _mk("Pw Weak")
    h = _tok(un, pw)
    for bad in ("aaaaaaaaaa", "short", "password123"):
        r = client.post("/api/auth/change-password", headers=h,
                        json={"current_password": pw, "new_password": bad})
        assert r.status_code == 422, bad


def test_D_change_password_oversized_is_422_not_500():
    un, pw = _mk("Pw Long")
    h = _tok(un, pw)
    r = client.post("/api/auth/change-password", headers=h,
                    json={"current_password": pw, "new_password": "A9b!" + "x" * 100})
    assert r.status_code == 422


def test_D_change_password_confirm_mismatch_rejected():
    un, pw = _mk("Pw Confirm")
    h = _tok(un, pw)
    r = client.post("/api/auth/change-password", headers=h,
                    json={"current_password": pw, "new_password": "Grand#Slam99",
                          "confirm_password": "Grand#Slam98"})
    assert r.status_code == 422


def test_D_change_password_same_as_current_rejected():
    un, pw = _mk("Pw Same")
    h = _tok(un, pw)
    r = client.post("/api/auth/change-password", headers=h,
                    json={"current_password": pw, "new_password": pw})
    assert r.status_code == 422


def test_D_owner_manual_reset_enforces_policy():
    un, _ = _mk("Pw Reset")
    r = client.post(f"/api/users/{un}/reset-password", headers=_tok("U-owner"),
                    json={"password_mode": "manual", "password": "weak",
                          "confirm_password": "weak"})
    assert r.status_code == 422


def test_D_strong_password_accepted_and_never_echoed():
    un, pw = _mk("Pw Good")
    h = _tok(un, pw)
    r = client.post("/api/auth/change-password", headers=h,
                    json={"current_password": pw, "new_password": "Mighty#Oak2026"})
    assert r.status_code == 200
    assert "Mighty#Oak2026" not in r.text and "password_hash" not in r.text


# ============================================================ E · SS-H-011 (session revocation)
def test_E_old_jwt_invalid_after_password_change():
    un, pw = _mk("Rev Change")
    old = _tok(un, pw)
    ch = client.post("/api/auth/change-password", headers=old,
                     json={"current_password": pw, "new_password": "Rotate#Me2026"})
    assert ch.status_code == 200
    new = {"Authorization": "Bearer " + ch.json()["access_token"]}
    assert client.get("/api/auth/me", headers=old).status_code == 401   # old revoked
    assert client.get("/api/auth/me", headers=new).status_code == 200   # rotated session works


def test_E_logout_revokes_current_session():
    un, pw = _mk("Rev Logout")
    h = _tok(un, pw)
    assert client.post("/api/auth/logout", headers=h).status_code == 200
    assert client.get("/api/auth/me", headers=h).status_code == 401


def test_E_owner_reset_revokes_targets_sessions():
    un, pw = _mk("Rev Reset")
    h = _tok(un, pw)
    assert client.post(f"/api/users/{un}/reset-password", headers=_tok("U-owner"),
                       json={}).status_code == 200
    assert client.get("/api/auth/me", headers=h).status_code == 401


def test_E_deactivate_revokes_targets_sessions():
    un, pw = _mk("Rev Deact")
    h = _tok(un, pw)
    assert client.post(f"/api/users/{un}/deactivate", headers=_tok("U-owner")).status_code == 200
    assert client.get("/api/auth/me", headers=h).status_code == 401


def test_E_role_or_branch_change_revokes_old_session():
    un, pw = _mk("Rev Scope")
    h = _tok(un, pw)
    assert client.put(f"/api/users/{un}", headers=_tok("U-owner"),
                      json={"branches": ["Store B"]}).status_code == 200
    assert client.get("/api/auth/me", headers=h).status_code == 401


# ============================================================ F · SS-H-007 (throttling)
def test_F_repeated_bad_logins_are_throttled():
    ip = "203.0.113.7"
    saw_429 = False
    for _ in range(14):
        r = _login("does-not-exist", "wrong", ip=ip)
        assert r.status_code in (401, 429)
        if r.status_code == 429:
            saw_429 = True
            break
    assert saw_429, "login was never throttled"


def test_F_throttle_does_not_disclose_account_existence():
    # A real and a fake identifier hammered from the same fresh IP must return the
    # same status, so throttling never reveals which usernames exist. A DISPOSABLE
    # real account is used so no shared seed login gets locked for later tests.
    real, _ = _mk("Throttle Real")
    ip = "203.0.113.9"
    last_real = last_fake = None
    for _ in range(14):
        last_real = _login(real, "wrong", ip=ip).status_code
        last_fake = _login("ghost-user", "wrong", ip=ip).status_code
    assert last_real == last_fake == 429


def test_F_telegram_link_verify_is_throttled():
    ip = "203.0.113.20"
    saw_429 = False
    for i in range(12):
        r = client.post("/api/telegram/link/verify",
                        headers={"X-Forwarded-For": ip},
                        json={"code": f"{i:06d}", "tg_id": "9999"})
        assert r.status_code in (400, 409, 429)
        if r.status_code == 429:
            saw_429 = True
            break
    assert saw_429, "telegram link verify was never throttled"


# ============================================================ G · SS-C-003 (seeding safety)
def _manifest(path):
    root = os.path.join(os.path.dirname(__file__), "..", "..")
    with open(os.path.join(root, path), encoding="utf-8") as f:
        return f.read()


def _seed_value_for_service(text, service_name):
    """Return the SEED_ON_START value for a named service block in a render.yaml."""
    idx = text.find(f"name: {service_name}\n")
    assert idx != -1, f"service {service_name} not found"
    block = text[idx:]
    nxt = block.find("\n  - ", 1)          # next service entry
    block = block[:nxt] if nxt != -1 else block
    m = re.search(r'SEED_ON_START\s*\n\s*value:\s*"(\w+)"', block)
    return m.group(1) if m else None


def test_G_production_manifests_never_seed():
    assert _seed_value_for_service(_manifest("render.yaml"), "smokestack-api") == "false"
    assert _seed_value_for_service(_manifest("backend/render.yaml"), "smokestack-api") == "false"


def test_G_prod_db_defaults_seeding_off():
    # An unset SEED_ON_START on a real (non-SQLite) store must resolve to False.
    assert _bool(None, "postgresql+psycopg2://x/y".startswith("sqlite")) is False
    assert _bool(None, "sqlite:///x".startswith("sqlite")) is True


def test_G_production_secret_guard_flags_defaults():
    # On SQLite (this test env) there are no problems...
    assert settings.production_secret_problems() == []
    # ...but a simulated production store with default secrets is rejected.
    class _Prod:
        database_url = "postgresql+psycopg2://u:p@h/db"
        jwt_secret = settings._DEFAULT_JWT_SECRET
        seed_on_start = True
        seed_password = settings._DEFAULT_SEED_PASSWORD
        _DEFAULT_JWT_SECRET = settings._DEFAULT_JWT_SECRET
        _DEFAULT_SEED_PASSWORD = settings._DEFAULT_SEED_PASSWORD
        is_sqlite = False
        production_secret_problems = settings.__class__.production_secret_problems
    problems = _Prod.production_secret_problems(_Prod)
    assert len(problems) == 2


def test_G_empty_store_creates_no_demo_accounts():
    # Seeding is the ONLY source of U-* accounts; the startup gate wraps it in
    # `if settings.seed_on_start`. Prove the gate exists and that an empty store
    # (seed not run) has none of the default identities.
    from app import models
    main_src = _manifest(os.path.join("backend", "app", "main.py"))
    assert "if settings.seed_on_start:" in main_src
    # A throwaway empty engine with tables but no seed has zero users.
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    empty = os.path.join(tempfile.gettempdir(), f"ss_empty_{os.getpid()}.db")
    if os.path.exists(empty):
        os.remove(empty)
    eng = create_engine(f"sqlite:///{empty}")
    models.Base.metadata.create_all(bind=eng)
    S = sessionmaker(bind=eng)()
    try:
        assert S.query(models.User).count() == 0
    finally:
        S.close()


# ============================================================ RBAC matrix extras
def test_RBAC_user_management_is_owner_only():
    for uid in ("U-admin", "U-acct", "U-bm", "U-inv", "U-cash", "U-emp"):
        assert client.get("/api/users", headers=_tok(uid)).status_code == 403, uid
    assert client.get("/api/users", headers=_tok("U-owner")).status_code == 200


def test_RBAC_non_owner_cannot_self_escalate():
    # No manage_users permission → cannot touch the user store at all, including self.
    assert client.put("/api/users/U-cash", headers=_tok("U-cash"),
                      json={"role": "owner"}).status_code == 403


def test_RBAC_permission_map_keeps_cashier_minimal():
    perms = set(P.PERMS["cashier"])
    for forbidden in ("manage_users", "manage_permissions", "view_cost", "view_profit",
                      "view_payroll", "view_all_branches", "run_payroll"):
        assert forbidden not in perms, forbidden

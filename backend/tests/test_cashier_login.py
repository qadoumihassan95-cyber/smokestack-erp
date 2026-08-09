"""Cashier (non-Owner) login + post-login authorization boundaries.

Regression for the production bug where an active cashier could not sign in and saw
"Not permitted (server rejected this action)". Root cause: login SUCCEEDS (200), but
the frontend boot fetched GET /api/inventory/movements which requires
`view_inventory_history` — a permission cashiers correctly lack — and the client
turned that post-login 403 into a misleading login error.

These tests pin the backend contract the fix relies on WITHOUT weakening RBAC or
branch scoping: a cashier authenticates and can read every boot dataset it is
entitled to, `movements` is the only intentional 403, Owner-only endpoints stay
denied, and branch scope is enforced.
"""
import os
import json
import tempfile

_DB = os.path.join(tempfile.gettempdir(), f"smokestack_cashier_{os.getpid()}.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["SEED_ON_START"] = "true"
os.environ["JWT_SECRET"] = "cashier-login-secret-long-enough"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402
from app import permissions as P  # noqa: E402

client = TestClient(app)

# The exact sequence the frontend boot (hydrate) reads, in order.
BOOT_VIEW_ENDPOINTS = [
    "/api/branches",
    "/api/inventory/products?branch=all",
    "/api/sales?branch=all",
    "/api/expenses?branch=all",
    "/api/purchases?branch=all",
    "/api/employees?branch=all",
]


@pytest.fixture(scope="module", autouse=True)
def _boot():
    with TestClient(app):
        yield


def _login(uid, pw):
    return client.post("/api/auth/login", data={"username": uid, "password": pw})


def _tok(uid="U-owner", pw="demo1234"):
    r = _login(uid, pw)
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["access_token"]}


def _create(name, role="cashier", branches=("Store A",), mode="generated", pw=None, must_change=None):
    body = {"name": name, "role": role, "branches": list(branches), "password_mode": mode}
    if mode == "manual":
        body.update(password=pw, confirm_password=pw)
        if must_change is not None:
            body["must_change_password"] = must_change
    r = client.post("/api/users", headers=_tok(), json=body)
    assert r.status_code == 201, r.text
    return r.json()


# --------------------------------------------------------------- baseline auth
def test_active_owner_login_succeeds():
    r = _login("U-owner", "demo1234")
    assert r.status_code == 200 and r.json()["user"]["role"] == "owner"


def test_invalid_password_rejected():
    assert _login("U-cash", "wrong-password").status_code == 401


# --------------------------------------------------------------- the core regression
def test_active_cashier_logs_in_and_can_read_every_boot_dataset():
    r = _login("U-cash", "demo1234")
    assert r.status_code == 200, r.text
    assert r.json()["user"]["role"] == "cashier"
    assert "password_hash" not in r.text
    h = {"Authorization": "Bearer " + r.json()["access_token"]}
    # Every dataset the boot needs and the cashier is entitled to must be readable.
    for ep in BOOT_VIEW_ENDPOINTS:
        assert client.get(ep, headers=h).status_code == 200, ep


def test_cashier_movements_is_the_only_intentional_boot_403():
    """This is the endpoint that broke login. It MUST stay 403 (RBAC intact); the
    fix is that the client tolerates it, not that the cashier gains the permission."""
    h = _tok("U-cash", "demo1234")
    r = client.get("/api/inventory/movements?branch=all", headers=h)
    assert r.status_code == 403
    # safe, generic-ish error; never leaks secrets
    body = r.text
    assert "view_inventory_history" in body            # names the missing perm, not a secret
    assert "password" not in body and "token" not in body.lower()


# --------------------------------------------------------------- forced-password change
def test_cashier_forced_password_change_flow():
    c = _create("Cash Forced")                          # generated → must_change=True
    un, tmp = c["username"], c["temp_password"]
    li = _login(un, tmp)
    assert li.status_code == 200 and li.json()["must_change_password"] is True
    th = {"Authorization": "Bearer " + li.json()["access_token"]}
    # locked to change-password only: a normal boot call is blocked until the change
    assert client.get("/api/branches", headers=th).status_code == 403
    assert client.get("/api/auth/me", headers=th).status_code == 200
    ch = client.post("/api/auth/change-password", headers=th,
                     json={"new_password": "Cashier#Strong2026"})
    assert ch.status_code == 200 and ch.json()["must_change_password"] is False
    # after the change: normal session, boot datasets readable, movements still 403
    fresh = _login(un, "Cashier#Strong2026")
    assert fresh.status_code == 200 and fresh.json()["must_change_password"] is False
    fh = {"Authorization": "Bearer " + fresh.json()["access_token"]}
    for ep in BOOT_VIEW_ENDPOINTS:
        assert client.get(ep, headers=fh).status_code == 200, ep
    assert client.get("/api/inventory/movements?branch=all", headers=fh).status_code == 403


# --------------------------------------------------------------- RBAC boundaries
def test_cashier_blocked_from_owner_only_endpoints():
    h = _tok("U-cash", "demo1234")
    assert client.get("/api/users", headers=h).status_code == 403          # User Management
    assert client.post("/api/users", headers=h,
                       json={"name": "X", "role": "employee"}).status_code == 403


def test_cashier_never_holds_privileged_permissions():
    perms = set(P.PERMS["cashier"])
    for forbidden in ("manage_users", "manage_permissions", "view_cost", "view_profit",
                      "view_payroll", "view_all_branches", "view_inventory_history",
                      "manage_branches", "run_payroll"):
        assert forbidden not in perms, forbidden


def test_cashier_cannot_act_on_unassigned_branch():
    h = _tok("U-cash", "demo1234")                       # U-cash is scoped to Store A
    denied = client.post("/api/expenses", headers=h,
                         json={"branch": "Store B", "category": "Rent", "amount": 10})
    assert denied.status_code == 403                      # assert_branch blocks Store B
    ok = client.post("/api/expenses", headers=h,
                     json={"branch": "Store A", "category": "Rent", "amount": 10})
    assert ok.status_code == 201                          # its own branch is allowed


# --------------------------------------------------------------- safe failures
def test_inactive_cashier_is_blocked():
    c = _create("Cash Inactive", mode="manual", pw="Cashier#Strong2026", must_change=False)
    un = c["username"]
    assert client.post(f"/api/users/{un}/deactivate", headers=_tok()).status_code == 200
    r = _login(un, "Cashier#Strong2026")
    assert r.status_code == 403 and "not active" in r.text.lower()


def test_cashier_without_any_branch_fails_safely_not_500():
    c = _create("Cash NoBranch", branches=(), mode="manual",
                pw="Cashier#Strong2026", must_change=False)
    un = c["username"]
    li = _login(un, "Cashier#Strong2026")
    assert li.status_code == 200                          # login itself does not depend on branches
    h = {"Authorization": "Bearer " + li.json()["access_token"]}
    # boot-time reads never 500 for a branchless account — they resolve safely
    for ep in ("/api/branches", "/api/inventory/products?branch=all", "/api/sales?branch=all"):
        r = client.get(ep, headers=h)
        assert r.status_code == 200, ep
        assert isinstance(r.json(), list)
    # and RBAC still holds: no privileged data, no Owner-only access
    assert client.get("/api/inventory/movements?branch=all", headers=h).status_code == 403
    assert client.get("/api/users", headers=h).status_code == 403

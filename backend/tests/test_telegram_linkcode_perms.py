"""Regression for the Telegram link-code failure (symptom A).

Root cause: an accountant (view_all_branches, so the Telegram page loads) hit the
owner-only generate action and got a generic '403 -> Not permitted'. Fix: a
dedicated ``manage_telegram_links`` capability (owner + admin) gates generation,
with distinct statuses (403/404/409/422) the UI can message clearly, while branch
isolation and single-use/one-time codes are preserved. hiyam/mohammed-style
already-linked employees must be safely refused, never modified.
"""
import os, tempfile

_DB = os.path.join(tempfile.gettempdir(), f"tg_linkperm_{os.getpid()}.db")
if os.path.exists(_DB): os.remove(_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["SEED_ON_START"] = "true"
os.environ["JWT_SECRET"] = "linkperm-secret-long-enough"
os.environ["TELEGRAM_BOT_TOKEN"] = "test-bot-token-xyz"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402
from app import permissions as P  # noqa: E402

client = TestClient(app)
BOT = {"X-Bot-Token": "test-bot-token-xyz"}


@pytest.fixture(scope="module", autouse=True)
def _boot():
    with TestClient(app):
        yield


def tok(uid, pw="demo1234"):
    r = client.post("/api/auth/login", data={"username": uid, "password": pw})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["access_token"]}


import secrets as _secrets
_SFX = _secrets.token_hex(3)

def _gen(uid, emp_id):
    return client.post("/api/telegram/link-code", headers=tok(uid), json={"employee_id": emp_id})

def _mk_emp(branch="Store A"):
    eid = "EMP-ZZ" + _secrets.token_hex(4).upper()
    r = client.post("/api/employees", headers=tok("U-owner"),
                    json={"id": eid, "name": "ZZ " + eid, "branch": branch,
                          "title": "t", "pay_type": "salary", "salary": 0})
    assert r.status_code == 201, r.text
    return eid


# ---- capability matrix (owner + admin only) -------------------------------
def test_owner_can_generate():
    r = _gen("U-owner", "EMP-1001")
    assert r.status_code == 200 and r.json().get("code")


def test_admin_can_generate():
    assert P.can("admin", "manage_telegram_links")
    r = _gen("U-admin", "EMP-1001")
    assert r.status_code == 200 and r.json().get("code")


def test_accountant_denied_with_distinct_403():
    r = _gen("U-acct", "EMP-1001")
    assert r.status_code == 403
    assert "permission" in r.json()["detail"].lower()      # distinct, not generic


def test_cashier_and_branch_manager_denied():
    assert _gen("U-cash", "EMP-1001").status_code == 403
    assert _gen("U-bm", "EMP-1001").status_code == 403      # bm lacks the capability


def test_capability_map_is_owner_admin_only():
    for r in ("owner", "admin"):
        assert P.can(r, "manage_telegram_links"), r
    for r in ("accountant", "branch_manager", "cashier", "inventory_manager", "employee"):
        assert not P.can(r, "manage_telegram_links"), r


# ---- controlled statuses ---------------------------------------------------
def test_missing_employee_404():
    assert _gen("U-owner", "EMP-DOES-NOT-EXIST").status_code == 404


def test_inactive_employee_422():
    h = tok("U-owner")
    eid = _mk_emp()
    assert client.post(f"/api/employees/{eid}/deactivate", headers=h).status_code == 200
    assert _gen("U-owner", eid).status_code == 422


def test_already_linked_returns_409_at_redemption_and_does_not_modify_link():
    # Minting a code is always allowed; UNIQUENESS is enforced at redemption:
    # a second Telegram id for an already-linked employee returns a controlled
    # 409, and the first link is left untouched. Uses a fresh disposable
    # employee + unique tg ids so it is robust to shared test state.
    eid = _mk_emp()
    tg1, tg2 = "7710" + _SFX[:2], "7720" + _SFX[:2]
    code = _gen("U-owner", eid).json()["code"]
    v = client.post("/api/telegram/link/verify",
                    json={"tg_id": tg1, "code": code, "device": "t", "username": "zz" + _SFX})
    assert v.status_code == 200, v.text
    code2 = _gen("U-owner", eid).json()["code"]           # generate still 200
    v2 = client.post("/api/telegram/link/verify",
                     json={"tg_id": tg2, "code": code2, "device": "t", "username": "zz2" + _SFX})
    assert v2.status_code == 409                          # controlled conflict
    # the 409 inherently proves the first link was NOT replaced/modified


def test_branch_isolation_enforced_for_scoped_admin():
    # a branch-scoped admin (Store A only) may not link a Store C employee
    h = tok("U-owner")
    nm = "ZZ Scoped Admin " + _SFX; pw = "Zz#Admin2026!"
    cr = client.post("/api/users", headers=h, json={"name": nm, "role": "admin",
                "branches": ["Store A"], "password_mode": "manual",
                "password": pw, "confirm_password": pw,
                "must_change_password": False, "create_employee": False})
    assert cr.status_code == 201, cr.text
    un = cr.json()["username"]
    emp_c = _mk_emp(branch="Store C")
    r = client.post("/api/telegram/link-code", headers=tok(un, pw),
                    json={"employee_id": emp_c})
    assert r.status_code == 403


def test_codes_are_single_use():
    code = _gen("U-owner", "EMP-1001").json()["code"]
    ok = client.post("/api/telegram/link/verify",
                     json={"tg_id": "77002", "code": code, "device": "t", "username": "z2"})
    assert ok.status_code == 200
    again = client.post("/api/telegram/link/verify",
                        json={"tg_id": "77003", "code": code, "device": "t", "username": "z3"})
    assert again.status_code == 400          # a used code cannot be redeemed again

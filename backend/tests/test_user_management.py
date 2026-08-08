"""Owner-only user management — server-enforced RBAC, create/update/activate/
deactivate, secure password reset, and the last-active-Owner lockout guard.

Everything is enforced on the server via require("manage_users") (Owner-only in
the permission map). Front-end role controls are never trusted.
"""
import os
import tempfile

_DB = os.path.join(tempfile.gettempdir(), f"smokestack_usermgmt_{os.getpid()}.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["SEED_ON_START"] = "true"
os.environ["JWT_SECRET"] = "test-secret"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402
from app import models  # noqa: E402
from app.database import SessionLocal  # noqa: E402

client = TestClient(app)
PW = "demo1234"
BRANCHES = ["Store A", "Store B", "Store C"]


def tok(uid, pw=PW):
    r = client.post("/api/auth/login", data={"username": uid, "password": pw})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["access_token"]}


@pytest.fixture(autouse=True)
def _restore_shared_state():
    """CI runs the whole suite in one process on a shared SQLite DB. Clean up the
    accounts this module creates and re-assert U-owner as the canonical active
    Owner, so no other module inherits a demoted/disabled owner or stray users."""
    yield
    db = SessionLocal()
    try:
        ids = [u.id for u in db.query(models.User).filter(models.User.id.like("UM-%")).all()]
        for uid in ids:
            db.query(models.UserBranch).filter(models.UserBranch.user_id == uid).delete()
        db.query(models.User).filter(models.User.id.like("UM-%")).delete()
        db.query(models.Employee).filter(models.Employee.id.like("EMP-UM-%")).delete()
        o = db.get(models.User, "U-owner")
        if o:
            o.role = "owner"
            o.status = "active"
            o.can_login = True
            o.must_change_password = False
        db.commit()
    finally:
        db.close()


def _create(owner_h, username, role="employee", branches=None, name=None):
    body = {"name": name or username.replace("UM-", "Test ").replace("-", " "),
            "username": username, "role": role, "branches": branches or []}
    r = client.post("/api/users", json=body, headers=owner_h)
    assert r.status_code == 201, r.text
    return r.json()


def _first_login(created, username, new_pw):
    """Sign in with the temporary password and complete the forced first-login
    change (no re-typing the temp). Returns the auth header with the rotated token."""
    r = client.post("/api/auth/login", data={"username": username, "password": created["temp_password"]})
    assert r.status_code == 200 and r.json()["must_change_password"] is True
    h = {"Authorization": "Bearer " + r.json()["access_token"]}
    r2 = client.post("/api/auth/change-password", headers=h, json={"new_password": new_pw})
    assert r2.status_code == 200, r2.text
    return {"Authorization": "Bearer " + r2.json()["access_token"]}


# --------------------------------------------------------------- RBAC gate
def test_manage_users_is_owner_only():
    with TestClient(app):
        owner = tok("U-owner")
        assert client.get("/api/users", headers=owner).status_code == 200
        # admin/accountant/cashier/branch_manager do NOT hold manage_users
        for uid in ("U-admin", "U-acct", "U-cash", "U-bm"):
            h = tok(uid)
            assert client.get("/api/users", headers=h).status_code == 403
            assert client.post("/api/users", json={"name": "X", "username": "UM-x", "role": "employee"},
                               headers=h).status_code == 403
            assert client.put("/api/users/U-cash", json={"role": "employee"}, headers=h).status_code == 403
            assert client.post("/api/users/U-cash/deactivate", headers=h).status_code == 403
            assert client.post("/api/users/U-cash/reset-password", headers=h).status_code == 403


def test_unauthenticated_is_rejected():
    with TestClient(app):
        assert client.get("/api/users").status_code == 401
        assert client.post("/api/users", json={"name": "x"}).status_code == 401


# --------------------------------------------------------------- create
def test_create_owner_returns_temp_password_once_and_can_login():
    with TestClient(app):
        owner = tok("U-owner")
        u = _create(owner, "UM-owner1", role="owner", branches=BRANCHES, name="Jane Owner")
        assert u["role"] == "owner"
        assert sorted(u["branches"]) == sorted(BRANCHES)
        assert u["must_change_password"] is True
        assert "temp_password" in u and len(u["temp_password"]) >= 12
        assert "password_hash" not in u        # never leak the stored hash
        # the new owner can sign in with the one-time password (and is told to change it)
        r = client.post("/api/auth/login", data={"username": "UM-owner1", "password": u["temp_password"]})
        assert r.status_code == 200 and r.json()["must_change_password"] is True
        # and their token carries owner role
        assert client.get("/api/auth/me", headers={"Authorization": "Bearer " + r.json()["access_token"]}
                          ).json()["role"] == "owner"


def test_list_and_create_never_expose_password_hash():
    with TestClient(app):
        owner = tok("U-owner")
        _create(owner, "UM-emp1", role="employee", branches=["Store A"])
        rows = client.get("/api/users", headers=owner).json()
        assert rows and all("password_hash" not in r for r in rows)


def test_create_rejects_unknown_role_and_branch():
    with TestClient(app):
        owner = tok("U-owner")
        assert client.post("/api/users", json={"name": "N", "username": "UM-bad", "role": "wizard"},
                           headers=owner).status_code == 422
        assert client.post("/api/users", json={"name": "N", "username": "UM-bad2", "role": "employee",
                                               "branches": ["Store Z"]}, headers=owner).status_code == 422


# --------------------------------------------------------------- update
def test_update_role_and_branches():
    with TestClient(app):
        owner = tok("U-owner")
        _create(owner, "UM-emp2", role="employee", branches=["Store A"])
        r = client.put("/api/users/UM-emp2", json={"role": "accountant", "branches": BRANCHES,
                                                   "name": "Renamed Person"}, headers=owner)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["role"] == "accountant"
        assert sorted(body["branches"]) == sorted(BRANCHES)
        assert body["name"] == "Renamed Person"


# --------------------------------------------------------------- activate/deactivate
def test_deactivate_blocks_login_then_activate_restores():
    with TestClient(app):
        owner = tok("U-owner")
        u = _create(owner, "UM-emp3", role="employee", branches=["Store A"])
        pw = u["temp_password"]
        assert client.post("/api/auth/login", data={"username": "UM-emp3", "password": pw}).status_code == 200
        assert client.post("/api/users/UM-emp3/deactivate", headers=owner).status_code == 200
        # inactive account cannot sign in
        assert client.post("/api/auth/login", data={"username": "UM-emp3", "password": pw}).status_code == 403
        assert client.post("/api/users/UM-emp3/activate", headers=owner).status_code == 200
        assert client.post("/api/auth/login", data={"username": "UM-emp3", "password": pw}).status_code == 200


def test_cannot_deactivate_your_own_account():
    with TestClient(app):
        owner = tok("U-owner")
        r = client.post("/api/users/U-owner/deactivate", headers=owner)
        assert r.status_code == 409
        assert "own account" in r.json()["detail"].lower()


# --------------------------------------------------------------- password reset
def test_reset_password_invalidates_old_and_forces_change():
    with TestClient(app):
        owner = tok("U-owner")
        u = _create(owner, "UM-emp4", role="employee", branches=["Store A"])
        old = u["temp_password"]
        r = client.post("/api/users/UM-emp4/reset-password", headers=owner)
        assert r.status_code == 200, r.text
        new = r.json()["temp_password"]
        assert "password_hash" not in r.json()
        assert new != old
        assert r.json()["must_change_password"] is True
        # old no longer works; new works
        assert client.post("/api/auth/login", data={"username": "UM-emp4", "password": old}).status_code == 401
        assert client.post("/api/auth/login", data={"username": "UM-emp4", "password": new}).status_code == 200


# --------------------------------------------------------------- last-active-owner guard
def test_last_active_owner_cannot_be_demoted():
    with TestClient(app):
        owner = tok("U-owner")
        # U-owner is the only active Owner → demotion is refused
        r = client.put("/api/users/U-owner", json={"role": "admin"}, headers=owner)
        assert r.status_code == 409
        assert "owner" in r.json()["detail"].lower()
        # add a second active Owner, then demotion of U-owner is allowed
        o2 = _create(owner, "UM-owner2", role="owner", branches=BRANCHES)
        owner2 = _first_login(o2, "UM-owner2", "Owner2NewPass!!")   # complete forced change → usable session
        assert client.put("/api/users/U-owner", json={"role": "admin"}, headers=owner2).status_code == 200
        # restore U-owner as owner via the second owner (teardown also re-asserts this)
        assert client.put("/api/users/U-owner", json={"role": "owner"}, headers=owner2).status_code == 200


# --------------------------------------------------- forced first-login password change
def test_forced_first_login_change_without_current_preserves_owner_and_branches():
    """The reported production bug: a new Owner on a temporary password reaches the
    forced-change screen and must set a new password. It must NOT require re-typing
    the single-use temp (already verified at sign-in); role + branches are preserved."""
    with TestClient(app):
        owner = tok("U-owner")
        u = _create(owner, "UM-laith", role="owner", branches=BRANCHES, name="Laith Owner")
        r = client.post("/api/auth/login", data={"username": "UM-laith", "password": u["temp_password"]})
        assert r.status_code == 200 and r.json()["must_change_password"] is True
        h = {"Authorization": "Bearer " + r.json()["access_token"]}
        # forced change with ONLY a new password (no current) — the session authorizes it
        r2 = client.post("/api/auth/change-password", headers=h, json={"new_password": "LaithStrong#2026"})
        assert r2.status_code == 200, r2.text
        assert r2.json()["must_change_password"] is False
        assert r2.json().get("access_token")                 # session rotated
        assert "password_hash" not in r2.json() and "new_password" not in r2.json()
        # a normal login with the NEW password now works and is no longer forced
        r3 = client.post("/api/auth/login", data={"username": "UM-laith", "password": "LaithStrong#2026"})
        assert r3.status_code == 200 and r3.json()["must_change_password"] is False
        # role + all three branches preserved
        row = [x for x in client.get("/api/users", headers=tok("U-owner")).json() if x["username"] == "UM-laith"][0]
        assert row["role"] == "owner" and sorted(row["branches"]) == sorted(BRANCHES)


def test_first_login_blocks_other_endpoints_until_changed():
    with TestClient(app):
        owner = tok("U-owner")
        u = _create(owner, "UM-fl", role="owner", branches=BRANCHES)
        r = client.post("/api/auth/login", data={"username": "UM-fl", "password": u["temp_password"]})
        h = {"Authorization": "Bearer " + r.json()["access_token"]}
        # allowlisted during first login
        assert client.get("/api/auth/me", headers=h).status_code == 200
        # every other protected endpoint is blocked while on the temporary password
        assert client.get("/api/users", headers=h).status_code == 403
        assert client.get("/api/branches", headers=h).status_code == 403
        # after the forced change (rotated token), the owner can use the app
        r2 = client.post("/api/auth/change-password", headers=h, json={"new_password": "FlNewPass#2026"})
        h2 = {"Authorization": "Bearer " + r2.json()["access_token"]}
        assert client.get("/api/users", headers=h2).status_code == 200
        assert client.get("/api/branches", headers=h2).status_code == 200


def test_forced_change_rejects_weak_new_password():
    with TestClient(app):
        owner = tok("U-owner")
        u = _create(owner, "UM-weak", role="employee", branches=["Store A"])
        r = client.post("/api/auth/login", data={"username": "UM-weak", "password": u["temp_password"]})
        h = {"Authorization": "Bearer " + r.json()["access_token"]}
        assert client.post("/api/auth/change-password", headers=h,
                           json={"new_password": "short"}).status_code == 422


def test_forced_change_if_current_supplied_it_must_match():
    with TestClient(app):
        owner = tok("U-owner")
        u = _create(owner, "UM-cur", role="employee", branches=["Store A"])
        r = client.post("/api/auth/login", data={"username": "UM-cur", "password": u["temp_password"]})
        h = {"Authorization": "Bearer " + r.json()["access_token"]}
        assert client.post("/api/auth/change-password", headers=h,
                           json={"current_password": "definitely-wrong",
                                 "new_password": "GoodPass#2026"}).status_code == 403


def test_self_service_change_still_requires_current_password():
    with TestClient(app):
        owner = tok("U-owner")
        u = _create(owner, "UM-ss", role="employee", branches=["Store A"])
        h = _first_login(u, "UM-ss", "FirstPass#2026")   # clears must_change → self-service rules
        # without the current password → rejected
        assert client.post("/api/auth/change-password", headers=h,
                           json={"new_password": "AnotherPass#9"}).status_code == 403
        # wrong current → rejected
        assert client.post("/api/auth/change-password", headers=h,
                           json={"current_password": "nope", "new_password": "AnotherPass#9"}).status_code == 403
        # correct current → ok
        assert client.post("/api/auth/change-password", headers=h,
                           json={"current_password": "FirstPass#2026",
                                 "new_password": "AnotherPass#9"}).status_code == 200

"""Owner-controlled password assignment — generated vs manual, and its guards.

Covers the Add-User and Reset-Password password modes:
  * generated one-time password (default, backward-compatible) — returned once,
    forces a change at first login;
  * manual password — accepted only in the POST body, hashed immediately, NEVER
    returned/logged/audited, may be permanent or forced-change at the Owner's choice.
All creation/reset stays Owner-only and is re-validated server-side.
"""
import os
import json
import tempfile

_DB = os.path.join(tempfile.gettempdir(), f"smokestack_pwmodes_{os.getpid()}.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["SEED_ON_START"] = "true"
os.environ["JWT_SECRET"] = "pw-modes-secret-long-enough"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402
from app import security as S  # noqa: E402

client = TestClient(app)
MANUAL_OK = "Wharf9-Basalt-Ledger"   # strong, varied, not in any deny-list


@pytest.fixture(scope="module", autouse=True)
def _boot():
    with TestClient(app):
        yield


def _tok(uid="U-owner", pw="demo1234"):
    r = client.post("/api/auth/login", data={"username": uid, "password": pw})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["access_token"]}


def _create(name, **extra):
    body = {"name": name, "role": "employee", "branches": ["Store A"]}
    body.update(extra)
    return client.post("/api/users", headers=_tok(), json=body)


# --------------------------------------------------------------- generated mode
def test_generated_mode_returns_temp_once_and_forces_change():
    r = _create("Gen Default")               # no password_mode → generated
    assert r.status_code == 201, r.text
    b = r.json()
    assert b["must_change_password"] is True
    assert b.get("temp_password") and len(b["temp_password"]) >= 14
    lr = client.post("/api/auth/login",
                     data={"username": b["username"], "password": b["temp_password"]})
    assert lr.status_code == 200 and lr.json()["must_change_password"] is True


def test_explicit_generated_mode_matches_default():
    r = _create("Gen Explicit", password_mode="generated")
    assert r.status_code == 201 and r.json().get("temp_password")


# --------------------------------------------------------------- manual mode
def test_manual_permanent_password_never_returned_and_logs_in():
    r = _create("Manual Perm", password_mode="manual",
                password=MANUAL_OK, confirm_password=MANUAL_OK,
                must_change_password=False)
    assert r.status_code == 201, r.text
    b = r.json()
    assert b["must_change_password"] is False
    # the manual password / hash must never come back
    blob = json.dumps(b)
    assert MANUAL_OK not in blob
    assert "temp_password" not in b and "password" not in b and "password_hash" not in blob
    # it works, with NO forced change
    lr = client.post("/api/auth/login",
                     data={"username": b["username"], "password": MANUAL_OK})
    assert lr.status_code == 200 and lr.json()["must_change_password"] is False


def test_manual_forced_change_password():
    r = _create("Manual Forced", password_mode="manual",
                password=MANUAL_OK, confirm_password=MANUAL_OK,
                must_change_password=True)
    assert r.status_code == 201
    b = r.json()
    assert b["must_change_password"] is True and "temp_password" not in b
    lr = client.post("/api/auth/login", data={"username": b["username"], "password": MANUAL_OK})
    assert lr.status_code == 200 and lr.json()["must_change_password"] is True


def test_manual_confirmation_mismatch_is_rejected():
    r = _create("Manual Mismatch", password_mode="manual",
                password=MANUAL_OK, confirm_password=MANUAL_OK + "x")
    assert r.status_code == 422
    assert MANUAL_OK not in r.text          # error must not echo the password


@pytest.mark.parametrize("pw", [
    "Ab1$xy",                    # too short (<10)
    "password123",               # common / deny-listed
    "ababababab",                # long enough but too little variety
    "Xy7$" * 19,                 # 76 bytes — exceeds bcrypt's 72-byte limit
])
def test_weak_short_and_overlimit_manual_passwords_rejected(pw):
    r = _create("Manual Weak", password_mode="manual", password=pw, confirm_password=pw)
    assert r.status_code == 422, (pw, r.text)
    assert pw not in r.text                  # generic error, no password echo


# --------------------------------------------------------------- authz
def test_non_owner_gets_403():
    # U-cash has no manage_users permission
    r = client.post("/api/users", headers=_tok("U-cash", "demo1234"),
                    json={"name": "Nope", "role": "employee"})
    assert r.status_code == 403


def test_unauthenticated_gets_401():
    r = client.post("/api/users", json={"name": "Nope", "role": "employee"})
    assert r.status_code == 401


# --------------------------------------------------------------- reset password
def test_reset_generated_backward_compatible_empty_body():
    b = _create("Reset Gen").json()
    r = client.post(f"/api/users/{b['username']}/reset-password", headers=_tok())
    assert r.status_code == 200
    body = r.json()
    assert body["must_change_password"] is True and body.get("temp_password")
    # old temp no longer works
    assert client.post("/api/auth/login",
                       data={"username": b["username"], "password": b["temp_password"]}
                       ).status_code == 401


def test_reset_manual_permanent_invalidates_old_and_is_not_returned():
    b = _create("Reset Manual").json()
    old = b["temp_password"]
    r = client.post(f"/api/users/{b['username']}/reset-password", headers=_tok(),
                    json={"password_mode": "manual", "password": MANUAL_OK,
                          "confirm_password": MANUAL_OK, "must_change_password": False})
    assert r.status_code == 200
    body = r.json()
    assert body["must_change_password"] is False
    assert "temp_password" not in body and MANUAL_OK not in json.dumps(body)
    assert client.post("/api/auth/login",
                       data={"username": b["username"], "password": old}).status_code == 401
    assert client.post("/api/auth/login",
                       data={"username": b["username"], "password": MANUAL_OK}
                       ).json()["must_change_password"] is False


def test_reset_manual_rejects_weak():
    b = _create("Reset Weak").json()
    r = client.post(f"/api/users/{b['username']}/reset-password", headers=_tok(),
                    json={"password_mode": "manual", "password": "short", "confirm_password": "short"})
    assert r.status_code == 422


# --------------------------------------------------------------- no leakage in audit
def test_audit_never_contains_password_values():
    r = _create("Audit Chk", password_mode="manual",
                password=MANUAL_OK, confirm_password=MANUAL_OK, must_change_password=False)
    un = r.json()["username"]
    client.post(f"/api/users/{un}/reset-password", headers=_tok(),
                json={"password_mode": "manual", "password": MANUAL_OK,
                      "confirm_password": MANUAL_OK, "must_change_password": False})
    rows = client.get("/api/audit?limit=200", headers=_tok()).json()
    dump = json.dumps(rows)
    assert MANUAL_OK not in dump
    assert "password_hash" not in dump
    # the relevant actions were audited by username, without the secret
    acts = [a for a in rows if a.get("ref") == un
            and a.get("action") in ("create_user", "reset_password")]
    assert acts, "create/reset must be audited"
    for a in acts:
        assert MANUAL_OK not in (a.get("detail") or "")


# --------------------------------------------------------------- safeguards intact
def test_last_active_owner_cannot_be_deactivated():
    # creating/resetting other users must not weaken the last-owner guard
    assert client.post("/api/users/U-owner/deactivate", headers=_tok()).status_code == 409


def test_existing_owner_role_and_branches_unchanged_by_password_ops():
    before = client.get("/api/users", headers=_tok()).json()
    owner_before = next(u for u in before if u["username"] == "U-owner")
    _create("Some User", password_mode="manual", password=MANUAL_OK, confirm_password=MANUAL_OK)
    after = client.get("/api/users", headers=_tok()).json()
    owner_after = next(u for u in after if u["username"] == "U-owner")
    assert owner_after["role"] == owner_before["role"] == "owner"
    assert owner_after["branches"] == owner_before["branches"]
    assert owner_after["must_change_password"] == owner_before["must_change_password"]


def test_manual_hash_actually_stored_not_plaintext():
    b = _create("Hash Chk", password_mode="manual",
                password=MANUAL_OK, confirm_password=MANUAL_OK).json()
    from app.database import SessionLocal
    from app import models
    db = SessionLocal()
    try:
        u = db.get(models.User, b["username"])
        assert u.password_hash and u.password_hash != MANUAL_OK
        assert S.verify_pw(MANUAL_OK, u.password_hash) is True
    finally:
        db.close()

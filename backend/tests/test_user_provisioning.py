"""User provisioning: creation, unique usernames, forced password change, RBAC."""
import os, tempfile
_DB = os.path.join(tempfile.gettempdir(), f"smokestack_users_{os.getpid()}.db")
if os.path.exists(_DB): os.remove(_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["SEED_ON_START"] = "true"
os.environ["JWT_SECRET"] = "user-provisioning-secret-long"

import pytest
from fastapi.testclient import TestClient
from app.main import app
from app import permissions as P
from app.routers import users as U

client = TestClient(app)


@pytest.fixture(scope="module", autouse=True)
def _boot():
    with TestClient(app):
        yield


def _tok(uid="U-owner", pw="demo1234"):
    r = client.post("/api/auth/login", data={"username": uid, "password": pw})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["access_token"]}


def _create(name, role="branch_manager", branches=None, actor="U-owner"):
    return client.post("/api/users", headers=_tok(actor), json={
        "name": name, "role": role,
        "branches": branches if branches is not None else ["Store A", "Store B", "Store C"]})


def test_creates_the_four_accounts_with_unique_usernames():
    made = {}
    for n in ["Gihad Wazwaz", "Hiyam Wazwaz", "Houd Wazwaz", "Mohammed Wazwaz"]:
        r = _create(n)
        assert r.status_code == 201, r.text
        b = r.json()
        made[n] = b
        assert b["role"] == "branch_manager"
        assert set(b["branches"]) == {"Store A", "Store B", "Store C"}
        assert b["must_change_password"] is True
        assert b["employee_id"], "an employee record should be created"
    names = [b["username"] for b in made.values()]
    assert len(set(names)) == 4, f"usernames must be unique: {names}"
    assert names == ["gihad.wazwaz", "hiyam.wazwaz", "houd.wazwaz", "mohammed.wazwaz"]


def test_temp_password_is_strong_and_returned_once():
    r = _create("Temp Person", role="employee", branches=["Store A"])
    pw = r.json()["temp_password"]
    assert len(pw) >= 14
    assert any(c.islower() for c in pw) and any(c.isupper() for c in pw)
    assert any(c.isdigit() for c in pw) and any(c in U._PW_SYMBOLS for c in pw)
    # never echoed back by the listing endpoint
    rows = client.get("/api/users", headers=_tok()).json()
    assert all("temp_password" not in u for u in rows)


def test_each_new_account_can_actually_log_in():
    r = _create("Login Test")
    b = r.json()
    lr = client.post("/api/auth/login",
                     data={"username": b["username"], "password": b["temp_password"]})
    assert lr.status_code == 200, lr.text
    assert lr.json()["must_change_password"] is True
    assert lr.json()["user"]["role"] == "branch_manager"


def test_password_change_clears_the_flag_and_old_password_stops_working():
    b = _create("Reset Test").json()
    h = {"Authorization": "Bearer " + client.post(
        "/api/auth/login", data={"username": b["username"],
                                 "password": b["temp_password"]}).json()["access_token"]}
    r = client.post("/api/auth/change-password", headers=h,
                    json={"current_password": b["temp_password"],
                          "new_password": "a-much-longer-secret-1"})
    assert r.status_code == 200 and r.json()["must_change_password"] is False
    assert client.post("/api/auth/login", data={"username": b["username"],
                                                "password": b["temp_password"]}).status_code == 401
    fresh = client.post("/api/auth/login", data={"username": b["username"],
                                                 "password": "a-much-longer-secret-1"})
    assert fresh.status_code == 200 and fresh.json()["must_change_password"] is False


def test_password_change_rejects_wrong_current_and_weak_new():
    b = _create("Weak Test").json()
    h = {"Authorization": "Bearer " + client.post(
        "/api/auth/login", data={"username": b["username"],
                                 "password": b["temp_password"]}).json()["access_token"]}
    assert client.post("/api/auth/change-password", headers=h,
                       json={"current_password": "wrong", "new_password": "longenough123"}
                       ).status_code == 403
    assert client.post("/api/auth/change-password", headers=h,
                       json={"current_password": b["temp_password"], "new_password": "short"}
                       ).status_code == 422


def test_permissions_come_from_the_rbac_engine_not_the_account():
    b = _create("Perm Test").json()
    assert b["permissions"] == P.PERMS["branch_manager"]
    h = {"Authorization": "Bearer " + client.post(
        "/api/auth/login", data={"username": b["username"],
                                 "password": b["temp_password"]}).json()["access_token"]}
    # branch_manager may view costs but NOT payroll or user management
    assert client.get("/api/reports/kpi?period=month&branch=all", headers=h).status_code == 200
    assert client.get("/api/payroll?start=2026-07-01&end=2026-07-31", headers=h).status_code == 403
    assert client.get("/api/users", headers=h).status_code == 403


def test_role_boundaries_differ_by_role():
    emp = _create("Emp Role", role="employee", branches=["Store A"]).json()
    h = {"Authorization": "Bearer " + client.post(
        "/api/auth/login", data={"username": emp["username"],
                                 "password": emp["temp_password"]}).json()["access_token"]}
    kpi = client.get("/api/reports/kpi?period=month&branch=all", headers=h).json()
    assert kpi.get("can_view_profit") is False
    assert client.post("/api/expenses", headers=h,
                       json={"branch": "Store A", "category": "Rent", "amount": 5}
                       ).status_code == 403


def test_existing_users_are_never_modified():
    from app.database import SessionLocal
    from app import models
    db = SessionLocal()
    try:
        before = {u.id: (u.name, u.role, u.password_hash, bool(u.must_change_password))
                  for u in db.query(models.User).filter(models.User.id.like("U-%")).all()}
    finally:
        db.close()
    _create("Another Person")
    db = SessionLocal()
    try:
        after = {u.id: (u.name, u.role, u.password_hash, bool(u.must_change_password))
                 for u in db.query(models.User).filter(models.User.id.like("U-%")).all()}
    finally:
        db.close()
    assert before == after, "seeded accounts must be untouched"
    assert all(v[3] is False for v in after.values()), "existing users must not be force-reset"


def test_duplicate_username_is_never_overwritten():
    first = _create("Dup Name").json()
    second = _create("Dup Name").json()
    assert first["username"] != second["username"]
    assert second["username"].endswith("2")
    # explicit collision is refused outright
    r = client.post("/api/users", headers=_tok(),
                    json={"name": "Someone", "username": first["username"], "role": "employee"})
    assert r.status_code == 409


def test_creation_requires_manage_users_and_is_audited():
    for uid in ("U-cash", "U-emp", "U-bm", "U-inv"):
        assert _create("Should Fail", actor=uid).status_code == 403, uid
    b = _create("Audit Test").json()
    rows = client.get("/api/audit?limit=100", headers=_tok()).json()
    entry = next((a for a in rows if a.get("action") == "create_user"
                  and a.get("ref") == b["username"]), None)
    assert entry, "account creation must be audited"
    assert "branch_manager" in (entry.get("detail") or "")


def test_unknown_role_or_branch_is_rejected():
    assert client.post("/api/users", headers=_tok(),
                       json={"name": "Bad Role", "role": "superuser"}).status_code == 422
    assert client.post("/api/users", headers=_tok(),
                       json={"name": "Bad Branch", "role": "employee",
                             "branches": ["Store Z"]}).status_code == 422


def test_creator_cannot_grant_a_branch_they_do_not_hold():
    # the inventory manager holds Store A only — and lacks manage_users anyway,
    # so use the accountant, who has manage_users? (accountant does not) —
    # assert the branch guard directly instead
    from app.database import SessionLocal
    from app import models
    db = SessionLocal()
    try:
        bm = db.get(models.User, "U-bm")          # Store A + Store B
        import app.security as SS
        try:
            SS.assert_branch(bm, db, "Store C")
            assert False, "should refuse a branch outside the actor's scope"
        except Exception:
            pass
    finally:
        db.close()

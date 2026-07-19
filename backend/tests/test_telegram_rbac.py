"""Telegram RBAC: every account inherits its employee's ERP permissions, the
owner can switch individual capabilities off, and nothing can be switched on
beyond what the role already allows.
"""
import os
import tempfile

_DB = os.path.join(tempfile.gettempdir(), f"smokestack_tgrbac_{os.getpid()}.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["SEED_ON_START"] = "true"
os.environ["JWT_SECRET"] = "tg-rbac-secret-long-enough"
os.environ["TELEGRAM_BOT_TOKEN"] = "test-bot-token"

from fastapi.testclient import TestClient
from app.main import app
from app import tg_caps as C, permissions as P
from app.config import settings

client = TestClient(app)


def _bot():
    if not settings.bot_token:
        settings.bot_token = "test-bot-token"
    return {"X-Bot-Token": settings.bot_token}


def _tok(uid="U-owner"):
    r = client.post("/api/auth/login", data={"username": uid, "password": "demo1234"})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["access_token"]}


# four employees, four different permission sets
CAST = [("RB-OWNER", "Rb Owner", "Store A", "owner", "71001"),
        ("RB-MGR", "Rb Manager", "Store B", "branch_manager", "71002"),
        ("RB-CASH", "Rb Cashier", "Store A", "cashier", "71003"),
        ("RB-EMP", "Rb Employee", "Store C", "employee", "71004")]


def _setup():
    h = _tok()
    for eid, name, branch, role, tg in CAST:
        client.post("/api/employees", headers=h, json={
            "id": eid, "name": name, "branch": branch, "title": "Staff",
            "pay_type": "salary", "salary": 1000, "role": role})
        code = client.post("/api/telegram/link-code", headers=h,
                           json={"employee_id": eid}).json().get("code")
        if code:
            client.post("/api/telegram/link/verify",
                        json={"tg_id": tg, "code": code, "username": f"tg_{eid.lower()}"})


def _authorize(tg, cap, branch=None, command=None):
    return client.post("/api/telegram/authorize", headers=_bot(), json={
        "tg_id": tg, "capability": cap, "branch": branch, "command": command or cap})


def _caps(tg):
    r = client.get(f"/api/telegram/capabilities/{tg}", headers=_bot())
    assert r.status_code == 200, r.text
    return r.json()["capabilities"]


def test_capabilities_are_derived_from_the_erp_role_not_hardcoded():
    with TestClient(app):
        _setup()
        owner, mgr, cash, emp = (_caps(t) for _, _, _, _, t in CAST)

        # the owner role grants every capability in the catalogue
        assert all(owner.values()), [k for k, v in owner.items() if not v]

        # a cashier cannot see payroll or the control centre; an employee cannot
        # even create — and none of this is written down in the bot
        assert cash["payroll"] is False
        assert cash["control_center"] is False
        assert cash["daily_sales"] is True          # cashier may create sales
        assert emp["daily_sales"] is False          # employee has view+print only
        assert emp["expenses"] is False
        assert emp["reports"] is True
        assert emp["print"] is True
        assert mgr["approvals"] is True             # branch manager may approve
        assert cash["approvals"] is False

        # every capability's state matches the permission engine exactly
        for eid, _, _, role, tg in CAST:
            eff = _caps(tg)
            for cap in C.CAP_KEYS:
                expected = all(P.can(role, p) for p in C.CAP_PERMS[cap])
                assert eff[cap] == expected, f"{role}/{cap}: {eff[cap]} != {expected}"


def test_authorize_allows_granted_and_denies_missing_with_the_exact_message():
    with TestClient(app):
        r = _authorize("71003", "daily_sales", "Store A")
        assert r.status_code == 200 and r.json()["allowed"] is True

        r = _authorize("71003", "payroll")
        body = r.json()
        assert body["allowed"] is False
        assert body["reason"] == "role_forbids"
        assert body["message"] == "❌ You don't have permission to perform this action."


def test_owner_can_switch_a_single_capability_off():
    with TestClient(app):
        h = _tok()
        # cashier may record sales today
        assert _authorize("71003", "daily_sales").json()["allowed"] is True

        r = client.put("/api/employees/RB-CASH/telegram-permissions", headers=h,
                       json={"capabilities": {"daily_sales": False}})
        assert r.status_code == 200, r.text

        denied = _authorize("71003", "daily_sales").json()
        assert denied["allowed"] is False
        assert denied["reason"] == "disabled_by_owner"
        assert denied["message"] == C.DENIED_MESSAGE
        # and only that one capability changed
        assert _caps("71003")["expenses"] is True
        assert _caps("71003")["print"] is True

        # switching it back on restores it
        client.put("/api/employees/RB-CASH/telegram-permissions", headers=h,
                   json={"capabilities": {"daily_sales": True}})
        assert _authorize("71003", "daily_sales").json()["allowed"] is True


def test_toggles_can_never_exceed_the_role():
    """The ERP permission map is the ceiling — Telegram is not an escalation path."""
    with TestClient(app):
        h = _tok()
        r = client.put("/api/employees/RB-EMP/telegram-permissions", headers=h,
                       json={"capabilities": {"payroll": True, "control_center": True,
                                              "daily_sales": True}})
        assert r.status_code == 200, r.text
        body = r.json()
        assert set(body["rejected"]) == {"payroll", "control_center", "daily_sales"}
        eff = _caps("71004")
        assert eff["payroll"] is False and eff["control_center"] is False
        assert _authorize("71004", "payroll").json()["allowed"] is False


def test_branch_scoped_capabilities():
    with TestClient(app):
        # the cashier belongs to Store A only
        assert _authorize("71003", "expenses", "Store A").json()["allowed"] is True
        out = _authorize("71003", "expenses", "Store B").json()
        assert out["allowed"] is False and out["reason"] == "branch_out_of_scope"
        assert out["message"] == C.DENIED_MESSAGE
        # the manager belongs to Store B
        assert _authorize("71002", "expenses", "Store B").json()["allowed"] is True
        assert _authorize("71002", "expenses", "Store A").json()["allowed"] is False
        # an owner reaches every branch
        for b in ("Store A", "Store B", "Store C"):
            assert _authorize("71001", "expenses", b).json()["allowed"] is True


def test_every_decision_is_audited_with_full_context():
    with TestClient(app):
        _authorize("71003", "payroll", "Store A", command="/payroll")
        _authorize("71003", "expenses", "Store A", command="/expense")
        act = client.get("/api/telegram/accounts/71003/activity", headers=_tok()).json()
        entries = act["entries"]
        denied = next(e for e in entries if e["action"] == "/payroll")
        okd = next(e for e in entries if e["action"] == "/expense")
        assert denied["result"] == "denied" and okd["result"] == "ok"
        for e in (denied, okd):
            assert e["ts"] and e["branch"] == "Store A"
            assert e["role"] == "cashier"
            assert e["tg_username"] == "tg_rb-cash"
            assert e["ip"] == "telegram"
        assert act["account"]["employee"] == "Rb Cashier"
        assert act["account"]["tg_id"] == "71003"


def test_disabled_account_is_denied_everything():
    with TestClient(app):
        h = _tok()
        assert client.post("/api/telegram/accounts/71004/disable", headers=h).status_code == 200
        out = _authorize("71004", "reports").json()
        assert out["allowed"] is False and out["reason"] == "not_linked"
        assert client.post("/api/telegram/accounts/71004/enable", headers=h).status_code == 200
        assert _authorize("71004", "reports").json()["allowed"] is True


def test_admin_interface_shows_state_and_explains_locked_rows():
    with TestClient(app):
        r = client.get("/api/employees/RB-CASH/telegram-permissions", headers=_tok())
        assert r.status_code == 200
        body = r.json()
        assert body["role"] == "cashier" and body["linked"] is True
        assert body["editable"] is True
        rows = {c["key"]: c for c in body["capabilities"]}
        assert len(rows) == len(C.CAP_KEYS) == 17
        assert rows["payroll"]["locked"] is True
        assert "view_payroll" in rows["payroll"]["reason"]
        assert rows["daily_sales"]["locked"] is False
        for c in body["capabilities"]:
            assert c["label"] and isinstance(c["requires"], list)


def test_permission_editing_requires_privilege():
    with TestClient(app):
        for uid in ("U-cash", "U-emp", "U-bm"):
            r = client.put("/api/employees/RB-CASH/telegram-permissions",
                           headers=_tok(uid), json={"capabilities": {"expenses": False}})
            assert r.status_code == 403, uid
        assert client.get("/api/telegram/capabilities/71003").status_code == 403
        assert client.post("/api/telegram/authorize",
                           json={"tg_id": "71003", "capability": "expenses"}).status_code == 403


def test_unknown_capability_is_rejected():
    with TestClient(app):
        out = _authorize("71003", "launch_missiles").json()
        assert out["allowed"] is False and out["reason"] == "unknown_capability"
        r = client.put("/api/employees/RB-CASH/telegram-permissions", headers=_tok(),
                       json={"capabilities": {"launch_missiles": True}})
        assert r.status_code == 422


def test_underlying_erp_endpoints_still_enforce_rbac_independently():
    """Defence in depth: even if the bot skipped /authorize, the ERP API refuses."""
    with TestClient(app):
        tok = client.post("/api/telegram/auth-token", json={"tg_id": "71003"},
                          headers=_bot()).json()["access_token"]
        h = {"Authorization": "Bearer " + tok}
        assert client.get("/api/payroll?start=2026-07-01&end=2026-07-31",
                          headers=h).status_code == 403
        assert client.post("/api/expenses", json={"branch": "Store B", "category": "Rent",
                                                  "amount": 5}, headers=h).status_code == 403

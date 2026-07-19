"""Telegram Management Center — multi-user linking, isolation, RBAC, audit,
concurrency and backward compatibility."""
import os
import tempfile
import threading

_DB = os.path.join(tempfile.gettempdir(), f"smokestack_tgmu_{os.getpid()}.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["SEED_ON_START"] = "true"
os.environ["JWT_SECRET"] = "tg-multiuser-secret-long"
os.environ["TELEGRAM_BOT_TOKEN"] = "test-bot-token"

from datetime import datetime, timezone
from fastapi.testclient import TestClient
from app.main import app
from app.database import SessionLocal
from app import models
from app.config import settings

client = TestClient(app)


def _bot():
    """Bot header resolved at call time. Other test modules mutate
    settings.bot_token at runtime, so an import-time constant goes stale."""
    if not settings.bot_token:
        settings.bot_token = "test-bot-token"
    return {"X-Bot-Token": settings.bot_token}

USERS = ["U-owner", "U-admin", "U-bm", "U-inv", "U-acct", "U-cash", "U-emp"]


def _tok(uid="U-owner"):
    r = client.post("/api/auth/login", data={"username": uid, "password": "demo1234"})
    return {"Authorization": "Bearer " + r.json()["access_token"]}


def _free_slot(user_id=None, tg_id=None):
    """Linking is INSERT-only and rejects a second active account, so tests that
    re-link the same person must first release the slot — exactly what an admin
    does with Remove in the Telegram Management Center."""
    from app.database import SessionLocal
    from app import models as _m
    db = SessionLocal()
    try:
        q = db.query(_m.TelegramLink)
        rows = list(q.filter(_m.TelegramLink.tg_id == str(tg_id)).all()) if tg_id else []
        if user_id:
            rows += list(q.filter(_m.TelegramLink.user_id == user_id,
                                  _m.TelegramLink.status == "active").all())
        for r in rows:
            db.delete(r)
        db.commit()
    finally:
        db.close()


def _link(uid, tg_id, username, free=True):
    """Issue a code as the user, then redeem it as the bot would."""
    if free:
        _free_slot(user_id=uid, tg_id=tg_id)
    code = client.post("/api/telegram/link-code", headers=_tok(uid)).json()["code"]
    return client.post("/api/telegram/link/verify",
                       json={"tg_id": str(tg_id), "code": code, "username": username})


def test_many_accounts_link_without_disconnecting_each_other():
    """The core requirement: linking new accounts never unlinks existing ones."""
    with TestClient(app):
        for i, uid in enumerate(USERS):
            r = _link(uid, 90000 + i, f"user{i}")
            assert r.status_code == 200, r.text
            # after EVERY new link, all previously linked accounts must still resolve
            for j, prev in enumerate(USERS[:i + 1]):
                s = client.get(f"/api/telegram/session/{90000 + j}").json()
                assert s["linked"] is True, f"{prev} was disconnected when linking {uid}"
        accts = client.get("/api/telegram/accounts", headers=_tok("U-owner")).json()
        # scoped to this module's ids: the suite shares one database, so other
        # modules legitimately contribute rows of their own
        mine = [a for a in accts if a["tg_id"] in {str(90000 + i) for i in range(len(USERS))}]
        assert len(mine) == len(USERS), mine
        assert len({a["tg_id"] for a in mine}) == len(USERS), "duplicate tg ids"


def test_each_account_maps_to_one_employee_role_and_branch():
    with TestClient(app):
        accts = client.get("/api/telegram/accounts", headers=_tok("U-owner")).json()
        for a in accts:
            assert a["employee"], a
            assert a["role"], a
            assert "branches" in a and "permissions" in a
        bm = next(a for a in accts if a["user_id"] == "U-bm")
        assert bm["role"] == "branch_manager"
        assert set(bm["branches"]) == {"Store A", "Store B"}


def test_second_device_is_rejected_never_silently_replaces():
    """An employee may hold one ACTIVE account. Linking a second device is
    REJECTED (409) rather than silently replacing the first — linking must never
    modify or delete an existing row."""
    with TestClient(app):
        before = client.get("/api/telegram/accounts", headers=_tok("U-owner")).json()
        n_before = len(before)
        r = _link("U-cash", 95555, "cashier_newphone", free=False)
        assert r.status_code == 409, "a second device must not overwrite the first"
        accts = client.get("/api/telegram/accounts", headers=_tok("U-owner")).json()
        mine = [a for a in accts if a["user_id"] == "U-cash"]
        assert len(mine) == 1, "employee ended up with more than one active account"
        assert len(accts) == n_before, "a rejected link must not change anything"
        # everyone else still linked
        for j, uid in enumerate(USERS):
            assert client.get(f"/api/telegram/session/{90000 + j}").json()["linked"] is True


def test_duplicate_telegram_id_is_rejected():
    with TestClient(app):
        code = client.post("/api/telegram/link-code", headers=_tok("U-acct")).json()["code"]
        r = client.post("/api/telegram/link/verify",
                        json={"tg_id": "90000", "code": code, "username": "thief"})
        assert r.status_code == 409, "a tg id must not be re-pointed at another employee"


def test_disable_enable_remove_affect_only_one_account():
    with TestClient(app):
        h = _tok("U-owner")
        target = "90003"
        assert client.post(f"/api/telegram/accounts/{target}/disable", headers=h).status_code == 200
        # disabled account is refused everywhere
        assert client.post("/api/telegram/auth-token", json={"tg_id": target},
                           headers=_bot()).status_code == 403
        assert client.get(f"/api/telegram/session/{target}").json()["linked"] is False
        # every other account still works
        assert client.post("/api/telegram/auth-token", json={"tg_id": "90000"},
                           headers=_bot()).status_code == 200
        # enable restores it
        assert client.post(f"/api/telegram/accounts/{target}/enable", headers=h).status_code == 200
        assert client.post("/api/telegram/auth-token", json={"tg_id": target},
                           headers=_bot()).status_code == 200
        # remove deletes only that row
        n = len(client.get("/api/telegram/accounts", headers=h).json())
        assert client.delete(f"/api/telegram/accounts/{target}", headers=h).status_code == 200
        after = client.get("/api/telegram/accounts", headers=h).json()
        assert len(after) == n - 1 and target not in {a["tg_id"] for a in after}
        assert client.post("/api/telegram/auth-token", json={"tg_id": "90000"},
                           headers=_bot()).status_code == 200


def test_stats_and_filters():
    with TestClient(app):
        h = _tok("U-owner")
        st = client.get("/api/telegram/stats", headers=h).json()
        for k in ("total", "active", "disabled", "last_sync", "last_bot_activity"):
            assert k in st, k
        assert st["total"] == st["active"] + st["disabled"]
        assert client.get("/api/telegram/accounts?role=owner", headers=h).status_code == 200
        by_role = client.get("/api/telegram/accounts?role=branch_manager", headers=h).json()
        assert all(a["role"] == "branch_manager" for a in by_role)
        by_branch = client.get("/api/telegram/accounts?branch=Store B", headers=h).json()
        assert all("Store B" in a["branches"] for a in by_branch)
        assert isinstance(client.get("/api/telegram/accounts?q=user1", headers=h).json(), list)


def test_rbac_each_account_scoped_to_its_own_permissions():
    """A Telegram session must inherit exactly the employee's ERP permissions."""
    with TestClient(app):
        # self-contained: link fresh accounts so this test never depends on
        # rows other tests disabled or removed
        assert _link("U-emp", 96001, "emp_rbac").status_code == 200
        assert _link("U-inv", 96002, "inv_rbac").status_code == 200

        etok = client.post("/api/telegram/auth-token", json={"tg_id": "96001"},
                           headers=_bot()).json()["access_token"]
        eh = {"Authorization": "Bearer " + etok}
        assert client.post("/api/expenses", json={"branch": "Store A", "category": "Rent",
                                                  "amount": 5}, headers=eh).status_code == 403
        assert client.get("/api/payroll?start=2026-07-01&end=2026-07-31",
                          headers=eh).status_code == 403
        assert client.get("/api/reports/kpi?period=month&branch=all",
                          headers=eh).json().get("can_view_profit") is False

        # branch scope: the inventory manager is limited to Store A
        itok = client.post("/api/telegram/auth-token", json={"tg_id": "96002"},
                           headers=_bot()).json()["access_token"]
        ih = {"Authorization": "Bearer " + itok}
        assert client.post("/api/inventory/receive", json={"sku": "RAW-CLS", "branch": "Store B",
                                                           "qty": 1}, headers=ih).status_code == 403
        assert client.post("/api/inventory/receive", json={"sku": "RAW-CLS", "branch": "Store A",
                                                           "qty": 1}, headers=ih).status_code == 200


def test_management_endpoints_require_privilege():
    with TestClient(app):
        assert client.get("/api/telegram/accounts").status_code == 401
        for uid in ("U-emp", "U-cash", "U-inv", "U-bm"):
            assert client.get("/api/telegram/accounts", headers=_tok(uid)).status_code == 403, uid
        assert client.post("/api/telegram/accounts/90000/disable",
                           headers=_tok("U-bm")).status_code == 403
        assert client.get("/api/telegram/accounts", headers=_tok("U-acct")).status_code == 200


def test_audit_records_full_telegram_context():
    with TestClient(app):
        r = client.post("/api/telegram/audit", headers=_bot(), json={
            "tg_id": "90000", "user_id": "U-owner", "action": "create", "entity": "expense",
            "ref": "L-1", "detail": "Utilities 50", "result": "ok",
            "tg_username": "user0", "branch": "Store A", "role": "owner", "ip": "1.2.3.4"})
        assert r.status_code == 200
        act = client.get("/api/telegram/accounts/90000/activity", headers=_tok("U-owner")).json()
        assert act["account"]["tg_id"] == "90000"
        e = next(x for x in act["entries"] if x["ref"] == "L-1")
        for k in ("ts", "action", "branch", "role", "ip", "tg_username", "result"):
            assert e[k] is not None, k


def test_concurrent_sessions_are_isolated():
    """Many employees using the bot at once must not cross over."""
    with TestClient(app):
        results = {}

        def worker(idx, uid):
            tg = str(90000 + idx)
            r = client.post("/api/telegram/auth-token", json={"tg_id": tg}, headers=_bot())
            if r.status_code != 200:
                results[tg] = ("ERR", r.status_code)
                return
            body = r.json()
            results[tg] = (body["user"]["id"], body["user"]["role"])

        # use only accounts that are currently linked and active
        alive = {a["tg_id"]: a["user_id"] for a in
                 client.get("/api/telegram/accounts?status=active", headers=_tok("U-owner")).json()}
        live = [(int(tg) - 90000, uid) for tg, uid in alive.items() if tg.startswith("9000")]
        ts = [threading.Thread(target=worker, args=(i, u)) for i, u in live]
        [t.start() for t in ts]
        [t.join() for t in ts]
        for i, uid in live:
            got = results[str(90000 + i)]
            assert got[0] == uid, f"session bleed: {90000+i} resolved to {got[0]} not {uid}"


def test_backward_compatibility_legacy_row_without_status():
    """A link created before this upgrade (status NULL) must keep working."""
    with TestClient(app):
        db = SessionLocal()
        try:
            db.add(models.TelegramLink(tg_id="70001", user_id="U-admin", username="legacy",
                                       linked_at=datetime.now(timezone.utc),
                                       last_activity=datetime.now(timezone.utc)))
            db.commit()
            db.execute(models.TelegramLink.__table__.update()
                       .where(models.TelegramLink.tg_id == "70001").values(status=None))
            db.commit()
        finally:
            db.close()
        assert client.get("/api/telegram/session/70001").json()["linked"] is True
        assert client.post("/api/telegram/auth-token", json={"tg_id": "70001"},
                           headers=_bot()).status_code == 200
        row = next(a for a in client.get("/api/telegram/accounts", headers=_tok("U-owner")).json()
                   if a["tg_id"] == "70001")
        assert row["status"] == "active", "legacy row must be treated as active"


def test_existing_commands_still_work_for_a_linked_user():
    """The bot's existing flows (session, prefs, clock-in) are unchanged."""
    with TestClient(app):
        assert client.get("/api/telegram/session/90000").json()["linked"] is True
        tok = client.post("/api/telegram/auth-token", json={"tg_id": "90000"},
                          headers=_bot()).json()["access_token"]
        h = {"Authorization": "Bearer " + tok, "Content-Type": "application/json"}
        assert client.get("/api/reports/dashboard?branch=all", headers=h).status_code == 200
        assert client.get("/api/expenses?branch=all", headers=h).status_code == 200
        assert client.get("/api/purchases?branch=all", headers=h).status_code == 200
        assert client.put("/api/telegram/prefs", json={"low_stock": False}, headers=h).status_code == 200
        client.put("/api/attendance/branch/Store A", headers=h,
                   json={"lat": 32.2211, "lng": 35.2544, "radius_m": 150})
        assert client.post("/api/attendance/clock-in",
                           json={"lat": 32.2213, "lng": 35.2544, "live": True},
                           headers=h).status_code == 200

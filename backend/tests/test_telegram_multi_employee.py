"""THE PRODUCTION BUG: one owner links four employees from his own session.

This reproduces exactly what a real operator does — sign in as the owner, open
the Telegram Management Center, and link Owner, Manager, Cashier and Employee
one after another. Before the fix the second link deleted the first, because the
link code was bound to the SIGNED-IN USER rather than to a chosen employee.

Every test here must fail if linking account N ever removes or mutates account
N-1.
"""
import os
import tempfile
import threading

_DB = os.path.join(tempfile.gettempdir(), f"smokestack_tgme_{os.getpid()}.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["SEED_ON_START"] = "true"
os.environ["JWT_SECRET"] = "tg-multi-employee-secret-long"
os.environ["TELEGRAM_BOT_TOKEN"] = "test-bot-token"

from fastapi.testclient import TestClient
from app.main import app
from app.database import SessionLocal
from app import models
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


# the four people from the brief: Owner, Manager, Cashier, Employee
CAST = [("TG-OWNER", "Test Owner", "Store A", "owner"),
        ("TG-MGR", "Test Manager", "Store B", "branch_manager"),
        ("TG-CASH", "Test Cashier", "Store A", "cashier"),
        ("TG-EMP", "Test Employee", "Store C", "employee"),
        ("TG-SPARE", "Test Spare", "Store A", "employee")]


def _ensure_cast():
    h = _tok()
    for eid, name, branch, role in CAST:
        client.post("/api/employees", headers=h, json={
            "id": eid, "name": name, "branch": branch, "title": "Staff",
            "pay_type": "salary", "salary": 1000, "role": role})
    return [e for e in client.get("/api/employees?branch=all", headers=h).json()
            if e["id"].startswith("TG-")]


def _employees():
    return _ensure_cast()


def _link_employee(emp_id, tg_id, username, actor="U-owner"):
    """The real flow: the OWNER mints a code for a chosen employee, then that
    employee's Telegram account redeems it."""
    r = client.post("/api/telegram/link-code", headers=_tok(actor),
                    json={"employee_id": emp_id})
    assert r.status_code == 200, r.text
    code = r.json()["code"]
    return client.post("/api/telegram/link/verify",
                       json={"tg_id": str(tg_id), "code": code, "username": username})


def _accounts():
    return client.get("/api/telegram/accounts", headers=_tok()).json()


def test_owner_links_four_employees_and_none_disappear():
    """THE regression test. Link four people from one owner session."""
    with TestClient(app):
        emps = _employees()
        assert len(emps) >= 4, "need at least four employees to run the real test"
        four = emps[:4]
        tg_ids = ["50001", "50002", "50003", "50004"]

        for i, (emp, tg) in enumerate(zip(four, tg_ids)):
            r = _link_employee(emp["id"], tg, f"tg_user_{i}")
            assert r.status_code == 200, f"linking {emp['name']} failed: {r.text}"

            # after EVERY link, every previously linked account must still exist
            accts = {a["tg_id"]: a for a in _accounts()}
            for prev_tg, prev_emp in zip(tg_ids[:i + 1], four[:i + 1]):
                assert prev_tg in accts, (
                    f"REGRESSION: linking {emp['name']} removed the account {prev_tg} "
                    f"belonging to {prev_emp['name']}")
                assert accts[prev_tg]["employee_id"] == prev_emp["id"], (
                    f"REGRESSION: account {prev_tg} was re-pointed at another employee")
                assert accts[prev_tg]["status"] == "active"

        final = _accounts()
        assert len([a for a in final if a["tg_id"] in tg_ids]) == 4, \
            f"expected four linked accounts, got {[a['tg_id'] for a in final]}"


def test_each_account_maps_to_its_own_employee():
    with TestClient(app):
        accts = {a["tg_id"]: a for a in _accounts()}
        emp_ids = [accts[t]["employee_id"] for t in ("50001", "50002", "50003", "50004")]
        assert len(set(emp_ids)) == 4, f"employees must be distinct, got {emp_ids}"
        for t in ("50001", "50002", "50003", "50004"):
            assert accts[t]["employee"], f"{t} has no employee name"
            assert accts[t]["user_id"], f"{t} has no session identity"


def test_four_accounts_work_simultaneously_with_separate_sessions():
    """All four must hold live, independent bot sessions at the same time."""
    with TestClient(app):
        results = {}

        def worker(tg):
            r = client.post("/api/telegram/auth-token", json={"tg_id": tg}, headers=_bot())
            results[tg] = (r.status_code,
                           r.json().get("user", {}).get("id") if r.status_code == 200 else None)

        ts = [threading.Thread(target=worker, args=(t,))
              for t in ("50001", "50002", "50003", "50004")]
        [t.start() for t in ts]
        [t.join() for t in ts]

        for tg, (code, uid) in results.items():
            assert code == 200, f"{tg} could not open a session: {code}"
        ids = [uid for _, uid in results.values()]
        assert len(set(ids)) == 4, f"sessions bled into each other: {ids}"


def test_separate_permissions_and_branch_access():
    """Each Telegram session inherits its own employee's role and branches."""
    with TestClient(app):
        accts = {a["tg_id"]: a for a in _accounts()}
        seen = {}
        for tg in ("50001", "50002", "50003", "50004"):
            tok = client.post("/api/telegram/auth-token", json={"tg_id": tg},
                              headers=_bot()).json()["access_token"]
            h = {"Authorization": "Bearer " + tok}
            me = client.get("/api/auth/me", headers=h).json()
            seen[tg] = (me["role"], tuple(sorted(me.get("branches") or [])))
            # the session's branches must match the account row the UI displays
            assert set(me.get("branches") or []) == set(accts[tg]["branches"] or []), tg
        # not everyone is an owner: roles/branches actually differ per employee
        assert len(set(seen.values())) > 1, f"all sessions identical: {seen}"


def test_separate_audit_logs():
    with TestClient(app):
        for i, tg in enumerate(("50001", "50002")):
            r = client.post("/api/telegram/audit", headers=_bot(), json={
                "tg_id": tg, "action": "clock_in", "entity": "attendance",
                "ref": f"A-{i}", "detail": "test", "result": "ok",
                "tg_username": f"tg_user_{i}", "branch": "Store A",
                "role": "employee", "ip": "telegram"})
            assert r.status_code == 200, r.text
        a1 = client.get("/api/telegram/accounts/50001/activity", headers=_tok()).json()
        a2 = client.get("/api/telegram/accounts/50002/activity", headers=_tok()).json()
        refs1 = {e["ref"] for e in a1["entries"]}
        refs2 = {e["ref"] for e in a2["entries"]}
        assert "A-0" in refs1 and "A-0" not in refs2, "audit trails must not mix"
        assert "A-1" in refs2 and "A-1" not in refs1, "audit trails must not mix"


def test_linking_never_updates_or_deletes_an_existing_row():
    """Linking must be INSERT-only: existing rows are byte-for-byte untouched."""
    with TestClient(app):
        db = SessionLocal()
        try:
            before = {l.tg_id: (l.user_id, l.employee_id, l.status, str(l.linked_at))
                      for l in db.query(models.TelegramLink).all()}
        finally:
            db.close()

        emps = _employees()
        target = emps[4] if len(emps) > 4 else emps[0]
        # a fresh employee link, or a rejection — either way nothing else may change
        _link_employee(target["id"], "50099", "tg_user_new")

        db = SessionLocal()
        try:
            after = {l.tg_id: (l.user_id, l.employee_id, l.status, str(l.linked_at))
                     for l in db.query(models.TelegramLink).all()}
        finally:
            db.close()
        for tg, row in before.items():
            assert tg in after, f"REGRESSION: row {tg} was deleted by a later link"
            assert after[tg] == row, f"REGRESSION: row {tg} was mutated by a later link"


def test_telegram_id_is_globally_unique():
    with TestClient(app):
        emps = _employees()
        other = next(e for e in emps if e["id"] != _accounts()[0]["employee_id"])
        r = _link_employee(other["id"], "50001", "thief")
        assert r.status_code == 409, "a Telegram id must never be re-pointed"
        # and the original row is intact
        accts = {a["tg_id"]: a for a in _accounts()}
        assert accts["50001"]["username"] == "tg_user_0"


def test_employee_may_hold_only_one_active_account():
    with TestClient(app):
        accts = _accounts()
        taken = next(a for a in accts if a["tg_id"] == "50002")
        r = _link_employee(taken["employee_id"], "50777", "second_device")
        assert r.status_code == 409, "an employee must not hold two active accounts"
        # freeing the slot makes it work, and still does not touch anyone else
        n_before = len(_accounts())
        assert client.post("/api/telegram/accounts/50002/disable",
                           headers=_tok()).status_code == 200
        r = _link_employee(taken["employee_id"], "50777", "second_device")
        assert r.status_code == 200, r.text
        accts = {a["tg_id"]: a for a in _accounts()}
        assert len(accts) == n_before + 1, "disable+link must ADD a row, not replace one"
        assert "50002" in accts and accts["50002"]["status"] == "disabled", \
            "the disabled row must be preserved as history"
        assert accts["50777"]["status"] == "active"
        # unrelated accounts untouched
        assert accts["50001"]["status"] == "active"
        assert accts["50003"]["status"] == "active"


def test_management_center_lists_every_row_not_just_the_first():
    """The UI endpoint must be a findMany, never a findFirst."""
    with TestClient(app):
        accts = _accounts()
        assert len(accts) >= 5, f"expected all rows, got {len(accts)}"
        db = SessionLocal()
        try:
            n = db.query(models.TelegramLink).count()
        finally:
            db.close()
        assert len(accts) == n, f"endpoint returned {len(accts)} of {n} rows"

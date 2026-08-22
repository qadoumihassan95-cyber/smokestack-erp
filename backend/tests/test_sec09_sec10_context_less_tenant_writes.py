"""SEC-09 + SEC-10 regression — a tenant-owned row written on a session with no
company context must not silently inherit Company 1.

These are ONE defect with two reported instances, and the shared cause is that
`company_id` carries `server_default="1"` on ~40 tables. When `tenancy._stamp_writes`
finds no company id, no system flag and no strict flag it falls through, the INSERT
omits the column, and the database supplies a **real, valid tenant**. Missing context
therefore does not produce an error or an obviously-empty row — it produces a
plausible row belonging to somebody else. A default that is a valid tenant cannot
fail closed.

**SEC-09 (CRITICAL)** — `routers/auth.py:login` takes `Depends(get_db)` and no user
dependency, so `db.info["company_id"]` is never set and every tenant's login and
failed-login rows landed in Company 1's audit log. Company 1's owner read who signed
in to Company 2 and when, through the ordinary `GET /api/audit`; Company 2 had no
login history at all. Always on: no bot token, no Telegram deployment, no client
cooperation.

**SEC-10 (CRITICAL)** — `/link/verify` is unauthenticated by design (a one-time code,
not a session). `telegram_links` IS registered as tenant-owned, so the row was written
with the default and then read back through perfectly-working tenant scoping: Company
1 saw Company 2's Telegram binding and could disable, inspect and permanently DELETE
it. The tell was in the same handler — `users` and `user_branches` are constructed
with an explicit company copied from the employee, and only `TelegramLink` was not.
Same handler, same session, two right and one wrong: the defect is per CONSTRUCTION
SITE, not per handler.

TESTS 5-7 ARE THREE MORE INSTANCES OF THE SAME CLASS, FOUND BY SWEEPING FOR IT RATHER
THAN BY BEING REPORTED. Fixing only what was reported would have left them:

* every Telegram clock-in `Attendance` row — payroll-relevant — filed as Company 1;
* the two `AuditLog` rows in `noactivity.reconcile`, built directly rather than through
  `S.audit()`, so the chokepoint fix does not reach them. One of those sits **two
  lines below** the incident stamping added for HIGH-10 in the previous candidate:
  the row was stamped and the audit row describing it was not.

WHY THE FIX IS AT `S.audit()` AND NOT AT THE LOGIN ROUTE. The auditor measured 11 of
29 no-auth routes as able to write `audit_log` with no company context, all through
that one function. Per-route stamping closes eleven and leaves the twelfth to whoever
adds it. The auditor's own fix direction says the same: "not 'stamp the audit
handler'".

WHAT THIS DOES **NOT** DO. It does not remove `server_default="1"`, and it does not
make unresolved context refuse — that is the auditor's item 18, it spans 66 sites
including the migration layer, and applying "refuse when unresolvable" naively breaks
every bot route and linking entirely (the derive-don't-require constraint). Test 4
pins the honest scope: when a tenant genuinely cannot be resolved, the row is written
with an explicit SQL NULL rather than being handed to the default.
"""
import os
import tempfile
from datetime import datetime, timedelta, timezone

_DB = os.path.join(tempfile.gettempdir(), f"sec0910_ctx_{os.getpid()}.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_DB}")
os.environ.setdefault("SEED_ON_START", "true")
os.environ.setdefault("SEED_DEMO_DATA", "true")
os.environ.setdefault("JWT_SECRET", "sec0910-secret-long-enough-for-tests")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-bot-token")

from fastapi.testclient import TestClient                      # noqa: E402

from app.main import app                                       # noqa: E402
from app import attendance_evidence as AE                      # noqa: E402
from app import models, noactivity as NA, security, tenancy    # noqa: E402

with TestClient(app):
    pass
client = TestClient(app)

C2 = 2
C2_OWNER = "K-owner"
C2_PW = "Kilo-Pass-7742"
C2_EMP = "K-EMP-1"
C2_STAFF = "K-staff"   # the fixture link belongs here, so the OWNER is free to link in test 5
C2_BRANCH = "Kilo Depot"
C2_TG = "660000010"
PW = "demo1234"


def setup_module(_m):
    with tenancy.system_session() as db:
        if not db.query(models.Company).filter(models.Company.id == C2).first():
            db.add(models.Company(id=C2, name="Kilo Co", slug="kilo-co-sec0910",
                                  application_key="smoke_shop",
                                  owner_user_id=C2_OWNER, status="active"))
        db.commit()
    with tenancy.tenant_session(C2) as db:
        if not db.get(models.User, C2_OWNER):
            db.add(models.User(id=C2_OWNER, name="Kilo Owner", role="owner",
                               password_hash=security.hash_pw(C2_PW),
                               status="active", can_login=True))
        if not db.get(models.User, C2_STAFF):
            db.add(models.User(id=C2_STAFF, name="Kilo Staff User", role="employee",
                               password_hash=security.hash_pw(C2_PW),
                               status="active", can_login=True))
        if not db.query(models.Branch).filter(models.Branch.name == C2_BRANCH).first():
            db.add(models.Branch(name=C2_BRANCH, timezone="UTC",
                                 attendance_active=True, loc_verify=False))
        if not db.get(models.Employee, C2_EMP):
            db.add(models.Employee(id=C2_EMP, name="Kilo Staff", branch=C2_BRANCH,
                                   title="Staff", salary=1000, active=True))
        db.commit()
    with tenancy.system_session() as db:
        if not db.get(models.TelegramLink, C2_TG):
            link = models.TelegramLink(tg_id=C2_TG, user_id=C2_STAFF,
                                       username="kilo", status="active")
            link.company_id = C2
            db.add(link)
        db.commit()


def teardown_module(_m):
    with tenancy.system_session() as db:
        db.query(models.AttendanceEvidence).filter(
            models.AttendanceEvidence.tg_id == C2_TG).delete(synchronize_session=False)
        db.query(models.Attendance).filter(
            models.Attendance.user_id.in_([C2_OWNER, C2_STAFF])).delete(synchronize_session=False)
        db.query(models.NoActivityIncident).filter(
            models.NoActivityIncident.branch.in_([C2_BRANCH, "Kilo Idle Depot"])).delete(
            synchronize_session=False)
        db.query(models.TelegramLink).filter(
            models.TelegramLink.tg_id == C2_TG).delete(synchronize_session=False)
        db.query(models.UserBranch).filter(
            models.UserBranch.branch == C2_BRANCH).delete(synchronize_session=False)
        db.query(models.Branch).filter(
            models.Branch.name.in_([C2_BRANCH, "Kilo Idle Depot"])).delete(
            synchronize_session=False)
        db.query(models.RateHit).delete()
        db.commit()


def _clear_throttle():
    with tenancy.system_session() as db:
        db.query(models.RateHit).delete()
        db.commit()


def _audit_rows(user_id):
    with tenancy.system_session() as db:
        return [(a.action, a.company_id) for a in db.query(models.AuditLog)
                .filter(models.AuditLog.user_id == user_id).all()]


def _audit_for_ref(ref):
    with tenancy.system_session() as db:
        return [(a.action, a.company_id) for a in db.query(models.AuditLog)
                .filter(models.AuditLog.ref == ref).all()]


# ===========================================================================
# SEC-09 — login and failed-login rows belong to the account's tenant.
# ===========================================================================
def test_0_a_successful_login_is_audited_to_the_users_own_company():
    _clear_throttle()
    r = client.post("/api/auth/login", data={"username": C2_OWNER, "password": C2_PW})
    assert r.status_code == 200, r.text

    rows = [(a, c) for a, c in _audit_rows(C2_OWNER) if a == "login"]
    assert rows, "the login was not audited at all"
    assert all(c == C2 for _a, c in rows), (
        f"a company-{C2} login was recorded in company {[c for _a, c in rows]}'s audit "
        f"log. login() has no user dependency, so the session carries no company "
        f"context and the row takes the company_id server default of 1.")


def test_1_a_failed_login_for_a_real_account_is_audited_to_that_accounts_tenant():
    """`user=None` is deliberate — a failed attempt is not attributed to an account —
    but the EVENT still belongs to the tenant whose account was targeted."""
    _clear_throttle()
    r = client.post("/api/auth/login",
                    data={"username": C2_OWNER, "password": "definitely-wrong"})
    assert r.status_code == 401, r.text

    with tenancy.system_session() as db:
        rows = [a.company_id for a in db.query(models.AuditLog)
                .filter(models.AuditLog.action == "failed_login",
                        models.AuditLog.ref == C2_OWNER).all()]
    assert rows, "the failed login was not audited"
    assert all(c == C2 for c in rows), (
        f"failed logins against a company-{C2} account were filed under company {rows}")


def test_2_company_1_cannot_read_company_2s_login_history():
    """The consequence, through the shipped endpoint rather than the table."""
    _clear_throttle()
    client.post("/api/auth/login", data={"username": C2_OWNER, "password": C2_PW})

    tok = client.post("/api/auth/login",
                      data={"username": "U-owner", "password": PW}).json()["access_token"]
    seen = client.get("/api/audit?limit=200", headers={"Authorization": "Bearer " + tok})
    assert seen.status_code == 200, seen.text
    leaked = [a for a in seen.json()
              if (a.get("user_id") == C2_OWNER or a.get("ref") == C2_OWNER)]
    assert not leaked, (
        f"company 1's owner read {len(leaked)} audit rows about company {C2}'s owner "
        f"through GET /api/audit")


def test_3_company_2_can_read_its_own_login_history():
    """Discriminating control. If the rows had merely been hidden from everyone, test
    2 would pass while the tenant still had no login history at all — which is the
    other half of what SEC-09 described."""
    _clear_throttle()
    client.post("/api/auth/login", data={"username": C2_OWNER, "password": C2_PW})

    tok = client.post("/api/auth/login",
                      data={"username": C2_OWNER, "password": C2_PW}).json()["access_token"]
    mine = client.get("/api/audit?limit=200", headers={"Authorization": "Bearer " + tok})
    assert mine.status_code == 200, mine.text
    own = [a for a in mine.json() if a.get("action") == "login"]
    assert own, (
        f"company {C2} has no login history of its own — the rows were removed from "
        f"company 1 without arriving anywhere")


def test_4_an_unresolvable_subject_yields_NULL_not_a_real_company():
    """The honest scope of this fix, pinned.

    A failed login for a username that matches no account belongs to no tenant. It
    must not be filed under one. This does NOT remove `server_default="1"` — that is
    a 66-site change spanning the migration layer — so the column is set to an
    explicit SQL NULL, because leaving the attribute as Python `None` makes SQLAlchemy
    omit it from the INSERT and the default wins anyway.
    """
    _clear_throttle()
    r = client.post("/api/auth/login",
                    data={"username": "no-such-account-sec09", "password": "wrong"})
    assert r.status_code == 401

    with tenancy.system_session() as db:
        rows = [a.company_id for a in db.query(models.AuditLog)
                .filter(models.AuditLog.ref == "no-such-account-sec09").all()]
    assert rows, "the failed login was not audited"
    assert all(c is None for c in rows), (
        f"a failed login for a nonexistent username was filed under company {rows}; "
        f"an unattributable event must not inherit a real tenant")


# ===========================================================================
# SEC-10 — the Telegram binding belongs to the employee's tenant.
# ===========================================================================
def test_5_a_link_created_by_company_2_is_owned_by_company_2():
    """`/link/verify` end to end: company 2's owner issues a code, the account links,
    and the binding must be company 2's."""
    _clear_throttle()
    tok = client.post("/api/auth/login",
                      data={"username": C2_OWNER, "password": C2_PW}).json()["access_token"]
    h = {"Authorization": "Bearer " + tok}

    code = client.post("/api/telegram/link-code", headers=h)
    assert code.status_code == 200, code.text
    tg = "660000011"
    v = client.post("/api/telegram/link/verify",
                    json={"code": code.json()["code"], "tg_id": tg, "username": "kilo2"})
    assert v.status_code == 200, v.text

    try:
        with tenancy.system_session() as db:
            link = db.get(models.TelegramLink, tg)
            assert link is not None, "the link was not created"
            assert link.company_id == C2, (
                f"a company-{C2} Telegram binding was created as company "
                f"{link.company_id}'s. `users` and `user_branches` in the same handler "
                f"are stamped correctly; only TelegramLink was not.")

        # CONSEQUENCE, through the shipped endpoints: company 1 must not see it…
        c1 = client.post("/api/auth/login",
                         data={"username": "U-owner", "password": PW}).json()["access_token"]
        h1 = {"Authorization": "Bearer " + c1}
        accts = client.get("/api/telegram/accounts", headers=h1)
        assert accts.status_code == 200, accts.text
        ids = [a.get("tg_id") for a in (accts.json() if isinstance(accts.json(), list)
                                        else accts.json().get("accounts", []))]
        assert tg not in ids, "company 1 can see company 2's Telegram binding"

        # …and must not be able to destroy it.
        client.delete(f"/api/telegram/accounts/{tg}", headers=h1)
        with tenancy.system_session() as db:
            assert db.get(models.TelegramLink, tg) is not None, (
                "company 1 permanently DELETED company 2's Telegram binding")
    finally:
        with tenancy.system_session() as db:
            db.query(models.TelegramLink).filter(
                models.TelegramLink.tg_id == tg).delete(synchronize_session=False)
            db.commit()


# ===========================================================================
# The class — three instances found by sweeping, not by being reported.
# ===========================================================================
def test_6_a_bot_driven_clock_in_records_attendance_under_the_employees_tenant():
    """`attendance` is a tenant table and `submit_selfie` runs on the bot path: a bot
    token, no session, no company context. Every Telegram clock-in, for every tenant,
    was recorded as Company 1's attendance — payroll-relevant data under the wrong
    company. Not reported; found by sweeping SEC-09's class."""
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (40, 90, 200)).save(buf, format="PNG")
    png = buf.getvalue()

    now = datetime.now(timezone.utc)
    with tenancy.system_session() as db:
        ev = models.AttendanceEvidence(
            attempt_id="sec0910-clockin", employee_id=C2_STAFF, employee_name="Kilo Staff User",
            tg_id=C2_TG, branch=C2_BRANCH, status="pending_selfie",
            lat=32.0, lng=35.0, dist_m=4, out_of_area=False,
            created_at=now, expires_at=now + timedelta(minutes=30))
        ev.company_id = C2
        db.add(ev)
        db.commit()
        attempt = ev.attempt_id

    with tenancy.system_session() as db:
        _ev, rec = AE.submit_selfie(db, C2_TG, attempt, file_id="f1", msg_id="m1",
                                    mime="image/png", raw_bytes=png)
        aid = rec.id

    with tenancy.system_session() as db:
        att = db.get(models.Attendance, aid)
        assert att.company_id == C2, (
            f"a company-{C2} employee's Telegram clock-in was recorded as company "
            f"{att.company_id}'s attendance")


def test_7_the_inactivity_scans_audit_rows_carry_the_branchs_tenant():
    """`noactivity.reconcile` builds its AuditLog rows DIRECTLY rather than through
    `S.audit()`, so the chokepoint fix does not reach them — and one of them sits two
    lines below the incident stamping added for HIGH-10 in the previous candidate. The
    incident was stamped; the audit row describing it was not."""
    now = datetime.now(timezone.utc)
    # A branch of its own, with no activity on it. Reusing C2_BRANCH made this test
    # skip itself: test 6 clocks somebody IN there, `last_activity` then returns a
    # recent timestamp, the branch is judged active and no incident opens. Two
    # correct behaviours combining into a test that silently measured nothing.
    idle_branch = "Kilo Idle Depot"
    with tenancy.tenant_session(C2) as db:
        if not db.query(models.Branch).filter(models.Branch.name == idle_branch).first():
            db.add(models.Branch(name=idle_branch, timezone="UTC"))
        db.commit()
    with tenancy.system_session() as db:
        b = db.query(models.Branch).filter(models.Branch.name == idle_branch).first()
        # The threshold the evaluator actually reads is
        # `inactivity_threshold_hours` (noactivity.branch_schedule:73).
        b.inactivity_threshold_hours = 1
        b.inactivity_alert_enabled = True
        db.commit()
    with tenancy.system_session() as db:
        branches = db.query(models.Branch).filter(models.Branch.name == idle_branch).all()
        NA.reconcile(db, branches, now, "UTC")
        db.commit()

    rows = _audit_for_ref(idle_branch)
    opened = [c for a, c in rows if a == "no_activity_incident_opened"]
    assert opened, (
        "no incident was opened, so this assertion did not run. This used to be a "
        "pytest.skip(); it is an assert because a test that quietly opts out is "
        "indistinguishable from a passing one in a suite summary.")
    assert all(c == C2 for c in opened), (
        f"audit rows about a company-{C2} branch's inactivity were filed under "
        f"company {opened}")


def test_8_the_authenticated_path_is_untouched():
    """The fix must only act where the session has NO context. On an ordinary
    authenticated request the scoping engine already stamps correctly, and this must
    not start second-guessing it."""
    tok = client.post("/api/auth/login",
                      data={"username": "U-owner", "password": PW}).json()["access_token"]
    r = client.post("/api/purchases", headers={"Authorization": "Bearer " + tok},
                    json={"vendor": "Sec0910", "branch": "Store A", "amount": 42})
    assert r.status_code == 201, r.text
    pid = r.json()["id"]

    rows = _audit_for_ref(pid)
    assert rows, "the authenticated mutation was not audited"
    assert all(c == 1 for _a, c in rows), (
        f"an authenticated company-1 mutation was audited under company "
        f"{[c for _a, c in rows]}")

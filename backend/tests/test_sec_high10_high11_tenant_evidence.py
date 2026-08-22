"""SEC HIGH-10 + HIGH-11 regression — employee selfies, GPS coordinates, inactivity
incidents and chat images belong to the tenant that produced them, and the guards on
those endpoints must consult the tenant and not only the branch name.

HIGH-10. `attendance_evidence`, `no_activity_incidents` and `chat_attachments` sat in
`tenancy.TENANT_EXEMPT` marked "UNREVIEWED — inherited from baseline", so `_stamp_writes`
skipped them, and NONE of their three creation sites set `company_id`. Every row took the
`company_id` SERVER DEFAULT of 1 whichever tenant created it: company 2's clock-in
selfies and GPS coordinates, its chat images and its inactivity incidents were all
stored as company 1's data. Any per-tenant export, retention or deletion request, backup
segregation, or company_id-keyed query then operated on the wrong tenant.

HIGH-11. Those endpoints' guards are branch-NAME only. `/api/attendance/evidence/{eid}/selfie`
does an unscoped `db.get` on a global autoincrement id and then checks
`ev.branch in scope_branches(user)`; `/api/reports/no-activity-alerts` and its
`.../acknowledge` filter on `branch.in_(scope)`. Neither consulted `company_id`. That
held ONLY because `branches.name` is a global primary key today — and MEDIUM-03's fix
removes exactly that. Tests 4-6 force the rows into the post-MEDIUM-03 world (a
correctly-tenanted company-2 row carrying a branch name company 1 holds) so the branch
name is the only remaining variable.

**This is the ordering constraint on MEDIUM-03, and it is why MEDIUM-03 is a plan and
not a change.** Composite `(company_id, name)` branch keys make two tenants sharing
"Store A" legal, which is precisely the precondition tests 4-5 exploit. Landing composite
keys before these guards are tenant-scoped converts a metadata oracle into cross-tenant
disclosure of employee selfies and GPS, and cross-tenant mutation of incidents. Guards
first, keys second. Tests 4-6 are the executable form of that constraint: they must be
green before any composite-key migration is written.

DERIVED FROM the auditor's preserved probe
`RESEARCH/security-probes/smokestack-3eb1ccd/test_sec_probe_e_branchkey.py` (revision 2),
which already asserts the SECURE property and therefore fails at `3eb1ccd`. Tests 1 and 2
here are NOT in that probe and are the ones that matter most for regression: the probe
hand-builds its rows inside an ORM tenant session, which `_stamp_writes` would stamp
correctly on its own. The REAL creation paths are bot-driven — a bot token, no session,
no company context — so registering the tables is not sufficient there and the two
creation sites had to set the tenant explicitly. A test that only exercised the ORM path
would go green while the shipped path stayed broken.
"""
import io
import os
import tempfile
from datetime import datetime, timezone

_DB = os.path.join(tempfile.gettempdir(), f"sec_h10_evidence_{os.getpid()}.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_DB}")
os.environ.setdefault("SEED_ON_START", "true")
os.environ.setdefault("SEED_DEMO_DATA", "true")
os.environ.setdefault("JWT_SECRET", "sec-h10-secret-long-enough-for-tests")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-bot-token")

from fastapi.testclient import TestClient                      # noqa: E402

from app.main import app                                       # noqa: E402
from app import (attendance_evidence as AE, models, noactivity as NA,  # noqa: E402
                 security, tenancy)

with TestClient(app):
    pass
client = TestClient(app)

C2 = 2
C2_OWNER = "E-owner"
C2_PW = "Echo-Pass-3312"
C2_STAFF = "E-staff"
C2_TG = "880000002"
E_BRANCH = "Echo Depot"        # company 2's own branch
SHARED = "Store A"             # a branch name COMPANY 1 holds (seeded)
SECRET_SELFIE = b"ECHO-PRIVATE-SELFIE-BYTES"
IDS = {}


def setup_module(_m):
    with tenancy.system_session() as db:
        if not db.query(models.Company).filter(models.Company.id == C2).first():
            db.add(models.Company(id=C2, name="Echo Co", slug="echo-co-h10",
                                  application_key="smoke_shop",
                                  owner_user_id=C2_OWNER, status="active"))
        db.commit()
    with tenancy.tenant_session(C2) as db:
        if not db.get(models.User, C2_OWNER):
            db.add(models.User(id=C2_OWNER, name="Echo Owner", role="owner",
                               password_hash=security.hash_pw(C2_PW),
                               status="active", can_login=True))
        if not db.get(models.User, C2_STAFF):
            db.add(models.User(id=C2_STAFF, name="Echo Staff", role="employee",
                               password_hash=security.hash_pw(C2_PW),
                               status="active", can_login=True))
        if not db.query(models.Branch).filter(models.Branch.name == E_BRANCH).first():
            db.add(models.Branch(name=E_BRANCH, timezone="UTC",
                                 attendance_active=True, loc_verify=False))
        db.commit()
    with tenancy.tenant_session(C2) as db:
        if not db.query(models.UserBranch).filter(
                models.UserBranch.user_id == C2_STAFF).first():
            db.add(models.UserBranch(user_id=C2_STAFF, branch=E_BRANCH))
        db.commit()
    with tenancy.system_session() as db:
        if not db.get(models.TelegramLink, C2_TG):
            link = models.TelegramLink(tg_id=C2_TG, user_id=C2_STAFF,
                                       username="echo_staff", status="active")
            link.company_id = C2
            db.add(link)
        db.commit()


def teardown_module(_m):
    with tenancy.system_session() as db:
        db.query(models.AttendanceEvidence).filter(
            models.AttendanceEvidence.tg_id == C2_TG).delete(synchronize_session=False)
        db.query(models.NoActivityIncident).filter(
            models.NoActivityIncident.branch.in_([E_BRANCH, SHARED])).delete(
            synchronize_session=False)
        db.query(models.TelegramLink).filter(
            models.TelegramLink.tg_id == C2_TG).delete(synchronize_session=False)
        # One process-wide database: a branch left behind here appears in other
        # modules' branch-label and branch-count assertions.
        db.query(models.UserBranch).filter(
            models.UserBranch.branch == E_BRANCH).delete(synchronize_session=False)
        db.query(models.Branch).filter(models.Branch.name == E_BRANCH).delete(
            synchronize_session=False)
        db.commit()


def _h(uid, pw):
    r = client.post("/api/auth/login", data={"username": uid, "password": pw})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["access_token"]}


def _c1():
    return _h("U-owner", "demo1234")


def _c2():
    return _h(C2_OWNER, C2_PW)


def _png():
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (200, 40, 40)).save(buf, format="PNG")
    return buf.getvalue()


def _force(model, oid, **vals):
    with tenancy.system_session() as db:
        row = db.get(model, oid)
        assert row is not None, f"{model.__name__} {oid} vanished"
        for k, v in vals.items():
            setattr(row, k, v)
        db.commit()


# ===========================================================================
# HIGH-10 — the REAL creation paths, which are bot-driven and carry no session.
# ===========================================================================
def test_1_the_bot_driven_attendance_attempt_is_stamped_with_the_employees_tenant():
    """`start_attempt` runs on a session with NO company context — the caller
    presented a bot token, not a token. Registering the table in TENANT_TABLES
    does not help here, because `_stamp_writes` has nothing to apply. The tenant
    has to come from the employee the tg_id resolves to.
    """
    with tenancy.system_session() as db:
        ev, _first = AE.start_attempt(db, C2_TG)
        IDS["evidence"] = ev.id

    with tenancy.system_session() as db:
        row = db.get(models.AttendanceEvidence, IDS["evidence"])
        assert row.company_id == C2, (
            f"a company-{C2} employee's attendance attempt was stored with "
            f"company_id={row.company_id} (the column server default is 1). This is "
            f"the bot path: no session context, so only an explicit assignment works.")


def test_2_the_bot_driven_inactivity_scan_stamps_each_incident_to_its_branchs_tenant():
    """`reconcile` iterates EVERY company's branches on one system session. If the
    tenant is taken from the session there is none, and every tenant's incidents
    land in company 1 — including incidents opened against another tenant's branch.
    """
    now = datetime.now(timezone.utc)
    with tenancy.system_session() as db:
        # Force the branch inactive so an incident is certain to open.
        b = db.query(models.Branch).filter(models.Branch.name == E_BRANCH).first()
        # The threshold the evaluator actually reads is
        # `inactivity_threshold_hours` (noactivity.branch_schedule:73); setting a
        # differently-named field left the branch judged ACTIVE and this test
        # skipping itself, which is not evidence of anything.
        b.inactivity_threshold_hours = 1
        b.inactivity_alert_enabled = True
        b.attendance_active = True
        db.commit()
    with tenancy.system_session() as db:
        branches = db.query(models.Branch).filter(models.Branch.name == E_BRANCH).all()
        assert branches, "fixture branch missing"
        NA.reconcile(db, branches, now, "UTC")
        db.commit()

    with tenancy.system_session() as db:
        inc = (db.query(models.NoActivityIncident)
               .filter(models.NoActivityIncident.branch == E_BRANCH)
               .order_by(models.NoActivityIncident.id.desc()).first())
        if inc is None:
            # The branch was not judged inactive by the evaluator; say so instead of
            # passing silently on an assertion that never ran.
            import pytest
            pytest.skip("no incident opened — the inactivity evaluator did not fire; "
                        "this assertion did not run")
        IDS["incident"] = inc.id
        assert inc.company_id == C2, (
            f"an incident about a company-{C2} branch was stored as company "
            f"{inc.company_id}'s record")


def test_3_a_chat_attachment_uploaded_over_http_by_company2_is_stored_as_company2():
    """The third creation site, end to end through the real endpoint."""
    with tenancy.tenant_session(C2) as db:
        room = models.ChatRoom(kind="group", name="Echo Room", created_by=C2_OWNER)
        db.add(room)
        db.flush()
        db.add(models.ChatMember(room_id=room.id, user_id=C2_OWNER, role="admin"))
        db.commit()
        rid = room.id
        # CONTROL: a tenant-owned row in the same transaction IS stamped to 2, so a
        # failure below is about this table and not about the scoping engine.
        assert room.company_id == C2, f"chat_rooms not stamped: {room.company_id}"

    r = client.post(f"/api/chat/rooms/{rid}/attachments",
                    files={"file": ("echo.png", _png(), "image/png")},
                    data={"caption": "echo private"}, headers=_c2())
    assert r.status_code in (200, 201), f"upload failed (control): {r.status_code} {r.text[:300]}"

    with tenancy.system_session() as db:
        att = (db.query(models.ChatAttachment)
               .order_by(models.ChatAttachment.id.desc()).first())
        assert att is not None
        IDS["attachment"] = att.id
        assert att.company_id == C2, (
            f"a company-{C2} upload was stored with company_id={att.company_id}")


# ===========================================================================
# HIGH-11 — the guards must not be satisfied by a branch name alone.
# The rows below are CORRECTLY tenanted to company 2 and carry a branch name
# company 1 holds: the world composite branch keys would make legal.
# ===========================================================================
def test_4_a_correctly_tenanted_company2_selfie_is_not_served_to_company_1():
    """Two phases on one row, because the control and the attack need different
    branch names and conflating them makes the control unreadable.

    Phase 1 puts the row on company 2's OWN branch: company 2 must read it (the
    endpoint works) and company 1 must not (tenant isolation). Phase 2 moves it to
    a branch name company 1 holds — the post-MEDIUM-03 world — where the branch-name
    guard alone would now say yes. Company 1 must still be refused.

    The control cannot be run in phase 2: with the row on "Store A", company 2's own
    owner is legitimately out of scope for it, so a 403 there would say nothing.
    """
    eid = IDS.get("evidence")
    assert eid, "test 1 must run first"

    # --- phase 1: the row on company 2's own branch ---
    _force(models.AttendanceEvidence, eid, company_id=C2, branch=E_BRANCH,
           selfie=SECRET_SELFIE, selfie_mime="image/jpeg",
           selfie_at=datetime.now(timezone.utc), status="complete")

    own = client.get(f"/api/attendance/evidence/{eid}/selfie", headers=_c2())
    assert own.status_code == 200 and SECRET_SELFIE in own.content, (
        f"company 2 cannot read its OWN evidence ({own.status_code}) — the refusals "
        f"below would then be the endpoint being broken, not isolation")

    r = client.get(f"/api/attendance/evidence/{eid}/selfie", headers=_c1())
    assert r.status_code in (403, 404), f"company 1 reached company 2's selfie: {r.status_code}"

    # --- phase 2: the same row under a branch name company 1 holds ---
    _force(models.AttendanceEvidence, eid, company_id=C2, branch=SHARED)

    r = client.get(f"/api/attendance/evidence/{eid}/selfie", headers=_c1())
    assert not (r.status_code == 200 and SECRET_SELFIE in (r.content or b"")), (
        "company 1 received a correctly-tenanted company-2 employee selfie because "
        "the two rows share a branch name — the guard is branch-name only")
    assert r.status_code in (403, 404), f"unexpected {r.status_code}"


def test_5_a_correctly_tenanted_company2_incident_is_neither_read_nor_mutated_by_company_1():
    iid = IDS.get("incident")
    if not iid:
        import pytest
        pytest.skip("test 2 did not produce an incident")
    _force(models.NoActivityIncident, iid, company_id=C2, branch=SHARED, status="open")

    read = client.get(f"/api/reports/no-activity-alerts?branch={SHARED}", headers=_c1())
    assert read.status_code == 200, read.text
    leaked = iid in [i.get("id") for i in read.json().get("items", [])]

    client.post(f"/api/reports/no-activity-alerts/{SHARED}/acknowledge", headers=_c1())
    with tenancy.system_session() as db:
        after = db.get(models.NoActivityIncident, iid).status

    assert not leaked, "company 1 read a correctly-tenanted company-2 incident"
    assert after == "open", (
        f"company 1 MUTATED a correctly-tenanted company-2 incident through a shared "
        f"branch name: open -> {after}")


def test_6_the_room_scoped_guard_stays_closed_discriminating_control():
    """`chat_attachments` is equally exempt from branch-name guards, but its guard
    is room membership and `chat_rooms` IS tenant-scoped. It must stay closed —
    which is what makes tests 4 and 5 specific to the branch-name guards rather
    than a blanket property of these three tables."""
    aid = IDS.get("attachment")
    assert aid, "test 3 must run first"
    _force(models.ChatAttachment, aid, company_id=C2)
    r = client.get(f"/api/chat/attachments/{aid}", headers=_c1())
    assert r.status_code != 200, f"chat attachment crossed tenants too: {r.status_code}"


# ===========================================================================
# The registry itself — because a hand-maintained list is the actual defect.
# ===========================================================================
def test_7_no_table_is_left_marked_unreviewed_in_the_exempt_set():
    """The three tables were exempt because someone wrote UNREVIEWED next to them
    and moved on, and the note was then read as a decision.

    This asserts the property rather than the three names: nothing may sit in
    TENANT_EXEMPT with an unreviewed marker. Adding a fourth such table fails here.

    The block is located by PARSING, not by searching for the name. The first draft
    used `src.index("TENANT_EXEMPT")` and landed on the phrase inside a comment in
    the TENANT_TABLES block above — a guard that fired on the prose explaining the
    rule rather than on the rule. Comments are not in the AST, so the assignment's
    line range comes from `ast` and only that range is then read as text.
    """
    import ast
    import inspect

    src = inspect.getsource(tenancy)
    tree = ast.parse(src)
    node = next((n for n in tree.body
                 if isinstance(n, ast.Assign)
                 and any(getattr(t, "id", None) == "TENANT_EXEMPT" for t in n.targets)), None)
    assert node is not None, "TENANT_EXEMPT is no longer a module-level assignment"
    lines = src.splitlines()
    block = "\n".join(lines[node.lineno - 1:node.end_lineno])

    for table in ("attendance_evidence", "chat_attachments", "no_activity_incidents"):
        assert table not in tenancy.TENANT_EXEMPT, (
            f"{table} is still exempt from tenant scoping")

    # The rule is about ENTRIES, not about the word. A first version asserted
    # `"UNREVIEWED" not in block` and immediately failed on the comment explaining
    # that nothing is unreviewed any more — a guard firing on its own rationale.
    # An offending line is one that both declares a table and carries the marker.
    marked = [ln.strip() for ln in block.splitlines()
              if "UNREVIEWED" in ln and ln.strip().startswith('"')]
    assert not marked, (
        f"a table is exempt from tenant scoping with an UNREVIEWED marker: {marked}. "
        f"An unreviewed entry in an exemption list reads to every later reader as a "
        f"decision that was made.")


def test_8_the_three_tables_are_registered_as_tenant_owned():
    for table in ("attendance_evidence", "chat_attachments", "no_activity_incidents"):
        assert table in tenancy.TENANT_TABLES, f"{table} is not tenant-scoped"
    assert not (tenancy.TENANT_TABLES & tenancy.TENANT_EXEMPT), (
        "a table is in both sets; which one wins is then an accident of iteration order")

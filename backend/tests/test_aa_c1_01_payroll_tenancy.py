"""AA-C1-01 regression: a payroll finalized by company 2 must produce a payroll
run, a ledger posting and audit evidence that ALL carry company 2.

`payroll_runs` was added in candidate 1 without being registered in
`tenancy.TENANT_TABLES`. The scoping engine therefore never stamped it, so the row
fell back to the column's server default of 1 while the posting and audit row
carried the authenticated company. The source document did not reconcile with what
it produced.

Runs on whatever engine DATABASE_URL points at, so the same file is the PostgreSQL
evidence when pointed at PG16.
"""
import os
import tempfile

_DB = os.path.join(tempfile.gettempdir(), f"smokestack_aac101_{os.getpid()}.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_DB}")
os.environ.setdefault("SEED_ON_START", "true")
os.environ.setdefault("SEED_DEMO_DATA", "true")
os.environ.setdefault("JWT_SECRET", "aa-c1-01-secret-long-enough-for-tests")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-bot-token")

from fastapi.testclient import TestClient          # noqa: E402

from app.main import app                            # noqa: E402
from app.database import SessionLocal               # noqa: E402
from app import models, security, tenancy           # noqa: E402

C2 = 2
C2_BRANCH = "C2 Store"
PW = os.getenv("SEED_PASSWORD", "demo1234")

with TestClient(app):
    pass
client = TestClient(app)


def setup_module(_m):
    """A second company with its own branch and one salaried employee."""
    db = SessionLocal()
    try:
        tenancy.use_system_context(db)
        if not db.query(models.Company).filter(models.Company.id == C2).first():
            db.add(models.Company(id=C2, name="Acme Two", slug="acme-two-payroll",
                                  application_key="smoke_shop",
                                  owner_user_id="U2-pay-owner", status="active"))
        if not db.get(models.User, "U2-pay-owner"):
            u = models.User(id="U2-pay-owner", name="Owner Two", role="owner",
                            password_hash=security.hash_pw(PW), status="active")
            u.company_id = C2
            db.add(u)
        db.commit()
    finally:
        db.close()

    db = SessionLocal()
    try:
        tenancy.set_session_company(db, C2)
        if not db.query(models.Branch).filter(models.Branch.name == C2_BRANCH).first():
            db.add(models.Branch(name=C2_BRANCH, display_name="Acme Two Store"))
        if not db.query(models.Employee).filter(models.Employee.id == "C2-EMP-1").first():
            db.add(models.Employee(id="C2-EMP-1", name="Two Staff", branch=C2_BRANCH,
                                   title="Staff", salary=3000, active=True))
        db.commit()
    finally:
        db.close()


def teardown_module(_m):
    """Remove this module's company-2 branch and employee.

    Test modules share one process-wide engine and database file, so a branch left
    behind here shows up in another module's branch-label assertions. Clean up what
    this module created rather than making unrelated tests tolerate it.
    """
    db = SessionLocal()
    try:
        tenancy.use_system_context(db)
        for e in db.query(models.Employee).filter(models.Employee.id == "C2-EMP-1").all():
            db.delete(e)
        for b in db.query(models.Branch).filter(models.Branch.name == C2_BRANCH).all():
            db.delete(b)
        db.commit()
    finally:
        db.close()


def _h():
    r = client.post("/api/auth/login", data={"username": "U2-pay-owner", "password": PW})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _rows(start, end):
    """Read back with a SYSTEM context so the assertion sees the raw company_id
    rather than a filtered view that could hide the defect."""
    db = SessionLocal()
    try:
        tenancy.use_system_context(db)
        memo = f"Payroll {start}->{end}"
        runs = db.query(models.PayrollRun).filter(
            models.PayrollRun.period_start == start).all()
        led = db.query(models.Ledger).filter(models.Ledger.memo == memo).all()
        aud = db.query(models.AuditLog).filter(
            models.AuditLog.entity == "payroll",
            models.AuditLog.ref == f"{start}_{end}").all()
        return runs, led, aud
    finally:
        db.close()


def test_company2_payroll_run_posting_and_audit_share_company_2():
    start, end = "2028-01-01", "2028-01-15"
    r = client.post("/api/payroll/finalize", headers=_h(),
                    params={"start": start, "end": end, "branch": C2_BRANCH})
    assert r.status_code == 200, r.text

    runs, led, aud = _rows(start, end)
    assert len(runs) == 1 and len(led) == 1 and len(aud) >= 1

    assert runs[0].company_id == C2, (
        f"AA-C1-01: payroll_runs.company_id={runs[0].company_id}, expected {C2} — "
        "the source document does not reconcile with its own posting")
    assert led[0].company_id == C2
    assert all(a.company_id == C2 for a in aud)

    # the three must agree with each other, not merely each be 'not 1'
    assert {runs[0].company_id, led[0].company_id, aud[0].company_id} == {C2}
    assert runs[0].branch == C2_BRANCH


def test_company2_blind_retry_is_refused_and_leaves_no_delta():
    start, end = "2028-02-01", "2028-02-15"
    hdr = _h()
    assert client.post("/api/payroll/finalize", headers=hdr,
                       params={"start": start, "end": end,
                               "branch": C2_BRANCH}).status_code == 200
    before = tuple(len(x) for x in _rows(start, end))

    for _ in range(3):
        dup = client.post("/api/payroll/finalize", headers=hdr,
                          params={"start": start, "end": end, "branch": C2_BRANCH})
        assert dup.status_code == 409, f"expected 409, got {dup.status_code}"

    assert tuple(len(x) for x in _rows(start, end)) == before, \
        "a refused company-2 retry left state behind"
    runs, led, aud = _rows(start, end)
    assert runs[0].company_id == C2 and led[0].company_id == C2


def test_company2_distinct_period_still_posts_under_company_2():
    start, end = "2028-03-01", "2028-03-15"
    assert client.post("/api/payroll/finalize", headers=_h(),
                       params={"start": start, "end": end,
                               "branch": C2_BRANCH}).status_code == 200
    runs, led, _ = _rows(start, end)
    assert len(runs) == 1 and runs[0].company_id == C2 and led[0].company_id == C2


def test_uniqueness_does_not_leak_across_companies():
    """The unique key is (company_id, branch, period_start, period_end). Two
    different companies finalizing the SAME branch name and period must BOTH
    succeed — a constraint that collided across tenants would be a availability
    defect disguised as integrity."""
    import datetime as _dt
    start, end = "2028-04-01", "2028-04-15"
    db = SessionLocal()
    try:
        tenancy.use_system_context(db)
        db.add(models.PayrollRun(company_id=1, branch=C2_BRANCH,
                                 period_start=_dt.date.fromisoformat(start),
                                 period_end=_dt.date.fromisoformat(end), gross=10))
        db.commit()
    finally:
        db.close()

    assert client.post("/api/payroll/finalize", headers=_h(),
                       params={"start": start, "end": end,
                               "branch": C2_BRANCH}).status_code == 200, \
        "company 2 was blocked by company 1's row for the same branch/period"

    db = SessionLocal()
    try:
        tenancy.use_system_context(db)
        rows = db.query(models.PayrollRun).filter(
            models.PayrollRun.period_start == start).all()
    finally:
        db.close()
    assert sorted(r.company_id for r in rows) == [1, C2]

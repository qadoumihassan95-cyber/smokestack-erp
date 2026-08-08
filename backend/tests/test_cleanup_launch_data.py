"""Launch cleanup (scripts/cleanup_launch_data.py) — guardrails + preservation.

Verifies the demo/test data wipe is safe for the client handoff:
  * dry-run changes nothing and reports counts only,
  * fails closed without a valid, active, non-default verified owner,
  * refuses to run while SEED_ON_START is on,
  * wipes demo operational rows (products, employees, ...) but PRESERVES
    schema/identity/RBAC: branches (keys + display names), users, user_branches,
  * CRITICAL: a surviving account's OWN employee record is kept even though the
    `employees` table is on the wipe list (regression guard for the dry-run
    finding that laith.owner / moe.accountant each own an employees row).
"""
import os
import tempfile

import pytest

os.environ.setdefault("JWT_SECRET", "cleanup-test-secret")

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from app.database import Base  # noqa: E402
from app import models, security as S, seed as seedmod  # noqa: E402
from app.config import settings  # noqa: E402
from scripts import cleanup_launch_data as C  # noqa: E402

try:
    from app import tenancy
except Exception:  # noqa: BLE001
    tenancy = None

_ctr = iter(range(1, 10_000))


def _seeded_db():
    """Independent SQLite DB: seed branches + default U-* accounts, then add a real
    owner + accountant that each own an employees row, plus demo employees/products."""
    path = os.path.join(tempfile.gettempdir(), f"cleanup_{os.getpid()}_{next(_ctr)}.db")
    if os.path.exists(path):
        os.remove(path)
    eng = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=eng)
    db = sessionmaker(bind=eng)()
    if tenancy is not None:
        try:
            tenancy.use_system_context(db)
        except Exception:  # noqa: BLE001
            pass
    seedmod.seed(db)  # branches (Store A/B/C) + seven default accounts
    # Legit accounts, each owning an employees row (mirrors production laith/moe).
    db.add(models.Employee(id="EMP-REAL.OWNER", name="Real Owner", branch="Store A", active=True))
    db.add(models.Employee(id="EMP-REAL.ACCT", name="Real Acct", branch="Store B", active=True))
    db.add(models.User(id="real.owner", name="Real Owner", role="owner",
                       email="o@x.local", password_hash=S.hash_pw("x-not-secret"),
                       status="active", can_login=True, employee_id="EMP-REAL.OWNER"))
    db.add(models.User(id="real.acct", name="Real Acct", role="accountant",
                       email="a@x.local", password_hash=S.hash_pw("x-not-secret"),
                       status="active", can_login=True, employee_id="EMP-REAL.ACCT"))
    # Demo/test operational data that MUST be wiped.
    db.add(models.Employee(id="EMP-DEMO-1", name="Demo One", branch="Store A", active=True))
    db.add(models.Employee(id="EMP-DEMO-2", name="Demo Two", branch="Store C", active=True))
    db.add(models.Product(sku="SKU-DEMO-1", name="Demo Product"))
    db.commit()
    return db


@pytest.fixture(autouse=True)
def _seed_off():
    prev = settings.seed_on_start
    settings.seed_on_start = False
    yield
    settings.seed_on_start = prev


def test_dry_run_reports_and_changes_nothing():
    db = _seeded_db()
    try:
        prod_before = db.query(models.Product).count()
        emp_before = db.query(models.Employee).count()
        res = C.run(db, verified_owner_id="real.owner", apply=False)
        assert res["applied"] is False
        # nothing deleted by a dry run
        assert db.query(models.Product).count() == prod_before
        assert db.query(models.Employee).count() == emp_before
        # inventory reports deletable employees = all EXCEPT the 2 account-owned rows
        emp_wipe = dict(res["wipe"]).get("employees")
        assert emp_wipe == emp_before - 2, res["wipe"]
        # and flags exactly those 2 rows as kept back (a surviving user owns them)
        assert ("employees", 2) in res["kept_by_reference"]
        # branches/users/user_branches always appear under PRESERVE
        preserved = {t for t, _ in res["preserve"]}
        assert {"branches", "users", "user_branches"} <= preserved
    finally:
        db.close()


def test_refuses_without_valid_verified_owner():
    db = _seeded_db()
    try:
        with pytest.raises(ValueError):
            C.run(db, verified_owner_id=None, apply=False)
        with pytest.raises(ValueError):          # default seeded id not allowed
            C.run(db, verified_owner_id="U-owner", apply=False)
        with pytest.raises(ValueError):          # accountant is not an owner
            C.run(db, verified_owner_id="real.acct", apply=False)
    finally:
        db.close()


def test_refuses_when_seed_on_start_is_on():
    db = _seeded_db()
    settings.seed_on_start = True
    try:
        with pytest.raises(ValueError):
            C.run(db, verified_owner_id="real.owner", apply=False)
    finally:
        db.close()


def test_apply_requires_backup_confirmed():
    db = _seeded_db()
    try:
        with pytest.raises(ValueError):
            C.run(db, verified_owner_id="real.owner", apply=True, backup_confirmed=False)
    finally:
        db.close()


def test_apply_wipes_demo_but_preserves_accounts_and_their_employee_rows():
    db = _seeded_db()
    try:
        res = C.run(db, verified_owner_id="real.owner", apply=True, backup_confirmed=True)
        assert res["applied"] is True
        # demo operational data gone
        assert db.query(models.Product).count() == 0
        # employees: the 2 demo rows deleted, the 2 account-owned rows survive
        remaining = {e.id for e in db.query(models.Employee).all()}
        assert remaining == {"EMP-REAL.OWNER", "EMP-REAL.ACCT"}, remaining
        # both legit accounts still present, active, with intact employee links
        owner = db.get(models.User, "real.owner")
        acct = db.get(models.User, "real.acct")
        assert owner.status == "active" and owner.employee_id == "EMP-REAL.OWNER"
        assert acct.status == "active" and acct.employee_id == "EMP-REAL.ACCT"
        # branches (keys + display names) preserved
        labels = {b.name: b.display_name for b in db.query(models.Branch).all()}
        assert set(labels) == {"Store A", "Store B", "Store C"}
        # deleted-counts are counts only (ints), never row contents
        assert all(isinstance(v, int) for v in res["deleted"].values())
    finally:
        db.close()

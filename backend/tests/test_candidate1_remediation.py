"""Candidate 1 — positive regression tests for the FIXED behaviour.

The baseline reproduction suite asserts the broken behaviour and is evidence only.
These assert what must now be true, so they fail if a fix regresses rather than
passing by accident when something merely changes.

Covers AA-06 (one commit boundary per financial mutation), BF-14 (fail-closed
branch assignment), BF-12 (per-branch stock reconciliation), BF-13 (transfer
identity), SIM-06 (database-backed payroll uniqueness), SIM-08 (operationally
empty clean install).
"""
import sqlalchemy as sa
from fastapi.testclient import TestClient

from app.main import app
from app.database import SessionLocal
from app import models


def _hdr(c, user="U-owner", pw="demo1234"):
    r = c.post("/api/auth/login", data={"username": user, "password": pw})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _counts():
    db = SessionLocal()
    try:
        return (db.query(models.Ledger).count(),
                db.query(models.AuditLog).count(),
                db.query(models.Movement).count())
    finally:
        db.close()


class _AuditInsertFailure(Exception):
    """Injected failure of audit persistence, raised during flush."""


def _break_audit_persistence():
    """Make every AuditLog INSERT fail at flush time, the way a real audit-table
    outage would, rather than by stubbing out the call site."""
    def _raise(mapper, connection, target):
        raise _AuditInsertFailure("injected audit persistence failure")
    sa.event.listen(models.AuditLog, "before_insert", _raise)
    return lambda: sa.event.remove(models.AuditLog, "before_insert", _raise)


# ------------------------------------------------------------------ AA-06
def test_aa06_failed_audit_rolls_back_the_sale():
    """A sale whose audit evidence cannot be written must not exist. Previously
    the ledger row was committed first, so the money persisted without a record
    of who entered it."""
    with TestClient(app) as c:
        hdr = _hdr(c)
        before = _counts()
        restore = _break_audit_persistence()
        try:
            try:
                c.post("/api/sales", headers=hdr,
                       json={"branch": "Store A", "amount": 777.77})
            except _AuditInsertFailure:
                pass          # surfaces as a 500 through the real ASGI stack
        finally:
            restore()
        assert _counts() == before, "financial state changed despite audit failure"

        # and the endpoint still works once audit persistence recovers
        ok = c.post("/api/sales", headers=hdr, json={"branch": "Store A", "amount": 5.00})
        assert ok.status_code == 201
        after = _counts()
        assert after[0] == before[0] + 1 and after[1] > before[1]


def test_aa06_failed_audit_rolls_back_expense_and_purchase():
    with TestClient(app) as c:
        hdr = _hdr(c)
        for path, body in (("/api/expenses", {"branch": "Store A", "amount": 10.0,
                                              "category": "Utilities"}),
                           ("/api/purchases", {"vendor": "V", "branch": "Store A",
                                               "amount": 20.0})):
            before = _counts()
            restore = _break_audit_persistence()
            try:
                try:
                    c.post(path, headers=hdr, json=body)
                except _AuditInsertFailure:
                    pass
            finally:
                restore()
            assert _counts() == before, f"{path} left state changed after audit failure"


def test_aa06_transfer_legs_are_one_transaction():
    """Both legs of a transfer commit together. The second leg failing must not
    leave stock removed from the source branch."""
    with TestClient(app) as c:
        hdr = _hdr(c)
        db = SessionLocal()
        try:
            a0 = int(db.query(models.Stock).filter_by(sku="MRB-GLD", branch="Store A").first().qty)
            b_row = db.query(models.Stock).filter_by(sku="MRB-GLD", branch="Store B").first()
            b0 = int(b_row.qty) if b_row else 0
            moves0 = db.query(models.Movement).count()
        finally:
            db.close()

        r = c.post("/api/transfers", headers=hdr,
                   json={"sku": "MRB-GLD", "from_branch": "Store A",
                         "to_branch": "Store B", "qty": 5})
        assert r.status_code in (200, 201), r.text
        tid = r.json().get("id")

        db = SessionLocal()
        try:
            aid = db.query(models.Approval).filter(models.Approval.ref == tid).first().id
        finally:
            db.close()

        restore = _break_audit_persistence()
        try:
            try:
                c.post(f"/api/approvals/{aid}/approve", headers=hdr, json={"comment": "x"})
            except _AuditInsertFailure:
                pass
        finally:
            restore()

        db = SessionLocal()
        try:
            a1 = int(db.query(models.Stock).filter_by(sku="MRB-GLD", branch="Store A").first().qty)
            b_row = db.query(models.Stock).filter_by(sku="MRB-GLD", branch="Store B").first()
            b1 = int(b_row.qty) if b_row else 0
            moves1 = db.query(models.Movement).count()
        finally:
            db.close()
        assert (a1, b1, moves1) == (a0, b0, moves0), \
            "a failed approval left stock or movements behind"


# ------------------------------------------------------------------ BF-14
def _make_orphan(uid="U-orphan-fix"):
    from app.security import hash_pw
    db = SessionLocal()
    try:
        if not db.get(models.User, uid):
            db.add(models.User(id=uid, name="Orphan", role="cashier",
                               email=f"{uid}@smokestack.local", password_hash=hash_pw("demo1234")))
            db.commit()
        assert db.query(models.UserBranch).filter(models.UserBranch.user_id == uid).count() == 0
    finally:
        db.close()
    return uid


def test_bf14_unassigned_user_sees_nothing_and_cannot_write():
    with TestClient(app) as c:
        uid = _make_orphan()
        hdr = _hdr(c, user=uid)

        r = c.get("/api/sales?branch=all", headers=hdr)
        assert r.status_code == 200
        assert r.json() == [], "unassigned user must see no rows"

        w = c.post("/api/sales", headers=hdr, json={"branch": "Store A", "amount": 1.0})
        assert w.status_code == 403, f"unassigned user could still write ({w.status_code})"

        # explicit unauthorized branch stays 403 (unchanged behaviour)
        assert c.get("/api/sales?branch=Store B", headers=hdr).status_code == 403


def test_bf14_branch_sensitive_routes_403_never_500():
    """Every branch-sensitive route must refuse an unassigned user EXPLICITLY.
    payroll/finalize indexed resolve_branches(...)[0] and would raise IndexError,
    reporting an authorization failure as a server fault."""
    with TestClient(app) as c:
        hdr = _hdr(c, user=_make_orphan("U-orphan-500"))
        fin = c.post("/api/payroll/finalize", headers=hdr,
                     params={"start": "2026-08-01", "end": "2026-08-07"})
        assert fin.status_code == 403, f"expected 403, got {fin.status_code}"
        assert fin.status_code != 500

        for route in ("/api/sales", "/api/expenses", "/api/purchases",
                      "/api/inventory/movements", "/api/attendance/today"):
            resp = c.get(route, headers=hdr)
            assert resp.status_code != 500, f"{route} returned 500 for an unassigned user"


# ------------------------------------------------------------------ BF-12
def _checks(c, hdr):
    r = c.get("/api/control/validate", headers=hdr)
    assert r.status_code == 200, r.text
    return {ch["check"]: ch for s in r.json()["sections"] for ch in s["checks"]}


def test_bf12_offsetting_branch_corruption_is_detected_and_fails_the_gate():
    with TestClient(app) as c:
        hdr = _hdr(c)
        name = "Stock equals movement history"
        assert _checks(c, hdr)[name]["status"] == "pass", "precondition: clean"

        db = SessionLocal()
        try:
            a = db.query(models.Stock).filter_by(sku="ZYN-CM", branch="Store A").first()
            b = db.query(models.Stock).filter_by(sku="ZYN-CM", branch="Store B").first()
            a.qty = int(a.qty) + 5
            b.qty = int(b.qty) - 5
            db.commit()
            try:
                ch = _checks(c, hdr)[name]
                assert ch["status"] != "pass", "offsetting branch corruption still passes"
                assert ch["status"] == "error", "stock drift must fail the gate, not warn"
                assert "ZYN-CM@Store A" in ch["detail"] and "ZYN-CM@Store B" in ch["detail"]
            finally:
                a = db.query(models.Stock).filter_by(sku="ZYN-CM", branch="Store A").first()
                b = db.query(models.Stock).filter_by(sku="ZYN-CM", branch="Store B").first()
                a.qty = int(a.qty) - 5
                b.qty = int(b.qty) + 5
                db.commit()
        finally:
            db.close()


# ------------------------------------------------------------------ BF-13
def test_bf13_mismatched_transfer_pair_is_detected():
    """Two legs sharing an id but disagreeing on SKU/quantity/branch must fail —
    the old global count could not tell them from a correct transfer."""
    with TestClient(app) as c:
        hdr = _hdr(c)
        name = "Approved transfers moved stock both ways"
        db = SessionLocal()
        try:
            db.add(models.Movement(sku="MRB-GLD", branch="Store A", type="transfer_out",
                                   qty_before=100, qty_change=-40, qty_after=60,
                                   transfer_id="TR-BOGUS"))
            db.add(models.Movement(sku="RAW-CLS", branch="Store C", type="transfer_in",
                                   qty_before=10, qty_change=1, qty_after=11,
                                   transfer_id="TR-BOGUS"))
            db.commit()
            try:
                ch = _checks(c, hdr)[name]
                assert ch["status"] == "error", "mismatched pair reported as pass"
                assert "TR-BOGUS" in str(ch["value"])
            finally:
                for m in db.query(models.Movement).filter(
                        models.Movement.transfer_id == "TR-BOGUS").all():
                    db.delete(m)
                db.commit()
        finally:
            db.close()


def test_bf13_legacy_rows_are_reported_not_falsely_paired():
    """Movements written before transfer_id existed are unverifiable. They must be
    surfaced honestly, never back-filled with an invented identity."""
    with TestClient(app) as c:
        hdr = _hdr(c)
        name = "All transfer movements carry a transfer identity"
        db = SessionLocal()
        try:
            db.add(models.Movement(sku="MRB-GLD", branch="Store A", type="transfer_out",
                                   qty_before=10, qty_change=-1, qty_after=9,
                                   transfer_id=None))
            db.commit()
            try:
                ch = _checks(c, hdr)[name]
                assert ch["status"] == "warning"
                assert ch["value"] >= 1
                # the pair check must NOT have silently absorbed it
                pair = _checks(c, hdr)["Approved transfers moved stock both ways"]
                assert "1 out / 0 in" not in str(pair["value"])
            finally:
                for m in db.query(models.Movement).filter(
                        models.Movement.transfer_id.is_(None),
                        models.Movement.type == "transfer_out",
                        models.Movement.qty_change == -1).all():
                    db.delete(m)
                db.commit()
        finally:
            db.close()


def test_bf13_real_transfer_produces_an_identified_pair():
    with TestClient(app) as c:
        hdr = _hdr(c)
        r = c.post("/api/transfers", headers=hdr,
                   json={"sku": "GRP-GRN", "from_branch": "Store A",
                         "to_branch": "Store C", "qty": 2})
        assert r.status_code in (200, 201), r.text
        tid = r.json()["id"]
        db = SessionLocal()
        try:
            aid = db.query(models.Approval).filter(models.Approval.ref == tid).first().id
        finally:
            db.close()
        assert c.post(f"/api/approvals/{aid}/approve", headers=hdr,
                      json={"comment": "ok"}).status_code == 200

        db = SessionLocal()
        try:
            legs = db.query(models.Movement).filter(models.Movement.transfer_id == tid).all()
        finally:
            db.close()
        assert len(legs) == 2, "both legs must carry the transfer id"
        assert {m.type for m in legs} == {"transfer_in", "transfer_out"}
        assert {m.branch for m in legs} == {"Store A", "Store C"}
        assert sum(int(m.qty_change) for m in legs) == 0
        assert _checks(c, hdr)["Approved transfers moved stock both ways"]["status"] == "pass"


# ------------------------------------------------------------------ SIM-06
#
# CONTRACT MIGRATION (BF-PR-02). These three tests called `finalize` with no `branch`
# param, which defaulted to `branch="all"`. That default is now REFUSED before any
# write: finalize computed the total across every branch and then persisted it under
# `scope[0]`, so one PayrollRun labelled Store A carried Store A + B + C, and a later
# explicit Store B run for the same period could not collide with it and was accepted.
#
# ONLY the request setup changed — each test keeps its original assertions verbatim
# (200 then 409 on the duplicate, unchanged row counts after the refusal, distinct
# periods still posting). Nothing was relaxed to obtain a pass; the duplicate is still
# required to be refused by the DATABASE, and the natural key it collides on now
# genuinely describes one branch's pay run instead of a combined one.
_BR = "Store A"


def test_sim06_duplicate_payroll_finalize_is_refused_by_the_database():
    with TestClient(app) as c:
        hdr = _hdr(c)
        period = {"start": "2026-09-01", "end": "2026-09-15", "branch": _BR}
        memo = f"Payroll {period['start']}->{period['end']}"

        def rows():
            db = SessionLocal()
            try:
                return db.query(models.Ledger).filter(
                    models.Ledger.type == "payroll", models.Ledger.memo == memo).count()
            finally:
                db.close()

        first = c.post("/api/payroll/finalize", headers=hdr, params=period)
        assert first.status_code == 200, first.text
        assert rows() == 1

        for _ in range(3):
            dup = c.post("/api/payroll/finalize", headers=hdr, params=period)
            assert dup.status_code == 409, f"expected 409, got {dup.status_code}"
        assert rows() == 1, "duplicate finalize created extra postings"


def test_sim06_rejected_duplicate_leaves_no_partial_state():
    """The 409 must roll back the ledger row, the run row AND the audit row."""
    with TestClient(app) as c:
        hdr = _hdr(c)
        period = {"start": "2026-10-01", "end": "2026-10-15", "branch": _BR}
        assert c.post("/api/payroll/finalize", headers=hdr, params=period).status_code == 200
        before = _counts()
        assert c.post("/api/payroll/finalize", headers=hdr, params=period).status_code == 409
        assert _counts() == before, "the refused duplicate left state behind"


def test_sim06_distinct_periods_and_branches_still_post():
    with TestClient(app) as c:
        hdr = _hdr(c)
        a = c.post("/api/payroll/finalize", headers=hdr,
                   params={"start": "2026-11-01", "end": "2026-11-15", "branch": _BR})
        b = c.post("/api/payroll/finalize", headers=hdr,
                   params={"start": "2026-11-16", "end": "2026-11-30", "branch": _BR})
        assert a.status_code == 200 and b.status_code == 200, "distinct periods must post"
        # The name promised "and branches" but only distinct PERIODS were ever
        # exercised, because `branch` defaulted to "all" and every call resolved to
        # the same target. Now that the branch is explicit, assert the other half.
        d = c.post("/api/payroll/finalize", headers=hdr,
                   params={"start": "2026-11-01", "end": "2026-11-15", "branch": "Store B"})
        assert d.status_code == 200, (
            f"the same period at a DIFFERENT branch must post, got {d.status_code}: {d.text}")


def test_sim06_uniqueness_is_a_database_constraint_not_a_precheck():
    """Insert the duplicate directly, bypassing the endpoint entirely. If the
    guard were only an application pre-check this would succeed."""
    import datetime as dt
    db = SessionLocal()
    try:
        db.add(models.PayrollRun(branch="Store A", period_start=dt.date(2026, 12, 1),
                                 period_end=dt.date(2026, 12, 15), gross=100))
        db.commit()
        db.add(models.PayrollRun(branch="Store A", period_start=dt.date(2026, 12, 1),
                                 period_end=dt.date(2026, 12, 15), gross=100))
        try:
            db.commit()
            raise AssertionError("the database accepted a duplicate pay period")
        except sa.exc.IntegrityError:
            db.rollback()
    finally:
        db.close()


# ------------------------------------------------------------------ SIM-08
def test_sim08_clean_install_is_operationally_empty(tmp_path, monkeypatch):
    """Seeding without the explicit demo opt-in creates structure and NOTHING
    operational: no money, no stock, no movements, no partner balances."""
    from app import seed as seed_mod
    from app.config import settings

    url = f"sqlite:///{tmp_path/'clean.db'}"
    engine = sa.create_engine(url, connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(bind=engine)
    Session = sa.orm.sessionmaker(bind=engine, autoflush=False, autocommit=False)

    monkeypatch.setattr(settings, "seed_demo_data", False)
    db = Session()
    try:
        from app import tenancy
        tenancy.use_system_context(db)
        seed_mod.seed(db)
    finally:
        db.close()

    db = Session()
    try:
        assert db.query(models.Branch).count() > 0, "structure must still be created"
        assert db.query(models.User).count() > 0, "role logins must still be created"
        for model, label in ((models.Ledger, "ledger"), (models.Product, "products"),
                             (models.Stock, "stock"), (models.Movement, "movements"),
                             (models.Customer, "customers"), (models.Supplier, "suppliers"),
                             (models.Employee, "employees"), (models.License, "licenses")):
            assert db.query(model).count() == 0, f"clean install contains {label} rows"
    finally:
        db.close()
        engine.dispose()


def test_sim08_demo_data_requires_explicit_optin(tmp_path, monkeypatch):
    """The same seed WITH the opt-in still produces the demo fixtures, so the
    capability is preserved rather than deleted."""
    from app import seed as seed_mod
    from app.config import settings

    url = f"sqlite:///{tmp_path/'demo.db'}"
    engine = sa.create_engine(url, connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(bind=engine)
    Session = sa.orm.sessionmaker(bind=engine, autoflush=False, autocommit=False)

    monkeypatch.setattr(settings, "seed_demo_data", True)
    db = Session()
    try:
        from app import tenancy
        tenancy.use_system_context(db)
        seed_mod.seed(db)
    finally:
        db.close()

    db = Session()
    try:
        assert db.query(models.Product).count() > 0
        assert db.query(models.Ledger).count() > 0
    finally:
        db.close()
        engine.dispose()


def test_sim06_concurrent_finalize_posts_exactly_once():
    """SIM-06 under real concurrency. Skipped on SQLite, which serialises writers
    and therefore cannot demonstrate the race this constraint exists to lose."""
    import os
    import threading
    import pytest as _pytest

    from app.config import settings
    if settings.is_sqlite:
        _pytest.skip("needs PostgreSQL: SQLite serialises writers")

    period = {"start": "2027-06-01", "end": "2027-06-15"}
    memo = f"Payroll {period['start']}->{period['end']}"
    with TestClient(app) as c:
        hdr = _hdr(c, pw=os.getenv("SEED_PASSWORD", "demo1234"))
        n = 12
        codes, lock, barrier = [], threading.Lock(), threading.Barrier(n)

        def go():
            barrier.wait()                    # release every thread together
            r = c.post("/api/payroll/finalize", headers=hdr, params=period)
            with lock:
                codes.append(r.status_code)

        ts = [threading.Thread(target=go) for _ in range(n)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()

        db = SessionLocal()
        try:
            postings = db.query(models.Ledger).filter(models.Ledger.memo == memo).count()
        finally:
            db.close()

        assert codes.count(200) == 1, f"more than one finalize succeeded: {sorted(codes)}"
        assert codes.count(409) == n - 1, f"unexpected statuses: {sorted(codes)}"
        assert postings == 1, "concurrent finalize created duplicate financial state"

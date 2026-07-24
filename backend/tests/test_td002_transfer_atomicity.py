"""TD-002 — transfer atomicity: exhaustive behavioural + failure/concurrency coverage.

Covers the mandated matrix: happy path, replay/duplicate approval, insufficient
inventory, rollback-after-failure / partial-execution / interruption (failure
injection), concurrent double-approve (compare-and-swap), canonical lock order,
single-movement regression, and a reconciliation stress loop.

All assertions are DELTA-based (read a baseline, act, assert the change) so the tests
are order-independent under the shared process-wide test engine.
"""
import os
import tempfile

_DB = os.path.join(tempfile.gettempdir(), f"smokestack_td002_{os.getpid()}.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["SEED_ON_START"] = "true"
os.environ["JWT_SECRET"] = "td002-secret-long-enough"
os.environ["TELEGRAM_BOT_TOKEN"] = "test-bot-token"

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.database import SessionLocal
from app import models, tenancy, transfers_service, partners_repo as PR
import app.routers.inventory as inventory

with TestClient(app):
    pass
client = TestClient(app)

SKU = "MRB-GLD"
SRC, DST = "Store A", "Store B"


def _tok():
    return client.post("/api/auth/login",
                       data={"username": "U-owner", "password": "demo1234"}).json()["access_token"]


def _qty(sku, branch):
    db = SessionLocal(); tenancy.set_session_company(db, 1)
    try:
        st = db.query(models.Stock).filter_by(sku=sku, branch=branch).first()
        return int(st.qty) if st and st.qty is not None else 0
    finally:
        db.close()


def _receive(h, sku, branch, qty):
    return client.post("/api/inventory/receive", headers=h,
                       json={"sku": sku, "branch": branch, "qty": qty, "reason": "td002 setup"})


def _create_transfer(h, qty, sku=SKU, frm=SRC, to=DST):
    return client.post("/api/transfers", headers=h,
                       json={"sku": sku, "from_branch": frm, "to_branch": to, "qty": qty})


def _pending_aid(h, tid):
    for a in client.get("/api/approvals", headers=h).json():
        if a["ref"] == tid:
            return a["id"]
    return None


def _fetch_ctx(tid, aid):
    """Return (session, user, transfer, approval) for a direct-service call."""
    db = SessionLocal(); tenancy.set_session_company(db, 1)
    user = db.query(models.User).filter_by(id="U-owner").first()
    user._company_id = 1
    return db, user, PR.get_transfer(db, tid), PR.get_approval(db, aid)


# --------------------------------------------------------------------------- #
def test_transfer_is_atomic_and_moves_stock_exactly_once():
    h = {"Authorization": "Bearer " + _tok()}
    _receive(h, SKU, SRC, 50)                     # guarantee ample source stock
    s0, d0 = _qty(SKU, SRC), _qty(SKU, DST)
    tid = _create_transfer(h, 10).json()["id"]
    aid = _pending_aid(h, tid)
    r = client.post(f"/api/approvals/{aid}/approve", headers=h, json={"comment": "ok"})
    assert r.status_code == 200 and r.json()["status"] == "approved"
    assert _qty(SKU, SRC) == s0 - 10              # source debited
    assert _qty(SKU, DST) == d0 + 10              # destination credited (conserved)


def test_replay_or_duplicate_approval_is_rejected():
    h = {"Authorization": "Bearer " + _tok()}
    _receive(h, SKU, SRC, 30)
    s0, d0 = _qty(SKU, SRC), _qty(SKU, DST)
    tid = _create_transfer(h, 7).json()["id"]
    aid = _pending_aid(h, tid)
    r1 = client.post(f"/api/approvals/{aid}/approve", headers=h, json={"comment": "first"})
    r2 = client.post(f"/api/approvals/{aid}/approve", headers=h, json={"comment": "replay"})
    assert r1.status_code == 200
    assert r2.status_code == 409                  # second approval rejected
    assert _qty(SKU, SRC) == s0 - 7               # applied exactly once
    assert _qty(SKU, DST) == d0 + 7


def test_insufficient_stock_blocks_and_changes_nothing():
    h = {"Authorization": "Bearer " + _tok()}
    s0, d0 = _qty(SKU, SRC), _qty(SKU, DST)
    r = _create_transfer(h, s0 + 1000)            # more than the source holds
    assert r.status_code == 422
    assert _qty(SKU, SRC) == s0 and _qty(SKU, DST) == d0   # nothing moved


def test_failure_injection_between_legs_rolls_back_everything(monkeypatch):
    """Interruption/partial-execution: fail the 2nd leg → whole unit rolls back."""
    h = {"Authorization": "Bearer " + _tok()}
    _receive(h, SKU, SRC, 40)
    s0, d0 = _qty(SKU, SRC), _qty(SKU, DST)
    tid = _create_transfer(h, 9).json()["id"]
    aid = _pending_aid(h, tid)

    real = inventory._write_movement

    def boom(db, user, sku, branch, mtype, change, notes="", unit_cost=None):
        if branch == DST:                         # destination leg = the 2nd applied
            raise RuntimeError("injected interruption before commit")
        return real(db, user, sku, branch, mtype, change, notes=notes, unit_cost=unit_cost)

    monkeypatch.setattr(inventory, "_write_movement", boom)

    db, user, transfer, approval = _fetch_ctx(tid, aid)
    try:
        with pytest.raises(RuntimeError):
            transfers_service.execute_approved_transfer(db, user, transfer, approval, "boom")
        db.rollback()
    finally:
        db.close()

    # Nothing persisted: source untouched, destination untouched, transfer still pending.
    assert _qty(SKU, SRC) == s0
    assert _qty(SKU, DST) == d0
    db2 = SessionLocal(); tenancy.set_session_company(db2, 1)
    try:
        assert PR.get_transfer(db2, tid).status == "pending"
    finally:
        db2.close()


def test_concurrent_double_approve_only_one_wins():
    """Two sessions race the compare-and-swap claim; exactly one applies, one gets 409."""
    h = {"Authorization": "Bearer " + _tok()}
    _receive(h, SKU, SRC, 25)
    s0, d0 = _qty(SKU, SRC), _qty(SKU, DST)
    tid = _create_transfer(h, 6).json()["id"]
    aid = _pending_aid(h, tid)

    db1, u1, t1, a1 = _fetch_ctx(tid, aid)         # racer 1
    db2, u2, t2, a2 = _fetch_ctx(tid, aid)         # racer 2 (also sees pending)
    try:
        ok = transfers_service.execute_approved_transfer(db1, u1, t1, a1, "winner")
        assert ok["status"] == "approved"
        with pytest.raises(Exception) as ei:       # loser's CAS matches 0 rows -> 409
            transfers_service.execute_approved_transfer(db2, u2, t2, a2, "loser")
        assert getattr(ei.value, "status_code", None) == 409
        db2.rollback()
    finally:
        db1.close(); db2.close()

    assert _qty(SKU, SRC) == s0 - 6 and _qty(SKU, DST) == d0 + 6   # applied once


def test_receive_and_adjust_still_commit(monkeypatch):
    """Regression: caller-owns-commit refactor keeps single-movement endpoints working."""
    h = {"Authorization": "Bearer " + _tok()}
    s0 = _qty(SKU, SRC)
    assert _receive(h, SKU, SRC, 5).status_code == 200
    assert _qty(SKU, SRC) == s0 + 5               # receive persisted
    r = client.post("/api/inventory/adjust", headers=h,
                    json={"sku": SKU, "branch": SRC, "qty": -3, "reason": "count fix"})
    assert r.status_code == 200
    assert _qty(SKU, SRC) == s0 + 2               # adjust persisted


def test_canonical_lock_order_for_opposing_transfers():
    from app import locking
    captured = []
    real = inventory._write_movement

    def cap(db, user, sku, branch, mtype, change, notes="", unit_cost=None):
        captured.append(branch)
    inventory._write_movement = cap
    try:
        # opposing directions between the same two branches must lock in the SAME order
        locking.apply_ordered_movements(None, None, [
            {"sku": SKU, "branch": DST, "mtype": "transfer_in", "change": 1},
            {"sku": SKU, "branch": SRC, "mtype": "transfer_out", "change": -1},
        ])
        first_pass = list(captured); captured.clear()
        locking.apply_ordered_movements(None, None, [
            {"sku": SKU, "branch": SRC, "mtype": "transfer_out", "change": -1},
            {"sku": SKU, "branch": DST, "mtype": "transfer_in", "change": 1},
        ])
    finally:
        inventory._write_movement = real
    assert first_pass == captured == [SRC, DST]   # canonical (branch asc) regardless of input


def test_stress_many_transfers_reconcile():
    h = {"Authorization": "Bearer " + _tok()}
    _receive(h, SKU, SRC, 60)
    s0, d0 = _qty(SKU, SRC), _qty(SKU, DST)
    n = 15
    for _ in range(n):
        tid = _create_transfer(h, 2).json()["id"]
        aid = _pending_aid(h, tid)
        assert client.post(f"/api/approvals/{aid}/approve", headers=h, json={"comment": ""}).status_code == 200
    assert _qty(SKU, SRC) == s0 - 2 * n           # every unit accounted for
    assert _qty(SKU, DST) == d0 + 2 * n           # conserved: nothing created or lost

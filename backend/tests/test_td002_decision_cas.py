"""TD-002 follow-up — atomic approval decision (compare-and-swap) regression suite.

Proves EXACTLY ONE decision can ever win for a given approval, across every ordering,
with exactly one final status and exactly one audit — and that a moved-stock transfer
can never be labelled 'rejected' (the defect the adversarial campaign found).

Decisions are driven through the real `workflow._decide` (the same code path the HTTP
approve/reject endpoints use), so the CAS is exercised. SQLite serializes writers; each
`_decide` commits, so a second decision observes the first's committed status and its
CAS matches 0 rows -> conflict. A 1000-iteration stress covers all orderings.
"""
import os, tempfile
_DB = os.path.join(tempfile.gettempdir(), f"smokestack_cas_{os.getpid()}.db")
if os.path.exists(_DB): os.remove(_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["SEED_ON_START"] = "true"
os.environ["JWT_SECRET"] = "cas-secret-long-enough"
os.environ["TELEGRAM_BOT_TOKEN"] = "test-bot-token"

from fastapi import HTTPException
from fastapi.testclient import TestClient
from app.main import app
from app.database import SessionLocal
from app import models, tenancy, counters, partners_repo as PR
from app.routers.workflow import _decide

with TestClient(app): pass
client = TestClient(app)
SKU, SRC, DST = "MRB-GLD", "Store A", "Store B"


def _ensure_stock(qty=100000):
    db = SessionLocal(); tenancy.set_session_company(db, 1)
    st = db.query(models.Stock).filter_by(sku=SKU, branch=SRC).first()
    if not st:
        st = models.Stock(company_id=1, sku=SKU, branch=SRC, qty=0); db.add(st)
    st.qty = qty; db.commit(); db.close()


def _seed_transfer(qty=1):
    """Create a pending Transfer + Approval directly (mirrors create_transfer's rows)."""
    db = SessionLocal(); tenancy.set_session_company(db, 1)
    tid = counters.next_number(db, counters.TRANSFER)
    aid = counters.next_number(db, counters.APPROVAL)
    db.add(models.Transfer(id=tid, company_id=1, sku=SKU, from_branch=SRC, to_branch=DST,
                           qty=qty, status="pending"))
    db.add(models.Approval(id=aid, company_id=1, kind="transfer", ref=tid, branch=SRC, amount=0,
                           requested_by="U-owner", summary=f"stress {tid}", status="pending"))
    db.commit(); db.close()
    return tid, aid


def _decide_code(aid, decision):
    """Run one decision through the real _decide on its own session; return HTTP code."""
    db = SessionLocal(); tenancy.set_session_company(db, 1)
    u = db.query(models.User).filter_by(id="U-owner").first(); u._company_id = 1
    try:
        _decide(db, u, aid, decision, "")
        return 200
    except HTTPException as e:
        db.rollback(); return e.status_code
    finally:
        db.close()


def _astatus(aid):
    db = SessionLocal(); tenancy.set_session_company(db, 1)
    try: return PR.get_approval(db, aid).status
    finally: db.close()
def _tstatus(tid):
    db = SessionLocal(); tenancy.set_session_company(db, 1)
    try: return PR.get_transfer(db, tid).status
    finally: db.close()
def _audits(aid):
    db = SessionLocal(); tenancy.set_session_company(db, 1)
    try: return db.query(models.AuditLog).filter(models.AuditLog.ref == str(aid),
                                                  models.AuditLog.entity == "approval").count()
    finally: db.close()
def _moved(tid):
    db = SessionLocal(); tenancy.set_session_company(db, 1)
    try: return db.query(models.Movement).filter(models.Movement.notes.like(f"%{tid}%")).count() > 0
    finally: db.close()


def _assert_consistent(tid, aid):
    st = _astatus(aid)
    assert st in ("approved", "rejected")                 # exactly one final status
    assert _audits(aid) == 1                               # exactly one audit
    assert _moved(tid) == (st == "approved")              # stock moved IFF approved
    assert _tstatus(tid) == st                             # transfer + approval agree


def test_approve_vs_approve():
    _ensure_stock(); tid, aid = _seed_transfer()
    codes = sorted([_decide_code(aid, "approved"), _decide_code(aid, "approved")])
    assert codes == [200, 409]
    _assert_consistent(tid, aid); assert _astatus(aid) == "approved"

def test_reject_vs_reject():
    _ensure_stock(); tid, aid = _seed_transfer()
    codes = sorted([_decide_code(aid, "rejected"), _decide_code(aid, "rejected")])
    assert codes == [200, 409]
    _assert_consistent(tid, aid); assert _astatus(aid) == "rejected"; assert not _moved(tid)

def test_approve_then_reject():
    _ensure_stock(); tid, aid = _seed_transfer()
    assert _decide_code(aid, "approved") == 200
    assert _decide_code(aid, "rejected") == 409          # loser
    _assert_consistent(tid, aid); assert _astatus(aid) == "approved" and _moved(tid)

def test_reject_then_approve():
    _ensure_stock(); tid, aid = _seed_transfer()
    assert _decide_code(aid, "rejected") == 200
    assert _decide_code(aid, "approved") == 409          # loser: no stock moved
    _assert_consistent(tid, aid); assert _astatus(aid) == "rejected" and not _moved(tid)

def test_replay_after_approve():
    _ensure_stock(); tid, aid = _seed_transfer()
    assert _decide_code(aid, "approved") == 200
    for _ in range(5): assert _decide_code(aid, "approved") == 409
    _assert_consistent(tid, aid)

def test_replay_after_reject():
    _ensure_stock(); tid, aid = _seed_transfer()
    assert _decide_code(aid, "rejected") == 200
    for _ in range(5): assert _decide_code(aid, "rejected") == 409
    _assert_consistent(tid, aid)


def test_stress_race_1000_iterations():
    """1000 approvals, each hit by two competing decisions in alternating order.
    Invariant every time: exactly one 200 + one 409, exactly one status, one audit,
    and stock moved IFF approved (never a moved-stock 'rejected')."""
    _ensure_stock(200000)
    combos = [("approved", "rejected"), ("rejected", "approved"),
              ("approved", "approved"), ("rejected", "rejected")]
    inconsistencies = 0
    for i in range(1000):
        tid, aid = _seed_transfer()
        d1, d2 = combos[i % 4]
        c1 = _decide_code(aid, d1)
        c2 = _decide_code(aid, d2)
        if sorted([c1, c2]) != [200, 409]:
            inconsistencies += 1; continue
        st = _astatus(aid)
        if not (st in ("approved", "rejected") and _audits(aid) == 1
                and _moved(tid) == (st == "approved") and _tstatus(tid) == st):
            inconsistencies += 1
    assert inconsistencies == 0, f"{inconsistencies}/1000 iterations were inconsistent"

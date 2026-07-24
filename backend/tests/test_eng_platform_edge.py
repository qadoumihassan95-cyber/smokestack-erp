"""Additional edge-case & invariant coverage for the platform primitives
(counters / idempotency / locking) from Engineering Phases 4-6.

Pure additive tests — no production code is touched. They pin behaviours that the
B-C migration and future document types depend on:
  * counter formatting (width/prefix) and **transactional atomicity** (a rolled-back
    operation must not burn a number),
  * idempotency **scoping** (per-caller, no cross-tenant key collision) and the exact
    set of mutating verbs it governs,
  * the **determinism** of the canonical stock lock order regardless of input order.
"""
import os
import tempfile

_DB = os.path.join(tempfile.gettempdir(), f"smokestack_engedge_{os.getpid()}.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["SEED_ON_START"] = "true"
os.environ["JWT_SECRET"] = "engedge-secret-long-enough"
os.environ["TELEGRAM_BOT_TOKEN"] = "test-bot-token"

from fastapi.testclient import TestClient

from app.main import app
from app.database import SessionLocal
from app import counters, idempotency, locking, tenancy

with TestClient(app):
    pass
client = TestClient(app)


# ------------------------------- counters -------------------------------

def test_counter_width_and_prefix_formatting():
    d = SessionLocal(); tenancy.set_session_company(d, 9201)
    try:
        assert counters.next_number(d, "PO", width=4) == "PO-0001"
        assert counters.next_number(d, "PO", width=4) == "PO-0002"
        # a custom prefix changes the visible label but not the (company, doc_type) key
        assert counters.next_number(d, "PO", prefix="INV") == "INV-000003"
        d.commit()
    finally:
        d.close()


def test_counter_explicit_company_id_overrides_session_context():
    d = SessionLocal(); tenancy.set_session_company(d, 9202)
    try:
        # session context is 9202, but an explicit company_id must win and be independent
        assert counters.next_number(d, "PO", company_id=9203) == "PO-000001"
        assert counters.next_number(d, "PO", company_id=9203) == "PO-000002"
        assert counters.next_number(d, "PO") == "PO-000001"   # 9202's own sequence untouched
        d.commit()
    finally:
        d.close()


def test_counter_increment_is_rolled_back_with_the_transaction():
    # The increment runs INSIDE the caller's transaction and is not committed by the
    # service; a rolled-back operation must NOT consume a number (gap-minimal invariant).
    d = SessionLocal(); tenancy.set_session_company(d, 9204)
    try:
        a = counters.next_number(d, "PO")   # allocate, still uncommitted
        d.rollback()                         # operation fails -> revert
        b = counters.next_number(d, "PO")    # retry
        d.commit()
        assert a == "PO-000001" and b == "PO-000001"   # no number burned on rollback
    finally:
        d.close()


# ------------------------------ idempotency ------------------------------

class _FakeReq:
    def __init__(self, auth):
        self.headers = {"authorization": auth} if auth else {}


def test_idempotency_scope_isolates_by_authorization_header():
    s1 = idempotency._scope(_FakeReq("Bearer token-A"))
    s2 = idempotency._scope(_FakeReq("Bearer token-B"))
    s1_again = idempotency._scope(_FakeReq("Bearer token-A"))
    assert s1 == s1_again        # deterministic per caller
    assert s1 != s2              # different callers/tenants never share a key namespace
    assert idempotency._scope(_FakeReq(None)) == "anon"


def test_idempotency_governs_exactly_the_mutating_verbs():
    assert idempotency._METHODS == {"POST", "PUT", "PATCH"}
    assert "GET" not in idempotency._METHODS
    assert "DELETE" not in idempotency._METHODS


def test_get_request_is_never_idempotency_replayed():
    # even carrying a key, a GET must pass straight through
    r = client.get("/api/health", headers={"Idempotency-Key": "ignored-on-get"})
    assert r.status_code == 200
    assert "Idempotency-Replayed" not in r.headers


def test_replayed_write_preserves_status_and_flags_replay():
    tok = client.post("/api/auth/login",
                      data={"username": "U-owner", "password": "demo1234"}).json()["access_token"]
    h = {"Authorization": "Bearer " + tok, "Idempotency-Key": "engedge-po-1"}
    body = {"vendor": "EdgeCo", "branch": "Store A", "amount": 10}
    r1 = client.post("/api/purchases", json=body, headers=h)
    r2 = client.post("/api/purchases", json=body, headers=h)
    assert r1.status_code == 201 and r2.status_code == 201     # status preserved on replay
    assert r2.headers.get("Idempotency-Replayed") == "true"
    assert r2.json() == r1.json()                              # identical document, once


# ------------------------------- locking -------------------------------

def test_lock_key_is_string_tuple_branch_then_sku():
    assert locking.stock_lock_key("SKU9", "BranchZ") == ("BranchZ", "SKU9")
    assert locking.stock_lock_key(1, 2) == ("2", "1")   # numeric ids normalised to str


def test_apply_ordered_movements_is_deterministic_across_input_permutations():
    import app.routers.inventory as inv
    orig = inv._write_movement

    def run(ops):
        captured = []
        inv._write_movement = (
            lambda db, user, sku, branch, mtype, change, notes="", unit_cost=None:
            captured.append((branch, sku))
        )
        try:
            locking.apply_ordered_movements(None, None, ops)
        finally:
            inv._write_movement = orig
        return captured

    ops_a = [
        {"sku": "S2", "branch": "Store B", "mtype": "x", "change": 1},
        {"sku": "S1", "branch": "Store A", "mtype": "x", "change": 1},
        {"sku": "S1", "branch": "Store B", "mtype": "x", "change": 1},
    ]
    out_a = run(ops_a)
    out_b = run(list(reversed(ops_a)))
    assert out_a == out_b                       # output independent of input order
    assert out_a == sorted(out_a)               # canonical ascending (branch, sku)
    assert out_a[0] == ("Store A", "S1")        # lowest key applied first

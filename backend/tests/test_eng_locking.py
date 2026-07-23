"""Engineering Phase 6 — global lock-ordering policy."""
import os
import tempfile

_DB = os.path.join(tempfile.gettempdir(), f"smokestack_lock_{os.getpid()}.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["SEED_ON_START"] = "true"
os.environ["JWT_SECRET"] = "lock-secret-long-enough"
os.environ["TELEGRAM_BOT_TOKEN"] = "test-bot-token"

from fastapi.testclient import TestClient
from app.main import app
from app import locking

with TestClient(app):
    pass


def test_canonical_key_orders_by_branch_then_sku():
    assert locking.stock_lock_key("Z", "A") < locking.stock_lock_key("A", "B")  # branch dominates
    assert locking.stock_lock_key("A", "Store A") < locking.stock_lock_key("B", "Store A")  # then sku


def test_apply_ordered_movements_sorts_and_applies_both():
    captured = []

    def fake_write(db, user, sku, branch, mtype, change, notes="", unit_cost=None):
        captured.append((branch, sku, mtype, change))

    import app.routers.inventory as inv
    orig = inv._write_movement
    inv._write_movement = fake_write
    try:
        # pass in reverse (Store B first) — helper must reorder to Store A, Store B
        locking.apply_ordered_movements(None, None, [
            {"sku": "X", "branch": "Store B", "mtype": "transfer_in", "change": 5},
            {"sku": "X", "branch": "Store A", "mtype": "transfer_out", "change": -5},
        ])
    finally:
        inv._write_movement = orig
    assert [c[0] for c in captured] == ["Store A", "Store B"]   # canonical lock order
    assert captured[0][3] == -5 and captured[1][3] == 5         # both applied, correct signs

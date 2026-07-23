"""Engineering Phase 5 — shared idempotency framework."""
import os
import tempfile

_DB = os.path.join(tempfile.gettempdir(), f"smokestack_idem_{os.getpid()}.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["SEED_ON_START"] = "true"
os.environ["JWT_SECRET"] = "idem-secret-long-enough"
os.environ["TELEGRAM_BOT_TOKEN"] = "test-bot-token"

from fastapi.testclient import TestClient
from app.main import app

with TestClient(app):
    pass
client = TestClient(app)


def _tok():
    return client.post("/api/auth/login", data={"username": "U-owner", "password": "demo1234"}).json()["access_token"]


def test_repeated_purchase_with_same_key_creates_one_document():
    h = {"Authorization": "Bearer " + _tok(), "Idempotency-Key": "idem-po-1"}
    body = {"vendor": "Acme", "branch": "Store A", "amount": 100}
    r1 = client.post("/api/purchases", json=body, headers=h)
    r2 = client.post("/api/purchases", json=body, headers=h)
    assert r1.status_code == 201
    # replay returns the SAME body (same PO id) and is flagged
    assert r2.json() == r1.json()
    assert r2.headers.get("Idempotency-Replayed") == "true"


def test_different_key_creates_a_new_document():
    tok = _tok()
    body = {"vendor": "Beta", "branch": "Store A", "amount": 50}
    r1 = client.post("/api/purchases", json=body,
                     headers={"Authorization": "Bearer " + tok, "Idempotency-Key": "idem-po-a"})
    r2 = client.post("/api/purchases", json=body,
                     headers={"Authorization": "Bearer " + tok, "Idempotency-Key": "idem-po-b"})
    assert r1.json()["id"] != r2.json()["id"]


def test_no_key_behaves_normally():
    tok = _tok()
    body = {"vendor": "Gamma", "branch": "Store A", "amount": 25}
    r1 = client.post("/api/purchases", json=body, headers={"Authorization": "Bearer " + tok})
    r2 = client.post("/api/purchases", json=body, headers={"Authorization": "Bearer " + tok})
    assert r1.json()["id"] != r2.json()["id"]   # two distinct documents, no idempotency
    assert "Idempotency-Replayed" not in r2.headers

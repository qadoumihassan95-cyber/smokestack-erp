"""Telegram session endpoint tests backing the bot's /me command:
linked user, unlinked user, and an invalid Telegram id (must not 500)."""
import os, tempfile

_DB = os.path.join(tempfile.gettempdir(), f"smokestack_tg_{os.getpid()}.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["SEED_ON_START"] = "true"
os.environ["JWT_SECRET"] = "test-secret"

from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)


def _tok(uid="U-owner"):
    r = client.post("/api/auth/login", data={"username": uid, "password": "demo1234"})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["access_token"]}


def _link(tg_id, username="hassan"):
    h = _tok()
    code = client.post("/api/telegram/link-code", headers=h).json()["code"]
    r = client.post("/api/telegram/link/verify",
                    json={"tg_id": tg_id, "code": code, "username": username, "device": "Telegram"})
    assert r.status_code == 200, r.text


def test_session_linked_returns_full_profile():
    with TestClient(app):
        _link("999001", username="hassan")
        s = client.get("/api/telegram/session/999001").json()
        assert s["linked"] is True
        assert s["user"]["name"] and s["user"]["role"] == "owner"
        assert "branches" in s["user"]
        assert s["tg_id"] == "999001"
        assert s["username"] == "hassan"
        assert s["linked_at"] and s["status"] == "connected"


def test_session_unlinked_user():
    with TestClient(app):
        s = client.get("/api/telegram/session/424242").json()
        assert s == {"linked": False}


def test_session_invalid_tg_id():
    with TestClient(app):
        # non-numeric / bogus id must degrade gracefully, never 500
        r = client.get("/api/telegram/session/not-a-real-id")
        assert r.status_code == 200
        assert r.json() == {"linked": False}


def test_auth_token_exchange_and_reuse():
    from app.config import settings
    settings.bot_token = "TESTBOT"  # simulate the shared bot secret
    with TestClient(app):
        _link("777001", username="hassan")
        # wrong / missing bot token -> forbidden
        assert client.post("/api/telegram/auth-token", json={"tg_id": "777001"}).status_code == 403
        assert client.post("/api/telegram/auth-token", json={"tg_id": "777001"},
                           headers={"X-Bot-Token": "nope"}).status_code == 403
        # unlinked tg_id -> 404
        assert client.post("/api/telegram/auth-token", json={"tg_id": "000999"},
                           headers={"X-Bot-Token": "TESTBOT"}).status_code == 404
        # valid -> a JWT that works on an existing RBAC endpoint as the real user
        r = client.post("/api/telegram/auth-token", json={"tg_id": "777001"},
                        headers={"X-Bot-Token": "TESTBOT"})
        assert r.status_code == 200, r.text
        tok = r.json()["access_token"]
        me = client.get("/api/auth/me", headers={"Authorization": "Bearer " + tok})
        assert me.status_code == 200 and me.json()["role"] == "owner"


def test_prefs_roundtrip():
    from app.config import settings
    settings.bot_token = "TESTBOT"
    with TestClient(app):
        _link("777002", username="p")
        h = _tok()
        base = client.get("/api/telegram/prefs", headers=h).json()
        assert base["connected"] is True and base["prefs"]["low_stock"] is True
        client.put("/api/telegram/prefs", headers=h, json={"low_stock": False, "language": "es"})
        after = client.get("/api/telegram/prefs", headers=h).json()["prefs"]
        assert after["low_stock"] is False and after["language"] == "es"


def test_receive_stores_notes_and_unit_cost():
    with TestClient(app):
        h = _tok()
        r = client.post("/api/inventory/receive", headers=h,
                        json={"sku": "MRB-GLD", "branch": "Store A", "qty": 5,
                              "reason": "Supplier X · Inv 9", "unit_cost": 7.5})
        assert r.status_code == 200, r.text
        mv = client.get("/api/inventory/movements?branch=Store A", headers=h).json()
        m = next(x for x in mv if x["sku"] == "MRB-GLD" and x["type"] == "receive")
        assert abs(m["value"] - 7.5 * 5) < 0.01          # unit_cost override applied to the movement


def test_bot_audit_endpoint():
    from app.config import settings
    settings.bot_token = "TESTBOT"
    with TestClient(app):
        # bot-token gated
        assert client.post("/api/telegram/audit",
                           json={"tg_id": "1", "action": "create", "entity": "expense"}).status_code == 403
        r = client.post("/api/telegram/audit", headers={"X-Bot-Token": "TESTBOT"},
                        json={"tg_id": "55", "user_id": "U-owner", "action": "create",
                              "entity": "expense", "ref": "L-1", "detail": "telegram op"})
        assert r.status_code == 200
        logs = client.get("/api/audit?limit=20", headers=_tok()).json()
        assert any(a["source"] == "TELEGRAM" and a["entity"] == "expense" for a in logs)

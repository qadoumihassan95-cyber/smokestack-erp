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

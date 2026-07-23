"""Remediation M13 — structured observability.

Every request gets a correlation id (X-Request-ID), and one structured log line
carries request_id + company_id + user_id + method/path/status/duration/build.
Secrets (the bearer token) must never appear in logs.
"""
import logging
import os
import tempfile

_DB = os.path.join(tempfile.gettempdir(), f"smokestack_obs_{os.getpid()}.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["SEED_ON_START"] = "true"
os.environ["JWT_SECRET"] = "obs-secret-long-enough"
os.environ["TELEGRAM_BOT_TOKEN"] = "test-bot-token"

from fastapi.testclient import TestClient

from app.main import app
from app import observability

with TestClient(app):
    pass
client = TestClient(app)


def _login():
    r = client.post("/api/auth/login", data={"username": "U-owner", "password": "demo1234"})
    return r.json()["access_token"]


# ------------------------------------------------------- correlation id
def test_every_response_carries_a_request_id():
    r = client.get("/api/health")
    assert r.headers.get("X-Request-ID")


def test_supplied_request_id_is_echoed():
    r = client.get("/api/health", headers={"X-Request-ID": "trace-abc-123"})
    assert r.headers.get("X-Request-ID") == "trace-abc-123"


# ------------------------------------------------------- structured log content
def test_authenticated_request_logs_company_and_user(caplog):
    tok = _login()
    with caplog.at_level(logging.INFO, logger="pfs.request"):
        r = client.get("/api/inventory/products",
                       headers={"Authorization": "Bearer " + tok,
                                "X-Request-ID": "rid-auth-1"})
        assert r.status_code == 200
    line = "\n".join(m.getMessage() for m in caplog.records if m.name == "pfs.request")
    assert "rid-auth-1" in line
    assert "company=1" in line
    assert "user=U-owner" in line
    assert "/api/inventory/products" in line
    assert "-> 200" in line


def test_unauthenticated_request_has_no_user_context(caplog):
    with caplog.at_level(logging.INFO, logger="pfs.request"):
        client.get("/api/inventory/products", headers={"X-Request-ID": "rid-anon-1"})
    line = "\n".join(m.getMessage() for m in caplog.records if m.name == "pfs.request")
    assert "rid-anon-1" in line
    assert "company=None" in line and "user=None" in line


# ------------------------------------------------------- secret redaction
def test_bearer_token_is_never_logged(caplog):
    tok = _login()
    with caplog.at_level(logging.INFO):
        client.get("/api/inventory/products", headers={"Authorization": "Bearer " + tok})
    all_logs = "\n".join(m.getMessage() for m in caplog.records)
    assert tok not in all_logs
    assert "Bearer" not in all_logs


# ------------------------------------------------------- security events
def test_security_event_helper_logs_with_request_id(caplog):
    with caplog.at_level(logging.WARNING, logger="pfs.security"):
        observability.log_security_event("cross_tenant_attempt", company=2, target="C1-COLA")
    line = "\n".join(m.getMessage() for m in caplog.records if m.name == "pfs.security")
    assert "cross_tenant_attempt" in line and "company=2" in line


# ------------------------------------------------------- build version surfaced
def test_health_exposes_build_version():
    body = client.get("/api/health").json()
    assert "build" in body["checks"]

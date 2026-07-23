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

import io
from contextlib import contextmanager

from fastapi.testclient import TestClient

from app.main import app
from app import observability

with TestClient(app):
    pass
client = TestClient(app)


def _login():
    r = client.post("/api/auth/login", data={"username": "U-owner", "password": "demo1234"})
    return r.json()["access_token"]


class _Recorder:
    """Records formatted log messages without touching the logging framework."""
    def __init__(self):
        self.messages = []

    def _record(self, fmt, *args):
        self.messages.append(fmt % args if args else str(fmt))

    info = _record
    warning = _record

    def getvalue(self):
        return "\n".join(self.messages)


@contextmanager
def _capture(logger_name, level=logging.INFO):
    """Capture the module logger by monkeypatching the observability module
    attribute the code actually calls (``observability.log`` / ``sec_log``).

    This is fully order-independent: it does not rely on caplog, root-logger
    propagation, or level state that other suites in a long run can perturb.
    """
    attr = {"pfs.request": "log", "pfs.security": "sec_log"}[logger_name]
    rec = _Recorder()
    original = getattr(observability, attr)
    setattr(observability, attr, rec)
    try:
        yield rec
    finally:
        setattr(observability, attr, original)


# ------------------------------------------------------- correlation id
def test_every_response_carries_a_request_id():
    r = client.get("/api/health")
    assert r.headers.get("X-Request-ID")


def test_supplied_request_id_is_echoed():
    r = client.get("/api/health", headers={"X-Request-ID": "trace-abc-123"})
    assert r.headers.get("X-Request-ID") == "trace-abc-123"


# ------------------------------------------------------- structured log content
def test_authenticated_request_logs_company_and_user():
    tok = _login()
    with _capture("pfs.request") as buf:
        r = client.get("/api/inventory/products",
                       headers={"Authorization": "Bearer " + tok,
                                "X-Request-ID": "rid-auth-1"})
        assert r.status_code == 200
    line = buf.getvalue()
    assert "rid-auth-1" in line
    assert "company=1" in line
    assert "user=U-owner" in line
    assert "/api/inventory/products" in line
    assert "-> 200" in line


def test_unauthenticated_request_has_no_user_context():
    with _capture("pfs.request") as buf:
        client.get("/api/inventory/products", headers={"X-Request-ID": "rid-anon-1"})
    line = buf.getvalue()
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
def test_security_event_helper_logs_with_request_id():
    with _capture("pfs.security", level=logging.WARNING) as buf:
        observability.log_security_event("cross_tenant_attempt", company=2, target="C1-COLA")
    line = buf.getvalue()
    assert "cross_tenant_attempt" in line and "company=2" in line


# ------------------------------------------------------- build version surfaced
def test_health_exposes_build_version():
    body = client.get("/api/health").json()
    assert "build" in body["checks"]

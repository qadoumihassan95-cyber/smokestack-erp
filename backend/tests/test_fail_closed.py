"""Remediation M2 — fail-closed tenant context + explicit system context.

Proves tenant data cannot be read or written on a STRICT session without a
company context, that a deliberate SYSTEM context is the only sanctioned global
bypass, that a company-scoped session isolates correctly, and that the fail-open
legacy default is unchanged for authenticated Company #1 (backward compatible).
"""
import os
import tempfile

_DB = os.path.join(tempfile.gettempdir(), f"smokestack_failclosed_{os.getpid()}.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["SEED_ON_START"] = "true"
os.environ["JWT_SECRET"] = "failclosed-secret-long-enough"
os.environ["TELEGRAM_BOT_TOKEN"] = "test-bot-token"

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.database import SessionLocal
from app import models, tenancy
from app.tenancy import TenantContextError

with TestClient(app):        # boot: schema + Company #1 seed
    pass
client = TestClient(app)


def setup_module(_m):
    # a second company's product, written via an explicit company-2 session
    with tenancy.tenant_session(2) as db:
        if not db.query(models.Product).filter(models.Product.sku == "C2-STRICT").first():
            db.add(models.Product(sku="C2-STRICT", name="C2 item", status="active"))
            db.commit()


# ---------------------------------------------------------------- strict reads
def test_strict_session_tenant_read_fails_closed():
    db = SessionLocal()
    tenancy.make_strict(db)                 # strict, but NO company context
    try:
        with pytest.raises(TenantContextError):
            db.query(models.Product).all()
    finally:
        db.close()


def test_strict_session_allows_platform_reads():
    # platform tables are not tenant-owned — a strict session may read them
    db = SessionLocal()
    tenancy.make_strict(db)
    try:
        db.query(models.Company).all()      # must not raise
    finally:
        db.close()


# --------------------------------------------------------------- strict writes
def test_strict_session_tenant_write_fails_closed():
    db = SessionLocal()
    tenancy.make_strict(db)
    try:
        db.add(models.Product(sku="NOPE", name="x", status="active"))
        with pytest.raises(TenantContextError):
            db.commit()
    finally:
        db.rollback()
        db.close()


# ----------------------------------------------------------- system context
def test_system_context_is_the_only_global_bypass():
    with tenancy.system_session() as db:
        skus = {p.sku for p in db.query(models.Product).all()}
    # sees every company's rows (deliberate, audited global maintenance)
    assert "C2-STRICT" in skus


def test_company_session_isolates():
    with tenancy.tenant_session(2) as db:
        skus = {p.sku for p in db.query(models.Product).all()}
    assert "C2-STRICT" in skus
    # Company #1's seeded products must not appear in a company-2 session
    assert "MRB-GLD" not in skus


# ----------------------------------------------------------- require_company dep
def test_require_company_fails_closed_without_context():
    class _U:  # a user object with no resolved company
        pass
    with pytest.raises(Exception):
        tenancy.require_company(_U())


def test_require_company_returns_resolved_company():
    class _U:
        _company_id = 7
    assert tenancy.require_company(_U()) == 7


# --------------------------------------------------- backward compatibility
def test_set_session_company_clears_system_flag():
    db = SessionLocal()
    tenancy.use_system_context(db)
    assert db.info.get("system") is True
    tenancy.set_session_company(db, 1)
    assert db.info.get("system") is False
    assert db.info.get("company_id") == 1
    db.close()


def test_legacy_authenticated_company_one_still_works():
    # existing Company #1 login + a representative endpoint unchanged
    r = client.post("/api/auth/login", data={"username": "U-owner", "password": "demo1234"})
    assert r.status_code == 200
    h = {"Authorization": "Bearer " + r.json()["access_token"]}
    assert client.get("/api/inventory/products", headers=h).status_code == 200
    assert client.get("/api/reports/dashboard?branch=all", headers=h).status_code == 200

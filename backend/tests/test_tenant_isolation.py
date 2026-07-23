"""Phase 1 M3 + M4 — tenant resolution, realms, and company isolation.

Creates a SECOND company with its own user and data, then proves:
  * new ERP tokens carry realm=erp + company_id;
  * legacy tokens (no company claim) resolve to Company #1 and keep working;
  * a PFS token is rejected by the ERP, and an ERP token is rejected by PFS;
  * the scoping engine isolates reads/writes between two companies (session level
    and end-to-end HTTP), and cross-company id access returns 404;
  * the impersonation foundation mints a short-lived company-scoped ERP token
    carrying the initiating Super Admin id.
"""
import os
import tempfile

_DB = os.path.join(tempfile.gettempdir(), f"smokestack_tenant_{os.getpid()}.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["SEED_ON_START"] = "true"
os.environ["JWT_SECRET"] = "erp-secret-long-enough-for-tenant-tests"
os.environ["PFS_JWT_SECRET"] = "pfs-secret-distinct"
os.environ["TELEGRAM_BOT_TOKEN"] = "test-bot-token"

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from jose import jwt

from app.main import app
from app.database import SessionLocal
from app import models, security, tenancy
from app.config import settings

# boot once so the schema + Company #1 seed exist
with TestClient(app):
    pass
client = TestClient(app)

C1, C2 = 1, 2


def _seed_second_company():
    db = SessionLocal()
    try:
        if not db.query(models.Company).filter(models.Company.id == C2).first():
            db.add(models.Company(id=C2, name="Acme Two", slug="acme-two",
                                  application_key="smoke_shop", owner_user_id="U2-owner",
                                  status="active"))
        if not db.get(models.User, "U2-owner"):
            u = models.User(id="U2-owner", name="Owner Two", role="owner",
                            password_hash=security.hash_pw("demo1234"), status="active")
            u.company_id = C2
            db.add(u)
        db.commit()
    finally:
        db.close()

    # company-scoped product inserts (before_flush stamps company_id)
    for cid, sku, bar in [(C1, "C1-COLA", "C1BAR"), (C2, "C2-COLA", "C2BAR")]:
        db = SessionLocal()
        try:
            tenancy.set_session_company(db, cid)
            if not db.query(models.Product).filter(models.Product.sku == sku).first():
                db.add(models.Product(sku=sku, name=f"Cola {cid}", barcode=bar, status="active"))
                db.commit()
        finally:
            db.close()


def setup_module(_m):
    _seed_second_company()


def _login(user, pw="demo1234"):
    r = client.post("/api/auth/login", data={"username": user, "password": pw})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _h(tok):
    return {"Authorization": "Bearer " + tok}


# ------------------------------------------------------- token / realm shape
def test_new_erp_token_carries_realm_and_company():
    tok = _login("U-owner")
    claims = jwt.decode(tok, settings.jwt_secret, algorithms=[settings.jwt_alg])
    assert claims["realm"] == "erp"
    assert claims["company_id"] == C1


def test_legacy_token_without_company_resolves_to_company_one():
    # a token minted the OLD way — no realm, no company_id
    exp = datetime.now(timezone.utc) + timedelta(minutes=30)
    legacy = jwt.encode({"sub": "U-owner", "role": "owner", "exp": exp},
                        settings.jwt_secret, algorithm=settings.jwt_alg)
    me = client.get("/api/auth/me", headers=_h(legacy))
    assert me.status_code == 200 and me.json()["id"] == "U-owner"
    d = client.get("/api/reports/dashboard?branch=all", headers=_h(legacy))
    assert d.status_code == 200


def test_pfs_token_is_rejected_by_erp():
    exp = datetime.now(timezone.utc) + timedelta(minutes=30)
    pfs_tok = jwt.encode({"sub": "SA-root", "realm": "pfs", "exp": exp},
                         os.environ["PFS_JWT_SECRET"], algorithm="HS256")
    r = client.get("/api/auth/me", headers=_h(pfs_tok))
    assert r.status_code == 401


def test_erp_token_is_rejected_by_pfs():
    tok = _login("U-owner")
    r = client.get("/pfs/auth/me", headers=_h(tok))
    assert r.status_code == 401


# ------------------------------------------------------- session-level scoping
def test_session_scoping_isolates_reads_and_stamps_writes():
    # tagged sessions see only their own company's rows
    d1 = SessionLocal(); tenancy.set_session_company(d1, C1)
    d2 = SessionLocal(); tenancy.set_session_company(d2, C2)
    try:
        s1 = {p.sku for p in d1.query(models.Product).all()}
        s2 = {p.sku for p in d2.query(models.Product).all()}
        assert "C1-COLA" in s1 and "C2-COLA" not in s1
        assert "C2-COLA" in s2 and "C1-COLA" not in s2
    finally:
        d1.close(); d2.close()
    # an untagged session sees everything (backward compatible)
    d0 = SessionLocal()
    try:
        allskus = {p.sku for p in d0.query(models.Product).all()}
        assert {"C1-COLA", "C2-COLA"} <= allskus
    finally:
        d0.close()


# ------------------------------------------------------- HTTP isolation
def test_products_listing_is_company_scoped():
    p1 = client.get("/api/inventory/products", headers=_h(_login("U-owner"))).json()
    skus1 = {p["sku"] for p in p1}
    assert "C1-COLA" in skus1 and "C2-COLA" not in skus1

    p2 = client.get("/api/inventory/products", headers=_h(_login("U2-owner"))).json()
    skus2 = {p["sku"] for p in p2}
    assert "C2-COLA" in skus2 and "C1-COLA" not in skus2


def test_cross_company_read_by_id_returns_404():
    # company-1 owner asking for company-2's product barcode -> not found
    r = client.get("/api/inventory/barcode/C2BAR", headers=_h(_login("U-owner")))
    assert r.status_code == 404


def test_cross_company_update_returns_404():
    r = client.patch("/api/inventory/products/C2-COLA", json={"name": "hacked"},
                     headers=_h(_login("U-owner")))
    assert r.status_code == 404
    # and company-2's product is untouched
    d = SessionLocal()
    try:
        p = d.query(models.Product).filter(models.Product.sku == "C2-COLA").first()
        assert p.name != "hacked"
    finally:
        d.close()


# ------------------------------------------------------- impersonation foundation
def test_impersonation_token_scopes_to_target_company_with_metadata():
    tok = tenancy.mint_impersonation_token(
        target_company_id=C2, target_user_id="U2-owner", super_admin_id="SA-root")
    claims = jwt.decode(tok, settings.jwt_secret, algorithms=[settings.jwt_alg])
    assert claims["imp"] is True and claims["sa"] == "SA-root" and claims["company_id"] == C2
    # used against the ERP it sees ONLY company-2 data
    p = client.get("/api/inventory/products", headers=_h(tok)).json()
    skus = {x["sku"] for x in p}
    assert "C2-COLA" in skus and "C1-COLA" not in skus

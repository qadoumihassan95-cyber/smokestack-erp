"""PFS Platform — Phase 0 foundation. Verifies the additive multi-tenant
foundation seeds correctly and, crucially, that NO existing tenant behaviour
changed (the live SmokeStack business is Company #1 and still works exactly as
before).
"""
import os, tempfile, json

_DB = os.path.join(tempfile.gettempdir(), f"smokestack_plat_{os.getpid()}.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["SEED_ON_START"] = "true"
os.environ["JWT_SECRET"] = "platform-secret-long-enough"
os.environ["TELEGRAM_BOT_TOKEN"] = "test-bot-token"

from fastapi.testclient import TestClient
from app.main import app
from app.database import SessionLocal
from app import models
from app.apps import load_apps
from app.platform import registry
from app.platform.seed import seed_platform

load_apps()   # applications self-register (startup does this too)
client = TestClient(app)


def _db():
    return SessionLocal()


# ------------------------------------------------------------- registry seed
def test_applications_and_modules_seeded_from_registered_apps():
    with TestClient(app):
        db = _db()
        try:
            apps = {a.key for a in db.query(models.Application).all()}
            mods = {m.key for m in db.query(models.Module).all()}
            assert apps == {a.key for a in registry.applications()}
            assert mods == set(registry.all_module_specs().keys())
            # smoke_shop is the only live application; the catalog types are inactive
            assert db.get(models.Application, "smoke_shop").active is True
            assert db.get(models.Application, "retail").active is False
        finally:
            db.close()


def test_registry_is_internally_consistent():
    specs = registry.all_module_specs()
    valid = set(specs.keys())
    for key, (app_key, spec) in specs.items():
        for dep in spec.depends_on:
            assert dep in valid, f"{key} depends on unknown module {dep}"
    app_keys = {a.key for a in registry.applications()} | {"core"}
    for key, (app_key, spec) in specs.items():
        assert app_key in app_keys


# -------------------------------- BUSINESS-AGNOSTIC PLATFORM (the constraint)
def test_platform_layer_contains_no_business_specifics():
    """The platform package must not hardcode any business identity — all of that
    lives inside app/apps/. This guards the 'platform is business-agnostic' rule."""
    import os
    plat_dir = os.path.join(os.path.dirname(__file__), "..", "app", "platform")
    forbidden = ["smoke", "smokestack", "u-owner", "vape", "tobacco", "cigarette"]
    offenders = []
    for fn in os.listdir(plat_dir):
        if not fn.endswith(".py"):
            continue
        src = open(os.path.join(plat_dir, fn), encoding="utf-8").read().lower()
        for term in forbidden:
            if term in src:
                offenders.append(f"{fn}: '{term}'")
    assert not offenders, "business specifics leaked into the platform layer: " + ", ".join(offenders)


def test_smoke_shop_specifics_live_in_the_app_layer():
    # the founding company + its modules are defined by the smoke_shop APP, not the platform
    from app.apps import smoke_shop
    assert smoke_shop.SMOKE_SHOP.bootstrap is not None
    assert any(m.key == "inventory" for m in smoke_shop.MODULES)
    # the platform registry knows nothing until an app registers
    assert registry.get_application("smoke_shop").name == "Smoke Shop ERP"


# --------------------------------------------------------------- Company #1
def test_company_one_is_the_existing_smokestack_business():
    with TestClient(app):
        db = _db()
        try:
            c = db.query(models.Company).filter(models.Company.slug == "smokestack").first()
            assert c is not None and c.id == 1
            assert c.name == "SmokeStack"
            assert c.application_key == "smoke_shop"
            assert c.owner_user_id == "U-owner"
            assert c.status == "active"
            # every module enabled for the founding company
            cm = db.query(models.CompanyModule).filter(models.CompanyModule.company_id == c.id).count()
            assert cm == len(registry.all_module_specs())
            # lifetime subscription
            sub = db.query(models.Subscription).filter(models.Subscription.company_id == c.id).first()
            assert sub and sub.plan == "lifetime" and sub.status == "active"
        finally:
            db.close()


def test_seed_is_idempotent():
    with TestClient(app):
        db = _db()
        try:
            before = db.query(models.Company).count()
            seed_platform(db)
            seed_platform(db)
            assert db.query(models.Company).count() == before   # no duplicate companies
            # no duplicate module rows for Company #1 either
            cm = db.query(models.CompanyModule).filter(models.CompanyModule.company_id == 1).count()
            assert cm == len(registry.all_module_specs())
        finally:
            db.close()


def test_platform_audit_table_is_writable():
    with TestClient(app):
        db = _db()
        try:
            db.add(models.PlatformAudit(super_admin_id="SA-root", action="test",
                                        entity="company", ref="1", company_id=1,
                                        detail="phase0 check"))
            db.commit()
            assert db.query(models.PlatformAudit).filter_by(action="test").count() == 1
        finally:
            db.close()


# ------------------------------------------- REGRESSION: tenant app unchanged
def test_existing_tenant_login_and_dashboard_unchanged():
    with TestClient(app):
        r = client.post("/api/auth/login", data={"username": "U-owner", "password": "demo1234"})
        assert r.status_code == 200 and r.json().get("access_token")
        h = {"Authorization": "Bearer " + r.json()["access_token"]}
        # a representative existing endpoint still works exactly as before
        d = client.get("/api/reports/dashboard?branch=all", headers=h)
        assert d.status_code == 200
        me = client.get("/api/auth/me", headers=h)
        assert me.status_code == 200 and me.json().get("id") == "U-owner"


def test_platform_tables_do_not_leak_into_tenant_api():
    # there is no tenant endpoint exposing companies/platform data yet (Phase 3)
    with TestClient(app):
        r = client.post("/api/auth/login", data={"username": "U-owner", "password": "demo1234"})
        h = {"Authorization": "Bearer " + r.json()["access_token"]}
        # the tenant OpenAPI must not expose /api/pfs in Phase 0
        paths = client.get("/openapi.json").json()["paths"]
        assert not any(p.startswith("/api/pfs") for p in paths), "no super-admin API in Phase 0"

"""Remediation M12 — plugin + startup failure isolation + DB-aware health.

A broken OPTIONAL application plugin, or one application's failed bootstrap, must
be quarantined (recorded, skipped) rather than crashing the platform. Health must
verify DB connectivity + the core registry and surface quarantined failures,
returning 503 (not a silent OK) when a hard dependency is down.
"""
import os
import tempfile

_DB = os.path.join(tempfile.gettempdir(), f"smokestack_startup_{os.getpid()}.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["SEED_ON_START"] = "true"
os.environ["JWT_SECRET"] = "startup-secret-long-enough"
os.environ["TELEGRAM_BOT_TOKEN"] = "test-bot-token"

from fastapi.testclient import TestClient

from app.main import app
from app import apps as loader
from app.platform import registry
from app.platform import seed as platform_seed
from app.platform.registry import AppDescriptor

with TestClient(app):
    pass
client = TestClient(app)


def setup_module(_m):
    loader.load_apps()


# ---------------------------------------------------- plugin quarantine
def test_broken_plugin_is_quarantined_not_fatal():
    good_before = {a.key for a in registry.applications()}
    ok = loader._load_one("does_not_exist_zzz")   # import will raise
    assert ok is False
    # the failure is recorded...
    assert any(f["module"] == "does_not_exist_zzz" for f in loader.load_failures())
    # ...and the registry is UNCHANGED (no partial/dangling registration)
    assert {a.key for a in registry.applications()} == good_before


def test_partial_registration_is_rolled_back(monkeypatch):
    # simulate a plugin that registers an app THEN raises mid-import
    import importlib

    def _boom(_modpath):
        registry.register_application(AppDescriptor(key="halfbaked", name="Half"))
        raise RuntimeError("exploded after registering")

    monkeypatch.setattr(importlib, "import_module", _boom)
    before = {a.key for a in registry.applications()}
    assert loader._load_one("halfbaked_mod") is False
    # the half-registered app must have been rolled back
    assert "halfbaked" not in {a.key for a in registry.applications()}
    assert {a.key for a in registry.applications()} == before


# ---------------------------------------------------- bootstrap isolation
def test_one_failed_bootstrap_does_not_stop_others():
    from app.database import SessionLocal
    from app import tenancy

    boomed = {"ran": False}

    def bad_bootstrap(db):
        boomed["ran"] = True
        raise RuntimeError("provisioning failed for this app")

    registry.register_application(AppDescriptor(key="tmp_badboot", name="BadBoot",
                                                bootstrap=bad_bootstrap))
    try:
        db = SessionLocal(); tenancy.use_system_context(db)
        try:
            platform_seed.run_bootstraps(db)   # must NOT raise
        finally:
            db.close()
        assert boomed["ran"] is True
        assert any(f["app"] == "tmp_badboot" for f in platform_seed.bootstrap_failures())
        # the real founding company still provisioned
        db = SessionLocal()
        try:
            from app import models
            c = db.query(models.Company).filter(models.Company.slug == "smokestack").first()
            assert c is not None
        finally:
            db.close()
    finally:
        registry._REGISTRY.pop("tmp_badboot", None)


# ---------------------------------------------------- DB-aware health
def test_health_reports_db_and_registry():
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["checks"]["database"] == "ok"
    assert body["checks"]["applications"] >= 1


def test_health_is_degraded_503_when_db_unreachable(monkeypatch):
    from app import main as main_mod

    class _BrokenEngine:
        def connect(self):
            raise RuntimeError("db down")

    monkeypatch.setattr(main_mod, "engine", _BrokenEngine())
    r = client.get("/api/health")
    assert r.status_code == 503
    assert r.json()["status"] == "degraded"
    assert "error" in r.json()["checks"]["database"]


# ---------------------------------------------------- idempotent seed rerun
def test_seed_rerun_is_idempotent():
    from app.database import SessionLocal
    from app import tenancy, models
    db = SessionLocal(); tenancy.use_system_context(db)
    try:
        before = db.query(models.Company).count()
        platform_seed.seed_platform(db)
        platform_seed.seed_platform(db)
        assert db.query(models.Company).count() == before
    finally:
        db.close()

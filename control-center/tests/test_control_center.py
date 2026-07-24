"""PFS Control Center — Milestone-1 foundation tests (hermetic, SQLite)."""
import os
import tempfile

_DB = os.path.join(tempfile.gettempdir(), f"pfs_cc_{os.getpid()}.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["JWT_SECRET"] = "cc-test-secret"
os.environ["SEED_ON_START"] = "true"
os.environ["SEED_PASSWORD"] = "owner-test-pw"

from fastapi.testclient import TestClient

from main import app

with TestClient(app):   # triggers startup: create_all + seed
    pass
client = TestClient(app)


def _tok():
    r = client.post("/auth/login", data={"username": "OP-owner", "password": "owner-test-pw"})
    assert r.status_code == 200
    return r.json()["access_token"]


def _h():
    return {"Authorization": "Bearer " + _tok()}


# ------------------------------- health & auth -------------------------------
def test_health_ok():
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"
    assert r.json()["checks"]["database"] == "ok"


def test_login_bad_password_rejected():
    assert client.post("/auth/login", data={"username": "OP-owner", "password": "nope"}).status_code == 401


def test_api_requires_operator_auth():
    assert client.get("/api/products").status_code == 401           # no token


# --------------------- seeded fleet: SmokeStack registered safely ---------------------
def test_smokestack_product_and_master_environments_seeded():
    prods = {p["id"] for p in client.get("/api/products", headers=_h()).json()}
    assert "smokestack" in prods
    envs = client.get("/api/products/smokestack/environments", headers=_h()).json()
    kinds = {e["kind"] for e in envs}
    assert kinds == {"master_development", "master_testing", "master_production"}


def test_smokestack_registered_as_customer_production_runtime():
    rts = client.get("/api/runtimes", headers=_h()).json()
    cp = [r for r in rts if r["erp_product_id"] == "smokestack" and r["tier"] == "customer"]
    assert len(cp) == 1
    assert cp[0]["environment_kind"] == "customer_production"
    assert "smokestack-api.onrender.com" in (cp[0]["health_url"] or "")


def test_imported_legacy_release_present_and_marked():
    rels = client.get("/api/releases", headers=_h()).json()
    legacy = [r for r in rels if r["is_legacy_import"]]
    assert len(legacy) == 1
    assert legacy[0]["status"] == "imported_legacy" and legacy[0]["erp_product_id"] == "smokestack"


def test_company_one_reference_and_deployment_seeded():
    custs = client.get("/api/customers", headers=_h()).json()
    c1 = [c for c in custs if c["erp_product_id"] == "smokestack" and c["external_ref"] == "1"]
    assert len(c1) == 1
    deps = client.get("/api/customer-deployments", headers=_h()).json()
    assert any(d["tenant_ref"] == "1" for d in deps)
    assert client.get("/api/deployments", headers=_h()).json()   # at least one observed deployment


# ------------------------------- registry writes + audit -------------------------------
def test_register_product_creates_envs_and_audit():
    r = client.post("/api/products", headers=_h(),
                    json={"id": "dairy", "name": "Dairy ERP", "description": "future"})
    assert r.status_code == 201
    envs = client.get("/api/products/dairy/environments", headers=_h()).json()
    assert {e["kind"] for e in envs} == {"master_development", "master_testing", "master_production"}
    actions = [a for a in client.get("/api/audit", headers=_h()).json()
               if a["target_type"] == "erp_product" and a["target_id"] == "dairy"]
    assert actions and actions[0]["action"] == "create"


# ------------------------------- release provenance (ADR-028 / Decision 3) -------------------------------
def test_only_master_production_may_publish_a_release():
    bad = client.post("/api/releases", headers=_h(), json={
        "erp_product_id": "smokestack", "version": "9.9.9",
        "source_environment_kind": "master_testing"})
    assert bad.status_code == 400                                   # not from master_production
    good = client.post("/api/releases", headers=_h(), json={
        "erp_product_id": "smokestack", "version": "1.1.0", "source_sha": "abc123",
        "source_environment_kind": "master_production", "source_master_runtime": "sm-master-prod"})
    assert good.status_code == 201 and good.json()["status"] == "published"


def test_legacy_import_release_allowed_and_marked():
    r = client.post("/api/releases", headers=_h(), json={
        "erp_product_id": "smokestack", "version": "0.9-legacy", "is_legacy_import": True})
    assert r.status_code == 201 and r.json()["status"] == "imported_legacy"


# ------------------------------- read-only health polling -------------------------------
def test_health_check_records_unreachable_for_bad_url():
    rid = client.post("/api/runtimes", headers=_h(), json={
        "erp_product_id": "smokestack", "tier": "customer", "environment_kind": "customer_production",
        "name": "bad-runtime", "health_url": "http://127.0.0.1:9/nope"}).json()["id"]
    r = client.post(f"/api/runtimes/{rid}/health-check", headers=_h())
    assert r.status_code == 200 and r.json()["health"] == "unreachable"
    rt = next(x for x in client.get("/api/runtimes", headers=_h()).json() if x["id"] == rid)
    assert rt["last_health_state"] == "unreachable"


def test_fleet_summary_shape():
    f = client.get("/api/fleet", headers=_h()).json()
    assert f["products"] >= 1 and f["runtimes"] >= 1 and "by_health" in f

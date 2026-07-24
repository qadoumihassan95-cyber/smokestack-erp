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


# ------------------------------- ERP details aggregate + enriched lists -------------------------------
def test_product_overview_aggregate():
    o = client.get("/api/products/smokestack/overview", headers=_h()).json()
    assert o["product"]["id"] == "smokestack"
    assert {e["kind"] for e in o["environments"]} == {"master_development", "master_testing", "master_production"}
    cp = [r for r in o["runtimes"] if r["tier"] == "customer"]
    assert cp and "current_release_version" in cp[0] and "current_release_is_legacy" in cp[0]
    assert any(r["is_legacy_import"] for r in o["releases"])
    cd = o["customer_deployments"]
    assert cd and cd[0]["customer_name"] and cd[0]["tenant_ref"] == "1"
    assert "release_version" in cd[0] and "runtime_name" in cd[0]
    assert isinstance(o["deployments"], list) and isinstance(o["audit"], list)


def test_overview_404_for_unknown_product():
    assert client.get("/api/products/does-not-exist/overview", headers=_h()).status_code == 404


def test_customer_deployments_and_deployments_are_enriched():
    cds = client.get("/api/customer-deployments", headers=_h()).json()
    assert cds and {"customer_name", "release_version", "runtime_name", "erp_product_id"} <= set(cds[0])
    deps = client.get("/api/deployments", headers=_h()).json()
    assert deps and {"runtime_name", "release_version"} <= set(deps[0])


# =========================================================================================
#                    Milestone 1.1 — accountant model (Licenses, Sessions, Home)
# =========================================================================================

# ------------------------------- Home grid (My ERP Products) -------------------------------
def test_home_grid_cards_shape():
    h = client.get("/api/home", headers=_h()).json()
    assert h["operator"]["id"] == "OP-owner" and h["operator"]["role"] == "owner"
    sm = [p for p in h["products"] if p["id"] == "smokestack"]
    assert sm and {"customers", "active_licenses", "current_version", "erp_health",
                   "last_activity"} <= set(sm[0])
    assert sm[0]["customers"] >= 1


def test_home_requires_auth():
    assert client.get("/api/home").status_code == 401


# ------------------------------- enriched customers (the heart) -------------------------------
def test_product_customers_enriched_and_honest_health():
    rows = client.get("/api/products/smokestack/customers", headers=_h()).json()
    assert rows
    r = rows[0]
    assert {"name", "external_ref", "license_plan", "license_status", "current_version",
            "health", "health_source", "last_sync_state", "deployment_type"} <= set(r)
    # honesty: per-customer sync is explicitly NOT fabricated
    assert r["last_sync_state"] == "not_yet_integrated" and r["last_sync_at"] is None
    assert r["health_source"] in ("inherited_from_runtime", "unknown")


def test_product_customers_search_and_status_filter():
    all_rows = client.get("/api/products/smokestack/customers", headers=_h()).json()
    hit = client.get("/api/products/smokestack/customers?q=company", headers=_h()).json()
    assert len(hit) >= 1 and len(hit) <= len(all_rows)
    miss = client.get("/api/products/smokestack/customers?q=zzz-no-match", headers=_h()).json()
    assert miss == []
    active = client.get("/api/products/smokestack/customers?status=active", headers=_h()).json()
    assert all(x["status"] == "active" for x in active)


def test_customers_endpoint_404_unknown_product():
    assert client.get("/api/products/nope/customers", headers=_h()).status_code == 404


# ------------------------------- Licenses (first-class CRUD) -------------------------------
def _a_customer_id():
    return client.get("/api/products/smokestack/customers", headers=_h()).json()[0]["id"]


def test_license_seeded_for_company_one():
    lics = client.get("/api/licenses?erp_product_id=smokestack", headers=_h()).json()
    assert any(x["status"] == "active" for x in lics)


def test_license_create_update_and_status_validation():
    cid = _a_customer_id()
    r = client.post("/api/licenses", headers=_h(), json={
        "erp_product_id": "smokestack", "customer_ref_id": cid, "plan": "pro",
        "status": "trial", "seat_limit": 5, "start_date": "2026-07-01"})
    assert r.status_code == 201 and r.json()["license"]["plan"] == "pro"
    lid = r.json()["id"]
    # patch transitions status; audited
    up = client.patch(f"/api/licenses/{lid}", headers=_h(), json={"status": "active", "seat_limit": 9})
    assert up.status_code == 200 and up.json()["license"]["status"] == "active"
    assert up.json()["license"]["seat_limit"] == 9
    # invalid status rejected on both create and patch
    assert client.post("/api/licenses", headers=_h(), json={
        "erp_product_id": "smokestack", "customer_ref_id": cid, "status": "bogus"}).status_code == 422
    assert client.patch(f"/api/licenses/{lid}", headers=_h(), json={"status": "bogus"}).status_code == 422


def test_license_create_rejects_unknown_refs():
    assert client.post("/api/licenses", headers=_h(), json={
        "erp_product_id": "nope", "customer_ref_id": 1}).status_code == 404
    assert client.post("/api/licenses", headers=_h(), json={
        "erp_product_id": "smokestack", "customer_ref_id": 999999}).status_code == 404


def test_license_patch_404_unknown():
    assert client.patch("/api/licenses/999999", headers=_h(), json={"status": "active"}).status_code == 404


def test_licenses_require_auth():
    assert client.get("/api/licenses").status_code == 401
    assert client.post("/api/licenses", json={"erp_product_id": "smokestack", "customer_ref_id": 1}).status_code == 401


# ------------------------------- Support Sessions (Open ERP) -------------------------------
def test_open_support_session_is_pending_and_never_authenticates():
    cid = _a_customer_id()
    r = client.post("/api/support-sessions", headers=_h(), json={
        "erp_product_id": "smokestack", "customer_ref_id": cid, "reason": "help"})
    assert r.status_code == 201
    s = r.json()["session"]
    assert s["status"] == "pending_erp_integration"          # ERP-side consumption deferred
    assert s["session_ref"].startswith("sess_")               # opaque, non-authenticating
    assert s["expires_at"] and s["capabilities"] == "support:read"   # short-lived + restricted default
    assert "Pending ERP Integration" in r.json()["note"]


def test_open_session_rejects_mismatched_customer():
    # a customer that does not belong to the product
    assert client.post("/api/support-sessions", headers=_h(), json={
        "erp_product_id": "smokestack", "customer_ref_id": 999999}).status_code == 404


def test_support_session_revoke_is_terminal_and_audited():
    cid = _a_customer_id()
    sid = client.post("/api/support-sessions", headers=_h(), json={
        "erp_product_id": "smokestack", "customer_ref_id": cid}).json()["id"]
    rv = client.post(f"/api/support-sessions/{sid}/revoke", headers=_h())
    assert rv.status_code == 200 and rv.json()["session"]["status"] == "revoked"
    # idempotent
    assert client.post(f"/api/support-sessions/{sid}/revoke", headers=_h()).json()["session"]["status"] == "revoked"
    acts = [a for a in client.get("/api/audit", headers=_h()).json()
            if a["target_type"] == "support_session" and a["target_id"] == str(sid)]
    assert any(a["action"] == "revoke_support_session" for a in acts)


def test_support_session_expiry_is_time_derived():
    cid = _a_customer_id()
    sid = client.post("/api/support-sessions", headers=_h(), json={
        "erp_product_id": "smokestack", "customer_ref_id": cid, "minutes": 1}).json()["id"]
    # force expiry in the past and confirm the read model reports 'expired' without a writer job
    with TestClient(app):
        pass
    from database import SessionLocal
    import models as M
    import datetime as _dt
    db = SessionLocal()
    row = db.get(M.SupportSession, sid)
    row.expires_at = _dt.datetime.utcnow() - _dt.timedelta(minutes=5)
    db.commit()
    db.close()
    got = [s for s in client.get("/api/support-sessions?erp_product_id=smokestack", headers=_h()).json()
           if s["id"] == sid][0]
    assert got["status"] == "expired"


def test_sessions_require_auth():
    assert client.get("/api/support-sessions").status_code == 401


# ------------------------------- overview aggregate (accountant) -------------------------------
def test_overview_includes_customers_licenses_sessions_and_summary():
    o = client.get("/api/products/smokestack/overview", headers=_h()).json()
    assert {"summary", "customers", "licenses", "support_sessions"} <= set(o)
    s = o["summary"]
    assert {"customers", "active_licenses", "versions", "current_version",
            "open_sessions", "erp_health"} <= set(s)
    assert s["customers"] == len(o["customers"])


# ------------------------------- no transactional data leaks -------------------------------
def test_control_center_exposes_no_erp_transactional_tables():
    # The control plane must never model customer business data (invoices, stock, payroll, etc.)
    import models as M
    tables = set(M.Base.metadata.tables.keys())
    forbidden = {"invoices", "stock", "payroll", "expenses", "customers_erp",
                 "products", "orders", "transactions", "accounts"}
    assert tables.isdisjoint(forbidden), f"leak: {tables & forbidden}"
    # sanity: the metadata-only tables we DO expect are present
    assert {"licenses", "support_sessions", "customer_refs", "erp_products"} <= tables


# =========================================================================================
#                    Premium UI backing endpoints (dashboard + global search)
# =========================================================================================
def test_dashboard_widgets_shape():
    d = client.get("/api/dashboard", headers=_h()).json()
    assert {"fleet", "newest_products", "newest_customers", "recent_sessions",
            "latest_updates", "license_summary", "recent_activity"} <= set(d)
    assert {"products", "customers", "active_licenses", "open_sessions", "by_health"} <= set(d["fleet"])
    assert d["fleet"]["products"] >= 1
    assert isinstance(d["license_summary"]["by_status"], dict)


def test_dashboard_requires_auth():
    assert client.get("/api/dashboard").status_code == 401


def test_global_search_finds_across_entities():
    # seed data guarantees a SmokeStack product + Company #1 + an active licence
    r = client.get("/api/search?q=smoke", headers=_h()).json()
    assert any(p["id"] == "smokestack" for p in r["products"])
    r2 = client.get("/api/search?q=company", headers=_h()).json()
    assert any("company" in (c["name"] or "").lower() for c in r2["customers"])
    r3 = client.get("/api/search?q=legacy", headers=_h()).json()
    assert any("legacy" in (v["version"] or "").lower() for v in r3["versions"])
    # audit is searchable too (seed + registrations produce 'create'/'register' actions)
    r4 = client.get("/api/search?q=register", headers=_h()).json()
    assert "audit" in r4 and any("register" in (a["action"] or "").lower() for a in r4["audit"])


def test_global_search_empty_query_returns_empty_buckets():
    r = client.get("/api/search?q=", headers=_h()).json()
    assert r["products"] == [] and r["customers"] == [] and r["versions"] == [] and r["audit"] == []


def test_global_search_requires_auth():
    assert client.get("/api/search?q=x").status_code == 401


def test_effective_session_status_timezone_safe():
    """Regression: PostgreSQL returns tz-AWARE datetimes for DateTime(timezone=True);
    the status calc must not raise 'can't compare offset-naive and offset-aware datetimes'."""
    import datetime as _dt
    from types import SimpleNamespace
    import main as M
    aware_future = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(minutes=30)
    aware_past = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=30)
    naive_future = _dt.datetime.utcnow() + _dt.timedelta(minutes=30)
    assert M._effective_session_status(SimpleNamespace(status="pending_erp_integration", expires_at=aware_future)) == "pending_erp_integration"
    assert M._effective_session_status(SimpleNamespace(status="pending_erp_integration", expires_at=aware_past)) == "expired"
    assert M._effective_session_status(SimpleNamespace(status="active", expires_at=naive_future)) == "active"
    assert M._effective_session_status(SimpleNamespace(status="revoked", expires_at=aware_past)) == "revoked"

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


def test_home_cards_expose_richer_metadata():
    sm = [p for p in client.get("/api/home", headers=_h()).json()["products"] if p["id"] == "smokestack"][0]
    assert {"user_count", "branch_count", "last_deployment", "current_version",
            "erp_health", "customers", "active_licenses", "last_activity"} <= set(sm)


def test_health_check_all_returns_summary():
    r = client.post("/api/runtimes/health-check-all", headers=_h())
    assert r.status_code == 200
    d = r.json()
    assert d["checked"] >= 1 and isinstance(d["by_health"], dict)
    # audited as a fleet-level action
    assert any(a["action"] == "health_check_all" for a in client.get("/api/audit", headers=_h()).json())


def test_health_check_all_requires_auth():
    assert client.post("/api/runtimes/health-check-all").status_code == 401


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


# ==========================================================================================
#            FEATURE MANAGEMENT & INTERNAL TOOLS (deny-by-default) — Milestone 1
# ==========================================================================================
def _smokestack_customer_id():
    custs = client.get("/api/customers", headers=_h()).json()
    c = [c for c in custs if c["erp_product_id"] == "smokestack"][0]
    return c["id"]


def _open_session(minutes=30):
    cid = _smokestack_customer_id()
    r = client.post("/api/support-sessions", headers=_h(),
                    json={"erp_product_id": "smokestack", "customer_ref_id": cid,
                          "reason": "feature test", "capabilities": "support:read", "minutes": minutes})
    assert r.status_code == 201
    return r.json()["session"]["session_ref"], r.json()["id"]


def _mkflag(**kw):
    body = {"key": kw.pop("key"), "name": kw.pop("name", "F"), "erp_product_id": "smokestack"}
    body.update(kw)
    r = client.post("/api/platform/feature-flags", headers=_h(), json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"], r.json()["feature"]


def _check(**kw):
    kw.setdefault("erp_product_id", "smokestack")   # the calling ERP always identifies itself
    return client.post("/api/internal/feature-check", headers=_h(), json=kw).json()


# --------------------------- namespace auth (deny-by-default) ---------------------------
def test_internal_namespace_requires_auth():
    # No operator token → 401 on BOTH management and evaluation routes (not just UI hiding).
    assert client.get("/api/platform/products/smokestack/feature-flags").status_code == 401
    assert client.post("/api/platform/feature-flags",
                       json={"key": "x", "name": "x"}).status_code == 401
    assert client.post("/api/internal/feature-check",
                       json={"feature_key": "x"}).status_code == 401


def test_require_internal_denies_non_internal_tier_with_403():
    # A validated operator that is NOT owner/operator/internal is forbidden (deny-by-default → 403).
    import main as M
    from fastapi import HTTPException
    from types import SimpleNamespace
    try:
        M.require_internal(SimpleNamespace(id="OP-x", platform_role="customer"))
        assert False, "expected HTTPException"
    except HTTPException as e:
        assert e.status_code == 403
    # owner passes
    assert M.require_internal(SimpleNamespace(id="OP-owner", platform_role="owner")).id == "OP-owner"


# ------------------------------- CRUD + audit -------------------------------
def test_create_list_patch_flag_writes_audit_with_before_after():
    fid, feat = _mkflag(key="inventory.batch_repair", name="Batch Repair",
                        visibility="customer", default_state=False, module="inventory")
    assert feat["visibility"] == "customer" and feat["default_state"] is False
    listed = client.get("/api/platform/products/smokestack/feature-flags", headers=_h()).json()
    assert any(f["id"] == fid for f in listed)
    # enable it
    r = client.patch(f"/api/platform/feature-flags/{fid}", headers=_h(),
                     json={"default_state": True, "reason": "GA release"})
    assert r.status_code == 200 and r.json()["feature"]["default_state"] is True
    aud = client.get(f"/api/platform/feature-flags/{fid}/audit", headers=_h()).json()
    actions = [a["action"] for a in aud]
    assert "feature_created" in actions and "feature_enabled" in actions
    enab = [a for a in aud if a["action"] == "feature_enabled"][0]
    assert enab["before_state"]["default_state"] is False
    assert enab["after_state"]["default_state"] is True
    assert enab["reason"] == "GA release"


def test_duplicate_key_same_scope_rejected():
    _mkflag(key="sales.dup_guard", name="Dup")
    r = client.post("/api/platform/feature-flags", headers=_h(),
                    json={"key": "sales.dup_guard", "name": "Dup2", "erp_product_id": "smokestack"})
    assert r.status_code == 409


def test_invalid_visibility_and_rollout_rejected():
    assert client.post("/api/platform/feature-flags", headers=_h(),
                       json={"key": "z1", "name": "z", "visibility": "bogus"}).status_code == 422
    assert client.post("/api/platform/feature-flags", headers=_h(),
                       json={"key": "z2", "name": "z", "rollout_percentage": 150}).status_code == 422


# --------- Deliverable 7: customers CANNOT access owner-only / internal features ---------
def test_customer_denied_owner_only_feature():
    _mkflag(key="platform.sql_console", name="SQL Console", visibility="platform_owner_only")
    d = _check(feature_key="platform.sql_console", actor_type="customer",
               customer_ref="1", environment="production")
    assert d["allow"] is False and d["reason"].startswith("owner_only")


def test_customer_denied_internal_feature():
    _mkflag(key="platform.debug_traces", name="Debug Traces", visibility="internal_team")
    d = _check(feature_key="platform.debug_traces", actor_type="customer", customer_ref="1")
    assert d["allow"] is False and d["reason"].startswith("internal_only")


def test_owner_with_valid_session_allowed_owner_only():
    _mkflag(key="platform.license_override", name="License Override", visibility="platform_owner_only")
    ref, _sid = _open_session()
    d = _check(feature_key="platform.license_override", actor_type="platform_owner",
               support_session_ref=ref, environment="production")
    assert d["allow"] is True and d["reason"] == "owner_session"


def test_owner_only_denied_without_session():
    _mkflag(key="platform.wipe_cache", name="Wipe Cache", visibility="platform_owner_only")
    d = _check(feature_key="platform.wipe_cache", actor_type="platform_owner")
    assert d["allow"] is False and "no_session" in d["reason"]


# --------- Deliverable 8: disabled features inaccessible even when route/key is known ---------
def test_disabled_feature_denied_for_everyone_even_owner():
    _mkflag(key="sales.legacy_export", name="Legacy Export", visibility="disabled",
            default_state=True, rollout_percentage=100)
    ref, _sid = _open_session()
    assert _check(feature_key="sales.legacy_export", actor_type="customer",
                  customer_ref="1")["allow"] is False
    d_owner = _check(feature_key="sales.legacy_export", actor_type="platform_owner",
                     support_session_ref=ref)
    assert d_owner["allow"] is False and d_owner["reason"] == "feature_disabled"


def test_unknown_feature_denied():
    d = _check(feature_key="does.not.exist", actor_type="platform_owner")
    assert d["allow"] is False and d["reason"] == "unknown_feature"


# --------- Deliverable 9: expired / revoked support sessions deny elevated access ---------
def test_expired_session_denies_owner_only():
    _mkflag(key="platform.expired_probe", name="Expired Probe", visibility="platform_owner_only")
    ref, sid = _open_session(minutes=1)
    # force-expire by rewriting expires_at in the DB to the past
    import datetime as _dt
    import main as M
    from database import SessionLocal
    db = SessionLocal()
    s = db.get(M.models.SupportSession, sid)
    s.expires_at = _dt.datetime.utcnow() - _dt.timedelta(minutes=5)
    db.commit()
    db.close()
    d = _check(feature_key="platform.expired_probe", actor_type="platform_owner",
               support_session_ref=ref)
    assert d["allow"] is False and "session_expired" in d["reason"]


def test_revoked_session_denies_owner_only():
    _mkflag(key="platform.revoked_probe", name="Revoked Probe", visibility="platform_owner_only")
    ref, sid = _open_session()
    assert client.post(f"/api/support-sessions/{sid}/revoke", headers=_h()).status_code == 200
    d = _check(feature_key="platform.revoked_probe", actor_type="platform_owner",
               support_session_ref=ref)
    assert d["allow"] is False and "session_revoked" in d["reason"]


def test_session_for_wrong_erp_denied():
    # register a second product + customer + its own session, then use it against a smokestack flag
    client.post("/api/products", headers=_h(), json={"id": "dairy2", "name": "Dairy2"})
    r = client.post("/api/customers", headers=_h(),
                    json={"erp_product_id": "dairy2", "name": "D2 Cust", "external_ref": "d1"})
    cid = r.json()["id"]
    sref = client.post("/api/support-sessions", headers=_h(),
                       json={"erp_product_id": "dairy2", "customer_ref_id": cid}).json()["session"]["session_ref"]
    _mkflag(key="platform.cross_erp", name="Cross", visibility="platform_owner_only")
    d = _check(feature_key="platform.cross_erp", actor_type="platform_owner", support_session_ref=sref)
    assert d["allow"] is False and "session_wrong_erp" in d["reason"]


# --------- Three-gate customer access: flag AND license AND role ---------
def test_three_gate_role_and_license_and_flag():
    _mkflag(key="sales.premium_report", name="Premium Report", visibility="customer",
            default_state=True, role_requirements="admin,manager",
            license_plan_requirements="pro,enterprise")
    base = dict(feature_key="sales.premium_report", actor_type="customer", customer_ref="1")
    # role gate fails
    assert _check(**base, role="clerk", license_plan="pro")["reason"] == "role_not_permitted"
    # license gate fails
    assert _check(**base, role="admin", license_plan="standard")["reason"] == "license_not_entitled"
    # all three pass
    ok = _check(**base, role="admin", license_plan="pro")
    assert ok["allow"] is True and ok["reason"] == "granted"


def test_denylist_overrides_default_on():
    _mkflag(key="sales.broad_feature", name="Broad", visibility="customer",
            default_state=True, customer_denylist="99")
    assert _check(feature_key="sales.broad_feature", actor_type="customer",
                  customer_ref="99")["allow"] is False
    assert _check(feature_key="sales.broad_feature", actor_type="customer",
                  customer_ref="1")["allow"] is True


def test_customer_not_targeted_denied_by_default():
    _mkflag(key="sales.dark_feature", name="Dark", visibility="customer", default_state=False)
    d = _check(feature_key="sales.dark_feature", actor_type="customer", customer_ref="1")
    assert d["allow"] is False and d["reason"] == "not_targeted"
    # allowlisted customer gets in
    fid = client.get("/api/platform/products/smokestack/feature-flags", headers=_h()).json()
    fid = [f for f in fid if f["key"] == "sales.dark_feature"][0]["id"]
    client.patch(f"/api/platform/feature-flags/{fid}", headers=_h(),
                 json={"customer_allowlist": "1", "reason": "pilot"})
    assert _check(feature_key="sales.dark_feature", actor_type="customer",
                  customer_ref="1")["allow"] is True


def test_full_rollout_grants_all_customers():
    _mkflag(key="sales.rollout_full", name="Rollout", visibility="customer",
            default_state=False, rollout_percentage=100)
    assert _check(feature_key="sales.rollout_full", actor_type="customer",
                  customer_ref="anyone")["allow"] is True


# --------- environment scoping + date windows ---------
def test_environment_scope_enforced():
    _mkflag(key="sales.staging_only", name="Staging Only", visibility="customer",
            default_state=True, environment_scope="staging")
    # A customer is ALWAYS forced to production, so a staging-scoped flag is out of scope for them
    # even if the client asks for environment=staging.
    assert _check(feature_key="sales.staging_only", actor_type="customer",
                  customer_ref="1", environment="production")["reason"] == "environment_not_in_scope"
    assert _check(feature_key="sales.staging_only", actor_type="customer",
                  customer_ref="1", environment="staging")["reason"] == "environment_not_in_scope"
    # An elevated actor (owner/internal) may evaluate against staging.
    assert _check(feature_key="sales.staging_only", actor_type="internal",
                  customer_ref="1", environment="staging")["allow"] is True


def test_expiry_date_denies_after_window():
    past = "2000-01-01T00:00:00"
    _mkflag(key="sales.expired_flag", name="Expired", visibility="customer",
            default_state=True, expiry_date=past)
    assert _check(feature_key="sales.expired_flag", actor_type="customer",
                  customer_ref="1")["reason"] == "expired"


def test_archived_flag_denied():
    fid, _ = _mkflag(key="sales.to_archive", name="Archive", visibility="customer", default_state=True)
    client.patch(f"/api/platform/feature-flags/{fid}", headers=_h(),
                 json={"status": "archived", "reason": "retire"})
    assert _check(feature_key="sales.to_archive", actor_type="customer",
                  customer_ref="1")["reason"] == "flag_archived"


# --------- audit trail of sensitive access decisions ---------
def test_unauthorized_customer_access_is_audited():
    fid, _ = _mkflag(key="platform.audited_denial", name="Audited", visibility="platform_owner_only")
    _check(feature_key="platform.audited_denial", actor_type="customer", customer_ref="1")
    aud = client.get(f"/api/platform/feature-flags/{fid}/audit", headers=_h()).json()
    assert any(a["action"] == "unauthorized_access_denied" for a in aud)


def test_owner_tool_open_is_audited():
    fid, _ = _mkflag(key="platform.audited_open", name="AuditedOpen", visibility="platform_owner_only")
    ref, _sid = _open_session()
    _check(feature_key="platform.audited_open", actor_type="platform_owner", support_session_ref=ref)
    aud = client.get(f"/api/platform/feature-flags/{fid}/audit", headers=_h()).json()
    assert any(a["action"] == "owner_tool_opened" for a in aud)


def test_global_flag_applies_when_no_product_specific_exists():
    # erp_product_id None → applies to all products; product-specific wins when both exist
    r = client.post("/api/platform/feature-flags", headers=_h(),
                    json={"key": "global.telemetry", "name": "Telemetry",
                          "erp_product_id": None, "visibility": "customer", "default_state": True})
    assert r.status_code == 201
    d = _check(feature_key="global.telemetry", erp_product_id="smokestack",
               actor_type="customer", customer_ref="1")
    assert d["allow"] is True


# ==========================================================================================
#            INTERNAL DEVELOPMENT PLATFORM — Developer Mode (Platform Owner only)
# ==========================================================================================
def test_require_owner_denies_non_owner_and_allows_owner():
    import main as M
    from fastapi import HTTPException
    from types import SimpleNamespace
    for role in ("internal", "operator", "customer", None):
        try:
            M.require_owner(SimpleNamespace(id="OP-x", platform_role=role))
            assert False, f"expected 403 for role={role}"
        except HTTPException as e:
            assert e.status_code == 403
    assert M.require_owner(SimpleNamespace(id="OP-owner", platform_role="owner")).id == "OP-owner"


def test_dev_endpoints_require_auth_no_token():
    # A customer has no operator token → 401 on every developer endpoint (never UI-only hiding).
    for path in ("/api/platform/dev/context", "/api/platform/dev/diagnostics",
                 "/api/platform/dev/schema", "/api/platform/dev/routes",
                 "/api/platform/dev/debug", "/api/platform/dev/jobs",
                 "/api/platform/dev/integrations"):
        assert client.get(path).status_code == 401, path
    assert client.post("/api/platform/preview/start",
                       json={"erp_product_id": "smokestack"}).status_code == 401


def test_dev_context_reports_tools_and_profile():
    d = client.get("/api/platform/dev/context?pid=smokestack", headers=_h()).json()
    assert d["developer_mode"] is True and d["feature_profile"] == "platform_owner"
    keys = {t["key"] for t in d["tools"]}
    assert {"feature_manager", "release_manager", "rollout_manager", "db_inspector",
            "system_diagnostics", "api_explorer"}.issubset(keys)
    assert set(d["environments"]) == {"development", "staging", "production"}
    assert d["lifecycle_stages"][0] == "development" and d["lifecycle_stages"][-1] == "removed"


def test_customer_always_forced_to_production():
    # Flag scoped to development, default on. A customer asking for development is still forced
    # to production by the backend and therefore denied — customers never leave Production.
    _mkflag(key="sales.dev_only", name="Dev Only", visibility="customer",
            default_state=True, environment_scope="development")
    assert _check(feature_key="sales.dev_only", actor_type="customer", customer_ref="1",
                  environment="development")["reason"] == "environment_not_in_scope"
    # The owner previewing development sees it.
    assert _check(feature_key="sales.dev_only", actor_type="platform_owner", customer_ref="1",
                  environment="development")["allow"] is True


def test_preview_start_switch_end_are_audited():
    r = client.post("/api/platform/preview/start", headers=_h(),
                    json={"erp_product_id": "smokestack", "customer_ref": "1", "environment": "development"})
    assert r.status_code == 201
    pv = r.json()["preview"]
    assert pv["status"] == "active" and pv["feature_profile"] == "platform_owner"
    assert pv["environment"] == "development"
    # context now reports the active preview
    ctx = client.get("/api/platform/dev/context?pid=smokestack", headers=_h()).json()
    assert ctx["active_preview"] and ctx["active_preview"]["id"] == pv["id"]
    # switch environment instantly
    r2 = client.post(f"/api/platform/preview/{pv['id']}/environment?environment=staging", headers=_h())
    assert r2.status_code == 200 and r2.json()["preview"]["environment"] == "staging"
    # end it
    r3 = client.post(f"/api/platform/preview/{pv['id']}/end", headers=_h())
    assert r3.status_code == 200 and r3.json()["preview"]["status"] == "ended"
    actions = [a["action"] for a in client.get("/api/audit", headers=_h()).json()]
    assert "preview_started" in actions and "preview_ended" in actions
    assert "preview_environment_switched" in actions


def test_preview_start_rejects_bad_env_and_unknown_erp():
    assert client.post("/api/platform/preview/start", headers=_h(),
                       json={"erp_product_id": "smokestack", "environment": "prod"}).status_code == 422
    assert client.post("/api/platform/preview/start", headers=_h(),
                       json={"erp_product_id": "nope", "environment": "development"}).status_code == 404


def test_lifecycle_stage_promote_and_demote_audited():
    fid, feat = _mkflag(key="inventory.pipeline", name="Pipeline", visibility="customer")
    assert feat["lifecycle_stage"] == "development"
    r = client.post(f"/api/platform/feature-flags/{fid}/stage", headers=_h(),
                    json={"lifecycle_stage": "staging", "reason": "ready for staging"})
    assert r.status_code == 200 and r.json()["feature"]["lifecycle_stage"] == "staging"
    client.post(f"/api/platform/feature-flags/{fid}/stage", headers=_h(),
                json={"lifecycle_stage": "development", "reason": "regression found"})
    acts = [a["action"] for a in client.get(f"/api/platform/feature-flags/{fid}/audit", headers=_h()).json()]
    assert "feature_promoted" in acts and "feature_demoted" in acts


def test_invalid_lifecycle_stage_rejected():
    assert client.post("/api/platform/feature-flags", headers=_h(),
                       json={"key": "x.bad_stage", "name": "x", "erp_product_id": "smokestack",
                             "lifecycle_stage": "bogus"}).status_code == 422


def test_rollback_restores_previous_state():
    fid, _ = _mkflag(key="sales.rollback_me", name="RB", visibility="customer", default_state=False)
    # change 1: enable
    client.patch(f"/api/platform/feature-flags/{fid}", headers=_h(),
                 json={"default_state": True, "reason": "ship it"})
    assert client.get(f"/api/platform/feature-flags/{fid}", headers=_h()).json()["default_state"] is True
    # rollback → back to off
    r = client.post(f"/api/platform/feature-flags/{fid}/rollback", headers=_h())
    assert r.status_code == 200 and r.json()["feature"]["default_state"] is False
    acts = [a["action"] for a in client.get(f"/api/platform/feature-flags/{fid}/audit", headers=_h()).json()]
    assert "feature_rolledback" in acts


def test_db_inspector_is_metadata_only():
    d = client.get("/api/platform/dev/schema", headers=_h()).json()
    tables = {t["table"] for t in d["tables"]}
    assert "feature_flags" in tables and "operators" in tables
    # metadata only: each entry exposes counts + column NAMES, never row values
    for t in d["tables"]:
        assert "rows" in t and "columns" in t and isinstance(t["columns"], list)
    assert "metadata only" in d["note"]


def test_api_explorer_lists_namespaced_routes():
    d = client.get("/api/platform/dev/routes", headers=_h()).json()
    paths = {r["path"] for r in d["routes"]}
    assert "/api/internal/feature-check" in paths
    assert any(r["namespace"] == "developer" for r in d["routes"])
    assert any(r["namespace"] == "internal" for r in d["routes"])


def test_diagnostics_and_debug_and_jobs_owner_only():
    diag = client.get("/api/platform/dev/diagnostics?pid=smokestack", headers=_h()).json()
    assert diag["database"] == "ok" and "feature_flags" in diag["counts"]
    dbg = client.get("/api/platform/dev/debug?pid=smokestack", headers=_h()).json()
    assert "platform" in dbg and "feature_decisions" in dbg
    jobs = client.get("/api/platform/dev/jobs", headers=_h()).json()
    assert any(j["name"] == "Fleet health poll" for j in jobs["jobs"])
    integ = client.get("/api/platform/dev/integrations?pid=smokestack", headers=_h()).json()
    assert any("Feature-check" in i["name"] for i in integ["integrations"])

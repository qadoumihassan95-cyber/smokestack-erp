"""Remediation M5 — status / subscription / module / feature enforcement.

INTEGRATION tests against real protected write endpoints for every required
state: ACTIVE, TRIAL, READ_ONLY, SUSPENDED, EXPIRED subscription, DISABLED
MODULE, DISABLED FEATURE, and PFS emergency OVERRIDE. Company #1 (active +
lifetime + all modules) must remain fully functional.
"""
import os
import tempfile

_DB = os.path.join(tempfile.gettempdir(), f"smokestack_policy_{os.getpid()}.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["SEED_ON_START"] = "true"
os.environ["JWT_SECRET"] = "policy-secret-long-enough"
os.environ["TELEGRAM_BOT_TOKEN"] = "test-bot-token"

from fastapi.testclient import TestClient

from app.main import app
from app.database import SessionLocal
from app import models, security, policy

with TestClient(app):
    pass
client = TestClient(app)

# a second company we can put into any state without touching Company #1
CO = 2


def setup_module(_m):
    db = SessionLocal()
    try:
        if not db.query(models.Company).filter(models.Company.id == CO).first():
            db.add(models.Company(id=CO, name="Policy Co", slug="policy-co",
                                  application_key="smoke_shop", industry="Retail",
                                  owner_user_id="P-owner", status="active"))
        if not db.get(models.User, "P-owner"):
            u = models.User(id="P-owner", name="Policy Owner", role="owner",
                            password_hash=security.hash_pw("demo1234"), status="active")
            u.company_id = CO
            db.add(u)
        # a branch + product for company 2 so writes have something to touch
        db.commit()
    finally:
        db.close()
    # active subscription for CO
    db2 = SessionLocal()
    try:
        if not db2.query(models.Subscription).filter(models.Subscription.company_id == CO).first():
            db2.add(models.Subscription(company_id=CO, plan="monthly", status="active"))
            db2.commit()
    finally:
        db2.close()
    policy.invalidate(CO)


def _tok(user="P-owner"):
    r = client.post("/api/auth/login", data={"username": user, "password": "demo1234"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _h(t):
    return {"Authorization": "Bearer " + t}


def _set_company(status):
    db = SessionLocal()
    try:
        c = db.query(models.Company).filter(models.Company.id == CO).first()
        policy.change_company_status(db, c, status, actor="SA-test", reason="test")
    finally:
        db.close()


def _write():
    """A real protected WRITE endpoint (create product)."""
    return client.post("/api/inventory/products",
                       json={"sku": f"P2-{os.urandom(3).hex()}", "name": "Item",
                             "cost": 1, "price": 2},
                       headers=_h(_tok()))


def _read():
    return client.get("/api/inventory/products", headers=_h(_tok()))


# ---------------------------------------------------- Company #1 unaffected
def test_company_one_unaffected_reads_and_writes():
    t = client.post("/api/auth/login", data={"username": "U-owner", "password": "demo1234"}).json()["access_token"]
    assert client.get("/api/inventory/products", headers=_h(t)).status_code == 200
    w = client.post("/api/inventory/products",
                    json={"sku": "C1-POL", "name": "c1", "cost": 1, "price": 2}, headers=_h(t))
    assert w.status_code in (201, 409)   # created (or already exists on rerun)


# ---------------------------------------------------- ACTIVE / TRIAL
def test_active_allows_read_and_write():
    _set_company("active")
    assert _read().status_code == 200
    assert _write().status_code in (201, 409)


def test_trial_allows_read_and_write():
    _set_company("trial")
    assert _read().status_code == 200
    assert _write().status_code in (201, 409)


# ---------------------------------------------------- READ_ONLY
def test_read_only_blocks_writes_allows_reads():
    _set_company("read_only")
    assert _read().status_code == 200            # reads still work
    assert _write().status_code == 403           # writes blocked
    _set_company("active")


# ---------------------------------------------------- SUSPENDED
def test_suspended_blocks_everything_and_login():
    _set_company("suspended")
    # existing tokens become ineffective (checked every request)
    assert client.get("/api/inventory/products", headers=_h(_login_before_suspend())).status_code == 403
    # and new logins are refused
    assert client.post("/api/auth/login", data={"username": "P-owner", "password": "demo1234"}).status_code == 403
    _set_company("active")


def _login_before_suspend():
    # token minted while active, used after suspension
    _set_company("active")
    t = _tok()
    _set_company("suspended")
    return t


# ---------------------------------------------------- EXPIRED subscription
def test_expired_subscription_is_read_only():
    _set_company("active")
    db = SessionLocal()
    try:
        policy.change_subscription_status(db, CO, "expired", actor="SA-test")
    finally:
        db.close()
    assert _read().status_code == 200            # read + export still allowed
    assert _write().status_code == 403           # writes blocked by subscription
    db = SessionLocal()
    try:
        policy.change_subscription_status(db, CO, "active", actor="SA-test")
    finally:
        db.close()


# ---------------------------------------------------- DISABLED MODULE
def test_disabled_module_blocks_endpoint_server_side():
    _set_company("active")
    db = SessionLocal()
    try:
        policy.set_module_state(db, CO, "inventory", "disabled", actor="SA-test")
    finally:
        db.close()
    # inventory endpoints blocked even though company is active
    assert _read().status_code == 403
    assert _write().status_code == 403
    # a NON-inventory endpoint still works (module scoping is precise)
    assert client.get("/api/branches", headers=_h(_tok())).status_code == 200
    db = SessionLocal()
    try:
        policy.set_module_state(db, CO, "inventory", "enabled", actor="SA-test")
    finally:
        db.close()
    assert _read().status_code == 200


# ---------------------------------------------------- DISABLED FEATURE
def test_disabled_feature_blocks_endpoint():
    _set_company("active")
    db = SessionLocal()
    try:
        db.add(models.FeatureFlag(key="assistant", scope="company",
                                  scope_ref=str(CO), enabled=False))
        db.commit()
    finally:
        db.close()
    policy.invalidate(CO)
    r = client.post("/api/assistant/ask", json={"q": "hello"}, headers=_h(_tok()))
    assert r.status_code == 403
    # remove the flag → feature works again
    db = SessionLocal()
    try:
        db.query(models.FeatureFlag).filter(models.FeatureFlag.key == "assistant").delete()
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------- PFS OVERRIDE
def test_emergency_override_allows_a_blocked_write():
    _set_company("read_only")                    # writes normally blocked
    assert _write().status_code == 403
    db = SessionLocal()
    try:
        policy.set_override(db, CO, action="write", allow=True,
                            reason="emergency fix", super_admin_id="SA-test", ttl_minutes=60)
    finally:
        db.close()
    assert _write().status_code in (201, 409)    # override permits the write
    _set_company("active")


def test_override_is_audited_and_time_boxed():
    db = SessionLocal()
    try:
        o = policy.set_override(db, CO, action="all", allow=True, reason="drill",
                                super_admin_id="SA-test", ttl_minutes=30)
        assert o.expires_at is not None          # auto-expiring
        # audited in platform_audit
        n = db.query(models.PlatformAudit).filter(
            models.PlatformAudit.action == "policy_override",
            models.PlatformAudit.company_id == CO).count()
        assert n >= 1
        db.query(models.PolicyOverride).filter(models.PolicyOverride.company_id == CO).delete()
        db.commit()
    finally:
        db.close()
    policy.invalidate(CO)


# ---------------------------------------------------- audit on state change
def test_status_change_writes_immutable_audit():
    _set_company("read_only")
    _set_company("active")
    db = SessionLocal()
    try:
        n = db.query(models.PlatformAudit).filter(
            models.PlatformAudit.action == "company_status_change",
            models.PlatformAudit.company_id == CO).count()
        assert n >= 2
    finally:
        db.close()

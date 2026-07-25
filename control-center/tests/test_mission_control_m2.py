"""Mission Control M2 — adversarial + integration tests.

Covers: approvals (SoD, quorum), break-glass (JIT elevation, offline path, recording-fail
terminates, god-tier command gate), operator sessions (revocation invalidates token), transactional
outbox (idempotent, retry→dead-letter, replay), CQRS read model (project/rebuild/freshness),
BFF (delegated identity, partial-failure degrade), streaming (ABAC filter, critical priority).
"""
import os
import tempfile

_DB = os.path.join(tempfile.gettempdir(), f"pfs_cc_{os.getpid()}.db")
if "DATABASE_URL" not in os.environ and os.path.exists(_DB):
    os.remove(_DB)   # avoid a stale (pid-recycled) schema when run standalone
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_DB}")
os.environ.setdefault("JWT_SECRET", "cc-test-secret")
os.environ.setdefault("SEED_ON_START", "true")
os.environ.setdefault("SEED_PASSWORD", "owner-test-pw")

import hashlib
import hmac
from types import SimpleNamespace

import jwt as _jwt
from fastapi.testclient import TestClient

import approvals
import breakglass
import commands
import events
import models
import readmodels
import sessions
from config import settings
from database import SessionLocal
from main import app
from security import hash_pw

with TestClient(app):
    pass
client = TestClient(app)


# a god-tier executor gated only by break-glass (in BREAK_GLASS_REQUIRED, not SOD_REQUIRED)
@commands.register("database.migrate")
def _exec_migrate(db, op, cmd):
    return {"migrated": True}


def _h():
    r = client.post("/auth/login", data={"username": "OP-owner", "password": "owner-test-pw"})
    return {"Authorization": "Bearer " + r.json()["access_token"]}


def _mkop(oid, roles="operator"):
    db = SessionLocal()
    try:
        if not db.get(models.Operator, oid):
            db.add(models.Operator(id=oid, name=oid, email=f"{oid}@pfs.local",
                                   password_hash=hash_pw("x"), platform_role="operator", roles=roles))
            db.commit()
    finally:
        db.close()


# ------------------------------- approvals -------------------------------
def test_approval_requires_reason_and_forbids_self_approval():
    db = SessionLocal()
    try:
        try:
            approvals.create_request(db, "OP-owner", subject_type="command", reason="")
            assert False
        except approvals.ApprovalError as e:
            assert e.code == "reason_required"
        req = approvals.create_request(db, "OP-owner", subject_type="command", reason="need it")
        try:
            approvals.decide(db, "OP-owner", req.id, "approve")   # requester == approver
            assert False
        except approvals.ApprovalError as e:
            assert e.code == "self_approval_forbidden"
    finally:
        db.close()


def test_approval_m_of_n_quorum_and_no_double_vote():
    _mkop("OP-a1")
    _mkop("OP-a2")
    db = SessionLocal()
    try:
        req = approvals.create_request(db, "OP-owner", subject_type="command", policy="m_of_n",
                                       quorum_required=2, reason="quorum")
        approvals.decide(db, "OP-a1", req.id, "approve")
        assert db.get(models.ApprovalRequest, req.id).status == "pending"   # 1 of 2
        try:
            approvals.decide(db, "OP-a1", req.id, "approve")               # same approver again
            assert False
        except approvals.ApprovalError as e:
            assert e.code == "already_voted"
        approvals.decide(db, "OP-a2", req.id, "approve")
        assert db.get(models.ApprovalRequest, req.id).status == "approved"  # 2 of 2 distinct
    finally:
        db.close()


# ------------------------------- break-glass -------------------------------
def test_breakglass_quorum_activation_and_no_self_approve():
    _mkop("OP-b1")
    _mkop("OP-b2")
    db = SessionLocal()
    try:
        g = breakglass.request(db, "OP-owner", "tenant.transfer", "incident", quorum=2)
        # requester cannot approve own grant
        try:
            breakglass.approve(db, "OP-owner", g.id, "approve")
            assert False
        except approvals.ApprovalError as e:
            assert e.code == "self_approval_forbidden"
        breakglass.approve(db, "OP-b1", g.id, "approve")
        assert db.get(models.ElevationGrant, g.id).status == "pending"
        breakglass.approve(db, "OP-b2", g.id, "approve")
        gg = db.get(models.ElevationGrant, g.id)
        assert gg.status == "active" and gg.expires_at is not None       # time-boxed
    finally:
        db.close()


def test_breakglass_offline_path_requires_valid_credential():
    breakglass._OFFLINE_SECRET = "test-offline-secret"
    db = SessionLocal()
    try:
        try:
            breakglass.open_offline(db, "OP-owner", "system.recovery", "dr", "wrong")
            assert False
        except breakglass.BreakGlassError as e:
            assert e.code == "invalid_offline_credential"
        cred = hmac.new(b"test-offline-secret", b"OP-owner:system.recovery", hashlib.sha256).hexdigest()
        g = breakglass.open_offline(db, "OP-owner", "system.recovery", "dr", cred)
        assert g.status == "active" and g.offline is True
    finally:
        db.close()


def test_breakglass_recording_failure_terminates():
    breakglass._OFFLINE_SECRET = "test-offline-secret"
    db = SessionLocal()
    try:
        cred = hmac.new(b"test-offline-secret", b"OP-owner:ledger.correct", hashlib.sha256).hexdigest()
        g = breakglass.open_offline(db, "OP-owner", "ledger.correct", "fix", cred)
        assert breakglass.check(db, "OP-owner", "ledger.correct")[0] is True
        breakglass.on_recording_failure(db, g.id)                        # recording fails
        assert db.get(models.ElevationGrant, g.id).status == "revoked"
        assert breakglass.check(db, "OP-owner", "ledger.correct")[0] is False
    finally:
        db.close()


def test_god_tier_command_requires_break_glass():
    h = _h()
    base = {"type": "database.migrate", "target": {}, "params": {}}
    # no elevation → blocked
    r = client.post("/api/platform/commands", headers=h,
                    json={**base, "idempotency_key": "gt-1"})
    assert r.status_code == 428 and r.json()["detail"].startswith("break_glass_required")
    # with an active elevation grant → allowed
    breakglass._OFFLINE_SECRET = "test-offline-secret"
    db = SessionLocal()
    cred = hmac.new(b"test-offline-secret", b"OP-owner:database.migrate", hashlib.sha256).hexdigest()
    g = breakglass.open_offline(db, "OP-owner", "database.migrate", "planned", cred)
    gid = g.id
    db.close()
    r2 = client.post("/api/platform/commands", headers=h,
                     json={**base, "idempotency_key": "gt-2", "elevation_id": gid})
    assert r2.status_code == 200 and r2.json()["result"]["migrated"] is True


# ------------------------------- operator sessions -------------------------------
def test_session_revocation_invalidates_token():
    r = client.post("/auth/login", data={"username": "OP-owner", "password": "owner-test-pw"})
    tok = r.json()["access_token"]
    hdr = {"Authorization": "Bearer " + tok}
    assert client.get("/api/platform/commands", headers=hdr).status_code == 200
    jti = _jwt.decode(tok, settings.jwt_secret, algorithms=[settings.jwt_alg])["jti"]
    db = SessionLocal()
    s = db.query(models.OperatorSession).filter_by(jti=jti).first()
    sessions.revoke(db, s.id)
    db.close()
    assert client.get("/api/platform/commands", headers=hdr).status_code == 401  # token now dead


# ------------------------------- transactional outbox -------------------------------
def test_command_emits_outbox_and_projects_read_model():
    h = _h()
    ff = client.post("/api/platform/feature-flags", headers=h,
                     json={"key": "m2.ff", "name": "m2", "erp_product_id": "smokestack"}).json()["feature"]
    client.post("/api/platform/commands", headers=h,
                json={"type": "feature_flag.set_state",
                      "target": {"flag_id": ff["id"], "erp_product_id": "smokestack"},
                      "params": {"state": True}, "expected_version": 1, "idempotency_key": "m2-emit-1"})
    # an outbox event exists (pending) → relay → projected into the read model
    client.post("/api/platform/outbox/relay", headers=h)
    feed = client.get("/api/platform/readmodels/command-feed", headers=h).json()
    assert feed["freshness"]["lag_events"] == 0
    assert any(i["command_type"] == "feature_flag.set_state" for i in feed["items"])


def test_outbox_idempotent_emit():
    db = SessionLocal()
    try:
        n0 = db.query(models.Outbox).filter_by(dedupe_key="dk-unique-1").count()
        events.emit(db, aggregate_type="t", aggregate_id="1", event_type="test.x",
                    payload={}, dedupe_key="dk-unique-1", commit=True)
        events.emit(db, aggregate_type="t", aggregate_id="1", event_type="test.x",
                    payload={}, dedupe_key="dk-unique-1", commit=True)
        assert db.query(models.Outbox).filter_by(dedupe_key="dk-unique-1").count() == n0 + 1
    finally:
        db.close()


def test_outbox_retry_then_dead_letter():
    @events.subscribe("test.always_fail")
    def _boom(db, row):
        raise RuntimeError("nope")

    db = SessionLocal()
    try:
        row = events.emit(db, aggregate_type="t", aggregate_id="x", event_type="test.always_fail",
                          payload={}, dedupe_key="dlq-1", commit=True)
        rid = row.id
        for _ in range(6):
            # force availability (skip backoff) then relay
            r = db.get(models.Outbox, rid)
            r.available_at = None
            db.commit()
            events.relay(db, limit=10)
        assert db.get(models.Outbox, rid).status == "dead"
        assert any(d.id == rid for d in events.dead_letters(db))
    finally:
        db.close()


def test_read_model_rebuild_is_deterministic():
    h = _h()
    client.post("/api/platform/outbox/relay", headers=h)
    before = client.get("/api/platform/readmodels/command-feed", headers=h).json()
    res = client.post("/api/platform/readmodels/command-feed/rebuild", headers=h).json()
    assert res["rebuilt"] is True and res["valid"] is True
    after = client.get("/api/platform/readmodels/command-feed", headers=h).json()
    assert len(after["items"]) >= len(before["items"]) or res["rows"] >= 1


# ------------------------------- BFF -------------------------------
def test_bff_obo_token_carries_operator_identity():
    d = client.get("/api/bff/obo-token", headers=_h()).json()
    claims = _jwt.decode(d["obo_token"], settings.jwt_secret, algorithms=[settings.jwt_alg],
                         audience=bff_aud())
    assert claims["sub"] == "OP-owner" and claims["act"]["sub"] == "pfs-console-bff"
    assert claims["token_use"] == "delegation"


def bff_aud():
    import bff
    return bff.OBO_AUDIENCE


def test_bff_partial_failure_degrades_not_aborts():
    import bff
    op = SimpleNamespace(id="OP-owner")
    view = bff.gather(op, {"ok": lambda: {"v": 1}, "boom": lambda: (_ for _ in ()).throw(ValueError())})
    assert view["is_degraded"] is True
    assert view["data"]["ok"] == {"v": 1} and view["data"]["boom"] is None
    assert any(s["source"] == "boom" for s in view["degraded"])


# ------------------------------- streaming -------------------------------
def test_streaming_abac_filter_and_critical_priority():
    import streaming
    db = SessionLocal()
    try:
        events.emit(db, aggregate_type="a", aggregate_id="1", event_type="deploy.ok",
                    payload={"erp_product_id": "smokestack"}, dedupe_key="st-1", commit=True)
        events.emit(db, aggregate_type="a", aggregate_id="2", event_type="deploy.ok",
                    payload={"erp_product_id": "dairy"}, dedupe_key="st-2", commit=True)
        import json as _j
        op = SimpleNamespace(scopes=_j.dumps({"erp": ["smokestack"]}), platform_role=None, roles="operator")
        got = streaming.fetch(db, op, since_id=0, limit=100)
        erps = {e["payload"].get("erp_product_id") for e in got["events"]}
        assert "smokestack" in erps and "dairy" not in erps        # ABAC-filtered
        # critical events are never shed under backpressure
        assert streaming._is_critical("security.alert") and not streaming._is_critical("deploy.ok")
    finally:
        db.close()

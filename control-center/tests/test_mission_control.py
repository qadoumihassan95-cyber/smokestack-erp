"""Mission Control Foundation (Milestone 1) — adversarial + happy-path tests.

Covers the Foundation triad on the feature-flag mutation: IAM PDP (deny-by-default),
the typed command pipeline (idempotency + optimistic concurrency + SoD), and the
hash-chained tamper-evident audit.
"""
import os
import tempfile

_DB = os.path.join(tempfile.gettempdir(), f"pfs_cc_{os.getpid()}.db")
if "DATABASE_URL" not in os.environ and os.path.exists(_DB):
    os.remove(_DB)   # avoid a stale (pid-recycled) schema/data when run standalone
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_DB}")
os.environ.setdefault("JWT_SECRET", "cc-test-secret")
os.environ.setdefault("SEED_ON_START", "true")
os.environ.setdefault("SEED_PASSWORD", "owner-test-pw")

from fastapi.testclient import TestClient

from main import app

with TestClient(app):
    pass
client = TestClient(app)


def _h():
    r = client.post("/auth/login", data={"username": "OP-owner", "password": "owner-test-pw"})
    assert r.status_code == 200
    return {"Authorization": "Bearer " + r.json()["access_token"]}


def _mkflag(key):
    r = client.post("/api/platform/feature-flags", headers=_h(),
                    json={"key": key, "name": key, "erp_product_id": "smokestack",
                          "visibility": "customer", "default_state": False})
    assert r.status_code == 201, r.text
    return r.json()["feature"]


def _cmd(body, headers=None):
    return client.post("/api/platform/commands", headers=(headers if headers is not None else _h()),
                       json=body)


# ------------------------------- PDP (deny-by-default) -------------------------------
def test_pdp_owner_allowed_auditor_denied():
    import iam
    from types import SimpleNamespace
    owner = SimpleNamespace(id="OP-owner", status="active", platform_role="owner", roles="", scopes="")
    auditor = SimpleNamespace(id="OP-a", status="active", platform_role=None,
                              roles="read_only_auditor", scopes="")
    assert iam.decide(owner, "feature_flag.set_state", {}).allow is True
    d = iam.decide(auditor, "feature_flag.set_state", {})
    assert d.allow is False and d.reason == "capability_not_granted"


def test_pdp_out_of_scope_denied():
    import iam
    import json as _j
    from types import SimpleNamespace
    op = SimpleNamespace(id="OP-s", status="active", platform_role=None, roles="release_manager",
                         scopes=_j.dumps({"erp": ["dairy"]}))
    allow_same = iam.decide(op, "feature_flag.set_state", {"erp_product_id": "dairy"})
    deny_other = iam.decide(op, "feature_flag.set_state", {"erp_product_id": "smokestack"})
    assert allow_same.allow is True
    assert deny_other.allow is False and deny_other.reason == "out_of_scope:erp"


# ------------------------------- command pipeline -------------------------------
def test_command_requires_authentication():
    assert _cmd({"type": "feature_flag.set_state", "idempotency_key": "k1"}, headers={}).status_code == 401


def test_command_governed_feature_flag_toggle():
    f = _mkflag("mc.toggle_one")
    r = _cmd({"type": "feature_flag.set_state",
              "target": {"flag_id": f["id"], "erp_product_id": "smokestack"},
              "params": {"state": True}, "expected_version": 1,
              "justification": "enable for GA", "idempotency_key": "mc-cmd-1"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["status"] == "completed" and d["result"]["default_state"] is True
    assert d["result"]["version"] == 2 and d["blast_radius"] == "low"
    # the mutation actually happened + is audited as feature_enabled
    got = client.get(f"/api/platform/feature-flags/{f['id']}", headers=_h()).json()
    assert got["default_state"] is True
    acts = [a["action"] for a in
            client.get(f"/api/platform/feature-flags/{f['id']}/audit", headers=_h()).json()]
    assert "feature_enabled" in acts
    # command is recorded
    assert any(c["idempotency_key"] == "mc-cmd-1" and c["status"] == "completed"
               for c in client.get("/api/platform/commands", headers=_h()).json())


def test_command_idempotent_replay_does_not_double_apply():
    f = _mkflag("mc.idem")
    body = {"type": "feature_flag.set_state",
            "target": {"flag_id": f["id"], "erp_product_id": "smokestack"},
            "params": {"state": True}, "expected_version": 1, "idempotency_key": "mc-idem-1"}
    first = _cmd(body).json()
    assert first["result"]["version"] == 2 and first["idempotent_replay"] is False
    second = _cmd(body).json()
    assert second["idempotent_replay"] is True
    # version did NOT advance again (no double-apply)
    assert client.get(f"/api/platform/feature-flags/{f['id']}", headers=_h()).json()  # exists
    third_version = _cmd({**body, "idempotency_key": "mc-idem-2", "expected_version": 2,
                          "params": {"state": False}}).json()["result"]["version"]
    assert third_version == 3


def test_command_stale_expected_version_rejected():
    f = _mkflag("mc.stale")
    # first command bumps version 1→2
    assert _cmd({"type": "feature_flag.set_state",
                 "target": {"flag_id": f["id"], "erp_product_id": "smokestack"},
                 "params": {"state": True}, "expected_version": 1,
                 "idempotency_key": "mc-stale-1"}).status_code == 200
    # a command that still believes version==1 must be rejected (optimistic concurrency)
    r = _cmd({"type": "feature_flag.set_state",
              "target": {"flag_id": f["id"], "erp_product_id": "smokestack"},
              "params": {"state": False}, "expected_version": 1, "idempotency_key": "mc-stale-2"})
    assert r.status_code == 409 and "version_conflict" in r.json()["detail"]


def test_command_unknown_type_rejected():
    r = _cmd({"type": "does.not.exist", "idempotency_key": "mc-unknown-1", "target": {}})
    assert r.status_code == 400 and r.json()["detail"] == "unknown_command"


def test_command_missing_idempotency_key_rejected():
    assert _cmd({"type": "feature_flag.set_state", "target": {}}).status_code == 422


# ------------------------------- separation of duties -------------------------------
def test_sod_self_approval_forbidden():
    f = _mkflag("mc.sod")
    # forcing irreversible blast radius makes this command SoD-required
    base = {"type": "feature_flag.set_state",
            "target": {"flag_id": f["id"], "erp_product_id": "smokestack"},
            "params": {"state": True}, "expected_version": 1, "blast_radius": "irreversible"}
    # no approver → approval required
    assert _cmd({**base, "idempotency_key": "mc-sod-1"}).status_code == 428
    # self-approval → forbidden (operator cannot approve their own god-tier action)
    r = _cmd({**base, "idempotency_key": "mc-sod-2", "approved_by": "OP-owner"})
    assert r.status_code == 403 and r.json()["detail"] == "self_approval_forbidden"


# ------------------------------- tamper-evident audit -------------------------------
def test_audit_chain_verifies_then_detects_tampering():
    f = _mkflag("mc.audit")
    _cmd({"type": "feature_flag.set_state",
          "target": {"flag_id": f["id"], "erp_product_id": "smokestack"},
          "params": {"state": True}, "expected_version": 1, "idempotency_key": "mc-audit-1"})
    assert client.get("/api/platform/audit/verify", headers=_h()).json()["ok"] is True
    # tamper with a chained row directly in the DB → chain must fail verification
    import models
    from database import SessionLocal
    db = SessionLocal()
    row = (db.query(models.PlatformAuditLog)
           .filter(models.PlatformAuditLog.entry_hash.isnot(None))
           .order_by(models.PlatformAuditLog.id.desc()).first())
    row.detail = (row.detail or "") + "_tampered"
    db.commit()
    db.close()
    v = client.get("/api/platform/audit/verify", headers=_h()).json()
    assert v["ok"] is False and "tampered_row" in v["detail"]

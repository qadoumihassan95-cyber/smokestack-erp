"""Mission Control M3 — Bulk-Operations Safety Engine adversarial + integration tests."""
import os
import tempfile

_DB = os.path.join(tempfile.gettempdir(), f"pfs_cc_{os.getpid()}.db")
if "DATABASE_URL" not in os.environ and os.path.exists(_DB):
    os.remove(_DB)
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_DB}")
os.environ.setdefault("JWT_SECRET", "cc-test-secret")
os.environ.setdefault("SEED_ON_START", "true")
os.environ.setdefault("SEED_PASSWORD", "owner-test-pw")

import itertools

from fastapi.testclient import TestClient

import changes
import events
import models
from database import SessionLocal
from main import app
from security import hash_pw

with TestClient(app):
    pass
client = TestClient(app)
_seq = itertools.count(1)


def _owner():
    db = SessionLocal()
    op = db.get(models.Operator, "OP-owner")
    db.close()
    return op


def _mkop(oid):
    db = SessionLocal()
    try:
        if not db.get(models.Operator, oid):
            db.add(models.Operator(id=oid, name=oid, email=f"{oid}@x", password_hash=hash_pw("x"),
                                   platform_role="operator"))
            db.commit()
    finally:
        db.close()


def _mkcusts(n, region="us"):
    """Create n customers; return their ids (scoped by unique external_ref batch)."""
    db = SessionLocal()
    ids = []
    try:
        batch = next(_seq)
        for i in range(n):
            c = models.CustomerRef(erp_product_id="smokestack", name=f"m3-{batch}-{i}",
                                   external_ref=f"m3-{batch}-{i}", status="active", region=region)
            db.add(c)
            db.flush()
            ids.append(c.id)
        db.commit()
    finally:
        db.close()
    return ids


def _h():
    r = client.post("/auth/login", data={"username": "OP-owner", "password": "owner-test-pw"})
    return {"Authorization": "Bearer " + r.json()["access_token"]}


# ------------------------------- preview / planning -------------------------------
def test_preview_is_dry_run_no_persist():
    ids = _mkcusts(3)
    db = SessionLocal()
    before = db.query(models.ChangeJob).count()
    pv = changes.preview(db, "customer.set_status", {"ids": ids}, {"status": "trial"})
    assert pv["target_count"] == 3 and pv["blast_radius"] == "small"
    assert db.query(models.ChangeJob).count() == before          # nothing persisted
    db.close()


def test_blast_radius_and_approval_policy_scale():
    db = SessionLocal()
    p1 = changes.preview(db, "customer.set_status", {"ids": _mkcusts(1)}, {"status": "trial"})
    p12 = changes.preview(db, "customer.set_status", {"ids": _mkcusts(12)}, {"status": "trial"})
    pdes = changes.preview(db, "customer.set_status", {"ids": _mkcusts(1)}, {"status": "suspended"})
    db.close()
    assert p1["blast_radius"] == "single" and p1["approval_policy"] == "none"
    assert p12["blast_radius"] == "large" and p12["approval_policy"] == "m_of_n"
    assert pdes["destructive"] is True and pdes["approval_policy"] == "m_of_n"  # destructive escalates


# ------------------------------- happy path via rings -------------------------------
def test_single_target_no_approval_runs_to_completion():
    ids = _mkcusts(1)
    db = SessionLocal()
    job = changes.create_job(db, _owner(), name="j", command_type="customer.set_status",
                             filters={"ids": ids}, params={"status": "trial"}, reason="tune")
    assert job.status == "approved"                              # no approval needed
    changes.run(db, job.id)
    assert db.get(models.ChangeJob, job.id).status == "completed"
    assert db.get(models.CustomerRef, ids[0]).status == "trial"
    db.close()


def test_small_group_single_approval_then_run():
    _mkop("OP-appr")
    ids = _mkcusts(3)
    db = SessionLocal()
    job = changes.create_job(db, _owner(), name="j", command_type="customer.set_status",
                             filters={"ids": ids}, params={"status": "trial"}, reason="tune")
    assert job.status == "awaiting_approval" and job.approval_policy == "single"
    changes.approve_job(db, "OP-appr", job.id, "approve", "ok")
    assert db.get(models.ChangeJob, job.id).status == "approved"
    changes.run(db, job.id)
    assert db.get(models.ChangeJob, job.id).status == "completed"
    assert all(db.get(models.CustomerRef, i).status == "trial" for i in ids)
    db.close()


def test_large_group_m_of_n_two_operators():
    _mkop("OP-q1")
    _mkop("OP-q2")
    ids = _mkcusts(12)
    db = SessionLocal()
    job = changes.create_job(db, _owner(), name="big", command_type="customer.set_status",
                             filters={"ids": ids}, params={"status": "trial"}, reason="rollout")
    assert job.approval_policy == "m_of_n"
    changes.approve_job(db, "OP-q1", job.id, "approve", "ok")
    assert db.get(models.ChangeJob, job.id).status == "awaiting_approval"   # 1 of 2
    changes.approve_job(db, "OP-q2", job.id, "approve", "ok")
    assert db.get(models.ChangeJob, job.id).status == "approved"            # quorum met
    changes.run(db, job.id)
    assert db.get(models.ChangeJob, job.id).status == "completed"
    db.close()


# ------------------------------- safety mechanisms -------------------------------
def test_lease_conflict_excludes_overlapping_targets():
    ids = _mkcusts(3)
    db = SessionLocal()
    changes.create_job(db, _owner(), name="A", command_type="customer.set_status",
                       filters={"ids": ids[:2]}, params={"status": "trial"}, reason="a")
    # B overlaps ids[:2] and adds ids[2]; overlapping targets belong to non-terminal A → excluded
    jobB = changes.create_job(db, _owner(), name="B", command_type="customer.set_status",
                              filters={"ids": ids}, params={"status": "trial"}, reason="b")
    assert jobB.total_targets == 1                               # only the non-conflicted target
    db.close()


def test_duplicate_execution_is_idempotent():
    ids = _mkcusts(1)
    db = SessionLocal()
    job = changes.create_job(db, _owner(), name="j", command_type="customer.set_status",
                             filters={"ids": ids}, params={"status": "trial"}, reason="x")
    changes.run(db, job.id)
    v1 = db.get(models.CustomerRef, ids[0]).version
    changes.run(db, job.id)                                      # run again
    changes.execute_tick(db, job.id)                             # tick a completed job
    assert db.get(models.CustomerRef, ids[0]).version == v1     # no double-apply
    db.close()


def test_stale_version_fails_and_auto_halts_on_error_budget():
    ids = _mkcusts(1)
    db = SessionLocal()
    job = changes.create_job(db, _owner(), name="j", command_type="customer.set_status",
                             filters={"ids": ids}, params={"status": "trial"}, reason="x")
    # someone changes the customer out-of-band → planned expected_version is now stale
    c = db.get(models.CustomerRef, ids[0])
    c.version = (c.version or 1) + 5
    db.commit()
    changes.run(db, job.id)
    job = db.get(models.ChangeJob, job.id)
    assert job.status == "halted" and job.halt_reason == "error_budget_exceeded"
    t = db.query(models.ChangeTarget).filter_by(job_id=job.id).first()
    assert t.status == "failed" and t.error == "version_conflict" and t.attempts >= 1
    db.close()


def test_approval_revoked_mid_execution_halts():
    _mkop("OP-r1")
    _mkop("OP-r2")
    ids = _mkcusts(12)
    db = SessionLocal()
    job = changes.create_job(db, _owner(), name="j", command_type="customer.set_status",
                             filters={"ids": ids}, params={"status": "trial"}, reason="x")
    changes.approve_job(db, "OP-r1", job.id, "approve", "ok")
    changes.approve_job(db, "OP-r2", job.id, "approve", "ok")
    changes.execute_tick(db, job.id)                            # run canary ring
    # revoke the approval mid-flight
    req = db.get(models.ApprovalRequest, job.approval_request_id)
    req.status = "cancelled"
    db.commit()
    changes.execute_tick(db, job.id)
    job = db.get(models.ChangeJob, job.id)
    assert job.status == "halted" and job.halt_reason == "approval_revoked"
    db.close()


def test_pause_resume_and_abort():
    ids = _mkcusts(12)
    db = SessionLocal()
    job = changes.create_job(db, _owner(), name="j", command_type="customer.set_status",
                             filters={"ids": ids}, params={"status": "trial"}, reason="x")
    # single-approver? no — large ⇒ m_of_n. approve.
    _mkop("OP-p1")
    _mkop("OP-p2")
    changes.approve_job(db, "OP-p1", job.id, "approve", "ok")
    changes.approve_job(db, "OP-p2", job.id, "approve", "ok")
    changes.execute_tick(db, job.id)
    changes.pause(db, job.id)
    assert db.get(models.ChangeJob, job.id).status == "paused"
    changes.resume(db, job.id)
    assert db.get(models.ChangeJob, job.id).status in ("running", "approved")
    changes.run(db, job.id)
    assert db.get(models.ChangeJob, job.id).status == "completed"
    # a fresh job can be aborted
    ids2 = _mkcusts(1)
    j2 = changes.create_job(db, _owner(), name="j2", command_type="customer.set_status",
                            filters={"ids": ids2}, params={"status": "trial"}, reason="x")
    changes.abort(db, j2.id)
    assert db.get(models.ChangeJob, j2.id).status == "aborted"
    db.close()


def test_rollback_reverts_all_succeeded_targets():
    ids = _mkcusts(3)
    _mkop("OP-rb")
    db = SessionLocal()
    job = changes.create_job(db, _owner(), name="susp", command_type="customer.set_status",
                             filters={"ids": ids}, params={"status": "suspended"}, reason="incident")
    # destructive ⇒ m_of_n
    _mkop("OP-rb2")
    changes.approve_job(db, "OP-rb", job.id, "approve", "ok")
    changes.approve_job(db, "OP-rb2", job.id, "approve", "ok")
    changes.run(db, job.id)
    assert all(db.get(models.CustomerRef, i).status == "suspended" for i in ids)
    changes.rollback(db, job.id)
    assert db.get(models.ChangeJob, job.id).status == "rolled_back"
    assert all(db.get(models.CustomerRef, i).status == "active" for i in ids)
    db.close()


# ------------------------------- events / read model / overview -------------------------------
def test_job_emits_outbox_events_and_relays():
    ids = _mkcusts(1)
    db = SessionLocal()
    job = changes.create_job(db, _owner(), name="j", command_type="customer.set_status",
                             filters={"ids": ids}, params={"status": "trial"}, reason="x")
    changes.run(db, job.id)
    types = {r.event_type for r in db.query(models.Outbox)
             .filter(models.Outbox.aggregate_id == str(job.id)).all()}
    assert "change_job.created" in types and "change_job.completed" in types
    summary = events.relay(db)                                   # replay/deliver
    assert summary["published"] >= 0
    db.close()


def test_overview_and_targets_endpoints():
    ids = _mkcusts(1)
    h = _h()
    job = client.post("/api/platform/change-jobs", headers=h,
                      json={"command_type": "customer.set_status", "filters": {"ids": ids},
                            "params": {"status": "trial"}, "reason": "api"}).json()
    client.post(f"/api/platform/change-jobs/{job['id']}/run", headers=h)
    tgts = client.get(f"/api/platform/change-jobs/{job['id']}/targets", headers=h).json()
    assert len(tgts) == 1 and tgts[0]["status"] == "succeeded"
    ov = client.get("/api/platform/change-jobs-overview", headers=h).json()
    assert "running" in ov and "halted" in ov and "critical" in ov


def test_no_available_targets_rejected():
    db = SessionLocal()
    try:
        changes.create_job(db, _owner(), name="empty", command_type="customer.set_status",
                           filters={"ids": [999999]}, params={"status": "trial"}, reason="x")
        assert False
    except changes.ChangeError as e:
        assert e.code == "no_available_targets"
    finally:
        db.close()

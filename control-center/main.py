"""PFS Control Center — Milestone 1 (Foundation).

Metadata-only Control Plane. Registers and displays the fleet; read-only toward ERP
runtimes (a single outbound GET to a health URL). Does NOT deploy, orchestrate, provision
Master runtimes, access customer transactional data, or consume Enter-ERP grants (deferred).
"""
import datetime
import hashlib
import json
import os
import secrets
import urllib.request

import sqlalchemy as sa
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel
from sqlalchemy.orm import Session

import approvals
import audit_chain
import bff
import breakglass
import changes
import commands
import events
import iam
import models
import readmodels
import sessions
import streaming
from config import settings
from database import Base, SessionLocal, engine, get_db
from security import audit, create_token, current_operator, verify_pw

app = FastAPI(title="PFS Control Center", version="0.1.0")


@app.on_event("startup")
def _startup():
    # SQLite (dev/test) convenience only; production Postgres schema is created by Alembic
    # (preDeploy `alembic upgrade head`), never by create_all (Governance §2.7).
    if engine.url.get_backend_name() == "sqlite":
        Base.metadata.create_all(bind=engine)
    if settings.seed_on_start:
        db = SessionLocal()
        try:
            from seed import seed
            seed(db)
        finally:
            db.close()


# ----------------------------- health & dashboard -----------------------------
@app.get("/health")
def health(db: Session = Depends(get_db)):
    try:
        db.execute(sa.text("SELECT 1"))
        dbok = "ok"
    except Exception:
        dbok = "error"
    return {"status": "ok", "service": "pfs-control-center", "version": "0.1.0",
            "checks": {"database": dbok}}


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return FileResponse(os.path.join(os.path.dirname(__file__), "index.html"))


# ----------------------------- auth (operator realm) -----------------------------
@app.post("/auth/login")
def login(form: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    op = db.get(models.Operator, form.username)
    if not op or op.status != "active" or not verify_pw(form.password, op.password_hash):
        raise HTTPException(401, "Invalid credentials")
    jti = secrets.token_urlsafe(16)                       # bind the token to a revocable session
    sessions.open_session(db, op, jti)
    return {"access_token": create_token(op.id, jti=jti), "token_type": "bearer",
            "operator": op.id, "role": op.platform_role}


# ----------------------------- serialisers -----------------------------
def _product(p):
    return {"id": p.id, "name": p.name, "description": p.description, "status": p.status}


def _env(e):
    return {"id": e.id, "erp_product_id": e.erp_product_id, "kind": e.kind,
            "display_name": e.display_name, "status": e.status}


def _release(r):
    return {"id": r.id, "erp_product_id": r.erp_product_id, "version": r.version,
            "source_sha": r.source_sha, "build_identity": r.build_identity,
            "source_master_runtime": r.source_master_runtime, "status": r.status,
            "is_legacy_import": bool(r.is_legacy_import),
            "published_at": r.published_at.isoformat() if r.published_at else None,
            "published_by": r.published_by, "notes": r.notes}


def _runtime(r):
    return {"id": r.id, "erp_product_id": r.erp_product_id, "tier": r.tier,
            "environment_kind": r.environment_kind, "name": r.name, "url": r.url,
            "health_url": r.health_url, "status": r.status,
            "current_release_id": r.current_release_id,
            "last_health_state": r.last_health_state,
            "last_health_at": r.last_health_at.isoformat() if r.last_health_at else None}


def _customer(c):
    return {"id": c.id, "erp_product_id": c.erp_product_id, "name": c.name,
            "external_ref": c.external_ref, "status": c.status, "notes": c.notes}


def _cdep(d, db):
    cust = db.get(models.CustomerRef, d.customer_ref_id)
    rel = db.get(models.Release, d.release_id) if d.release_id else None
    rt = db.get(models.Runtime, d.runtime_id) if d.runtime_id else None
    return {"id": d.id, "customer_ref_id": d.customer_ref_id,
            "customer_name": cust.name if cust else None,
            "erp_product_id": cust.erp_product_id if cust else None,
            "tenant_ref": d.tenant_ref, "release_id": d.release_id,
            "release_version": rel.version if rel else None, "runtime_id": d.runtime_id,
            "runtime_name": rt.name if rt else None, "status": d.status}


def _dep(d, db):
    rt = db.get(models.Runtime, d.runtime_id)
    rel = db.get(models.Release, d.release_id) if d.release_id else None
    return {"id": d.id, "runtime_id": d.runtime_id, "runtime_name": rt.name if rt else None,
            "release_id": d.release_id, "release_version": rel.version if rel else None,
            "kind": d.kind, "status": d.status, "health_at_observe": d.health_at_observe,
            "observed_at": d.observed_at.isoformat() if d.observed_at else None}


def _iso(dt):
    return dt.isoformat() if dt else None


def _license(lic):
    return {"id": lic.id, "erp_product_id": lic.erp_product_id,
            "customer_ref_id": lic.customer_ref_id, "plan": lic.plan, "status": lic.status,
            "start_date": _iso(lic.start_date), "expiry_date": _iso(lic.expiry_date),
            "seat_limit": lic.seat_limit, "branch_limit": lic.branch_limit,
            "notes": lic.notes, "created_by": lic.created_by,
            "created_at": _iso(lic.created_at), "updated_at": _iso(lic.updated_at)}


def _effective_session_status(s):
    """Compute the live status without mutating the row (expiry is time-derived).

    Timezone-safe: `DateTime(timezone=True)` columns come back tz-aware on PostgreSQL but
    naive on SQLite, so we build `now` with the SAME awareness as `expires_at` before
    comparing (avoids 'can't compare offset-naive and offset-aware datetimes').
    """
    if s.status in ("revoked",):
        return "revoked"
    exp = s.expires_at
    if exp is not None:
        now = (datetime.datetime.now(exp.tzinfo) if exp.tzinfo is not None
               else datetime.datetime.utcnow())
        if now >= exp:
            return "expired"
    return s.status


def _session(s, db):
    cust = db.get(models.CustomerRef, s.customer_ref_id) if s.customer_ref_id else None
    return {"id": s.id, "session_ref": s.session_ref, "erp_product_id": s.erp_product_id,
            "customer_ref_id": s.customer_ref_id,
            "customer_name": cust.name if cust else None,
            "operator_id": s.operator_id, "reason": s.reason,
            "capabilities": s.capabilities, "status": _effective_session_status(s),
            "stored_status": s.status, "target_url": s.target_url,
            "created_at": _iso(s.created_at), "expires_at": _iso(s.expires_at),
            "revoked_at": _iso(s.revoked_at), "revoked_by": s.revoked_by}


def _customer_runtime(db, pid):
    """The shared Customer-Production runtime for a product (deployment_type = shared)."""
    return (db.query(models.Runtime)
            .filter_by(erp_product_id=pid, tier="customer",
                       environment_kind="customer_production")
            .order_by(models.Runtime.id).first())


def _enriched_customer(c, db, product_runtime=None):
    """Accountant customer row. Health/last-sync are HONEST: per-customer telemetry is not
    yet integrated (no ERP heartbeat), so health is inherited from the shared runtime and
    last-sync is explicitly 'not_yet_integrated' — never fabricated (per product spec)."""
    dep = (db.query(models.CustomerDeployment)
           .filter_by(customer_ref_id=c.id).order_by(models.CustomerDeployment.id.desc()).first())
    rel = db.get(models.Release, dep.release_id) if (dep and dep.release_id) else None
    rt = db.get(models.Runtime, dep.runtime_id) if (dep and dep.runtime_id) else product_runtime
    lic = (db.query(models.License).filter_by(customer_ref_id=c.id)
           .order_by(models.License.id.desc()).first())
    inherited = (rt.last_health_state if rt else None)
    return {
        "id": c.id, "erp_product_id": c.erp_product_id, "name": c.name,
        "external_ref": c.external_ref, "status": c.status, "notes": c.notes,
        "license_plan": lic.plan if lic else None,
        "license_status": lic.status if lic else "unlicensed",
        "license_id": lic.id if lic else None,
        "current_version": rel.version if rel else None,
        "current_version_is_legacy": bool(rel.is_legacy_import) if rel else False,
        # honest health/sync: inherited from runtime; per-customer not integrated
        "health_source": "inherited_from_runtime" if rt else "unknown",
        "health": inherited or "unknown",
        "last_sync_state": "not_yet_integrated",
        "last_sync_at": None,
        "deployment_type": ("customer_production_shared" if rt else "unassigned"),
        "runtime_id": rt.id if rt else None,
        "runtime_name": rt.name if rt else None,
        "target_url": (rt.url if rt else None),
    }


# ----------------------------- ERP products & master environments -----------------------------
class ProductIn(BaseModel):
    id: str
    name: str
    description: str = ""


@app.get("/api/products")
def list_products(db: Session = Depends(get_db), op=Depends(current_operator)):
    return [_product(p) for p in db.query(models.ErpProduct).order_by(models.ErpProduct.id).all()]


@app.post("/api/products", status_code=201)
def create_product(body: ProductIn, db: Session = Depends(get_db), op=Depends(current_operator)):
    if db.get(models.ErpProduct, body.id):
        raise HTTPException(409, "ERP product already exists")
    db.add(models.ErpProduct(id=body.id, name=body.name, description=body.description))
    db.flush()
    for kind, dn in [("master_development", "Master Development"),
                     ("master_testing", "Master Testing"),
                     ("master_production", "Master Production")]:
        db.add(models.MasterEnvironment(erp_product_id=body.id, kind=kind, display_name=dn))
    db.commit()
    audit(db, op, "create", "erp_product", body.id)
    return {"ok": True, "id": body.id}


@app.get("/api/products/{pid}/environments")
def list_environments(pid: str, db: Session = Depends(get_db), op=Depends(current_operator)):
    return [_env(e) for e in db.query(models.MasterEnvironment)
            .filter_by(erp_product_id=pid).order_by(models.MasterEnvironment.id).all()]


# ----------------------------- releases -----------------------------
class ReleaseIn(BaseModel):
    erp_product_id: str
    version: str
    source_sha: str = ""
    build_identity: str = ""
    source_environment_kind: str = "master_production"
    source_master_runtime: str | None = None
    is_legacy_import: bool = False
    notes: str = ""


@app.get("/api/releases")
def list_releases(db: Session = Depends(get_db), op=Depends(current_operator)):
    return [_release(r) for r in db.query(models.Release).order_by(models.Release.id.desc()).all()]


@app.post("/api/releases", status_code=201)
def create_release(body: ReleaseIn, db: Session = Depends(get_db), op=Depends(current_operator)):
    if not db.get(models.ErpProduct, body.erp_product_id):
        raise HTTPException(404, "ERP product not found")
    if body.is_legacy_import:
        status = "imported_legacy"                      # bootstrap exception, explicitly marked
    else:
        # Permanent rule (ADR-028 / Decision 3): only Master Production may publish a Release.
        if body.source_environment_kind != "master_production":
            raise HTTPException(400, "Only Master Production may publish a Release (ADR-028).")
        status = "published"
    r = models.Release(
        erp_product_id=body.erp_product_id, version=body.version, source_sha=body.source_sha,
        build_identity=body.build_identity, source_master_runtime=body.source_master_runtime,
        status=status, is_legacy_import=body.is_legacy_import,
        published_at=datetime.datetime.utcnow(), published_by=op.id, notes=body.notes)
    db.add(r)
    db.commit()
    audit(db, op, "publish" if status == "published" else "import_legacy", "release", r.id,
          f"{body.erp_product_id} {body.version} ({status})")
    return {"ok": True, "id": r.id, "status": status}


# ----------------------------- runtimes & health -----------------------------
class RuntimeIn(BaseModel):
    erp_product_id: str
    tier: str                     # master | customer
    environment_kind: str
    name: str
    url: str = ""
    health_url: str = ""
    current_release_id: int | None = None
    notes: str = ""


@app.get("/api/runtimes")
def list_runtimes(db: Session = Depends(get_db), op=Depends(current_operator)):
    return [_runtime(r) for r in db.query(models.Runtime).order_by(models.Runtime.id).all()]


@app.post("/api/runtimes", status_code=201)
def register_runtime(body: RuntimeIn, db: Session = Depends(get_db), op=Depends(current_operator)):
    if body.tier not in ("master", "customer"):
        raise HTTPException(422, "tier must be 'master' or 'customer'")
    if not db.get(models.ErpProduct, body.erp_product_id):
        raise HTTPException(404, "ERP product not found")
    r = models.Runtime(erp_product_id=body.erp_product_id, tier=body.tier,
                       environment_kind=body.environment_kind, name=body.name, url=body.url,
                       health_url=body.health_url, current_release_id=body.current_release_id,
                       notes=body.notes, status="registered")
    db.add(r)
    db.commit()
    audit(db, op, "register", "runtime", r.id, body.name)
    return {"ok": True, "id": r.id}


def _poll_runtime(rt):
    """Read-only outbound GET to a runtime's health URL; returns (state, detail). No DB writes."""
    if not rt.health_url:
        return "unknown", ""
    try:
        req = urllib.request.Request(rt.health_url, headers={"User-Agent": "pfs-control-center"})
        with urllib.request.urlopen(req, timeout=8) as resp:   # noqa: S310 (operator-registered URL)
            body = resp.read().decode("utf-8", "replace")[:500]
            code = resp.status
        j = json.loads(body) if body.strip().startswith("{") else {}
        state = "ok" if (code == 200 and j.get("status") == "ok") else "degraded"
        return state, body[:200]
    except Exception as e:   # noqa: BLE001
        return "unreachable", str(e)[:200]


@app.post("/api/runtimes/{rid}/health-check")
def health_check(rid: int, db: Session = Depends(get_db), op=Depends(current_operator)):
    """Read-only outbound GET to a runtime's health URL; records the last-known state."""
    rt = db.get(models.Runtime, rid)
    if not rt:
        raise HTTPException(404, "Runtime not found")
    state, detail = _poll_runtime(rt)
    rt.last_health_state = state
    rt.last_health_at = datetime.datetime.utcnow()
    rt.last_health_detail = detail
    db.commit()
    audit(db, op, "health_check", "runtime", rt.id, state)
    return {"runtime": rt.id, "health": state, "detail": detail}


@app.post("/api/runtimes/health-check-all")
def health_check_all(db: Session = Depends(get_db), op=Depends(current_operator)):
    """Poll every customer-tier runtime so Platform Health reflects real operational status."""
    rts = db.query(models.Runtime).filter_by(tier="customer").all()
    by_health = {}
    for rt in rts:
        state, detail = _poll_runtime(rt)
        rt.last_health_state = state
        rt.last_health_at = datetime.datetime.utcnow()
        rt.last_health_detail = detail
        by_health[state] = by_health.get(state, 0) + 1
    db.commit()
    audit(db, op, "health_check_all", "fleet", "", f"checked {len(rts)} runtimes")
    return {"checked": len(rts), "by_health": by_health}


# ----------------------------- customers & deployments -----------------------------
class CustomerIn(BaseModel):
    erp_product_id: str
    name: str
    external_ref: str = ""
    notes: str = ""


@app.get("/api/customers")
def list_customers(db: Session = Depends(get_db), op=Depends(current_operator)):
    return [_customer(c) for c in db.query(models.CustomerRef).order_by(models.CustomerRef.id).all()]


@app.post("/api/customers", status_code=201)
def register_customer(body: CustomerIn, db: Session = Depends(get_db), op=Depends(current_operator)):
    if not db.get(models.ErpProduct, body.erp_product_id):
        raise HTTPException(404, "ERP product not found")
    c = models.CustomerRef(erp_product_id=body.erp_product_id, name=body.name,
                           external_ref=body.external_ref, notes=body.notes, status="active")
    db.add(c)
    db.commit()
    audit(db, op, "register", "customer_ref", c.id, body.name)
    return {"ok": True, "id": c.id}


@app.get("/api/customer-deployments")
def list_customer_deployments(db: Session = Depends(get_db), op=Depends(current_operator)):
    return [_cdep(d, db) for d in
            db.query(models.CustomerDeployment).order_by(models.CustomerDeployment.id).all()]


@app.get("/api/deployments")
def list_deployments(db: Session = Depends(get_db), op=Depends(current_operator)):
    return [_dep(d, db) for d in
            db.query(models.Deployment).order_by(models.Deployment.id.desc()).all()]


@app.get("/api/products/{pid}/customers")
def list_product_customers(pid: str, q: str = "", status: str = "",
                           db: Session = Depends(get_db), op=Depends(current_operator)):
    """The heart of the ERP workspace: enriched customer rows (search + status filter)."""
    if not db.get(models.ErpProduct, pid):
        raise HTTPException(404, "ERP product not found")
    prt = _customer_runtime(db, pid)
    rows = [_enriched_customer(c, db, prt) for c in
            db.query(models.CustomerRef).filter_by(erp_product_id=pid)
            .order_by(models.CustomerRef.name).all()]
    if q:
        ql = q.lower()
        rows = [r for r in rows if ql in (r["name"] or "").lower()
                or ql in (r["external_ref"] or "").lower()]
    if status:
        rows = [r for r in rows if r["status"] == status]
    return rows


# ----------------------------- licenses (first-class metadata) -----------------------------
_LICENSE_STATUSES = {"trial", "active", "suspended", "expired", "cancelled"}


class LicenseIn(BaseModel):
    erp_product_id: str
    customer_ref_id: int
    plan: str = "standard"
    status: str = "trial"
    start_date: str | None = None
    expiry_date: str | None = None
    seat_limit: int | None = None
    branch_limit: int | None = None
    notes: str = ""


class LicensePatch(BaseModel):
    plan: str | None = None
    status: str | None = None
    start_date: str | None = None
    expiry_date: str | None = None
    seat_limit: int | None = None
    branch_limit: int | None = None
    notes: str | None = None


def _parse_dt(v):
    if not v:
        return None
    try:
        return datetime.datetime.fromisoformat(v.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        raise HTTPException(422, f"Invalid ISO date: {v}")


@app.get("/api/licenses")
def list_licenses(erp_product_id: str = "", customer_ref_id: int | None = None,
                  db: Session = Depends(get_db), op=Depends(current_operator)):
    qy = db.query(models.License)
    if erp_product_id:
        qy = qy.filter_by(erp_product_id=erp_product_id)
    if customer_ref_id is not None:
        qy = qy.filter_by(customer_ref_id=customer_ref_id)
    return [_license(x) for x in qy.order_by(models.License.id.desc()).all()]


@app.post("/api/licenses", status_code=201)
def create_license(body: LicenseIn, db: Session = Depends(get_db), op=Depends(current_operator)):
    if not db.get(models.ErpProduct, body.erp_product_id):
        raise HTTPException(404, "ERP product not found")
    if not db.get(models.CustomerRef, body.customer_ref_id):
        raise HTTPException(404, "Customer not found")
    if body.status not in _LICENSE_STATUSES:
        raise HTTPException(422, f"status must be one of {sorted(_LICENSE_STATUSES)}")
    lic = models.License(
        erp_product_id=body.erp_product_id, customer_ref_id=body.customer_ref_id,
        plan=body.plan, status=body.status, start_date=_parse_dt(body.start_date),
        expiry_date=_parse_dt(body.expiry_date), seat_limit=body.seat_limit,
        branch_limit=body.branch_limit, notes=body.notes, created_by=op.id)
    db.add(lic)
    db.commit()
    audit(db, op, "create", "license", lic.id, f"{body.erp_product_id}/{body.plan}/{body.status}")
    return {"ok": True, "id": lic.id, "license": _license(lic)}


@app.patch("/api/licenses/{lid}")
def update_license(lid: int, body: LicensePatch,
                   db: Session = Depends(get_db), op=Depends(current_operator)):
    lic = db.get(models.License, lid)
    if not lic:
        raise HTTPException(404, "License not found")
    if body.status is not None:
        if body.status not in _LICENSE_STATUSES:
            raise HTTPException(422, f"status must be one of {sorted(_LICENSE_STATUSES)}")
        lic.status = body.status
    if body.plan is not None:
        lic.plan = body.plan
    if body.start_date is not None:
        lic.start_date = _parse_dt(body.start_date)
    if body.expiry_date is not None:
        lic.expiry_date = _parse_dt(body.expiry_date)
    if body.seat_limit is not None:
        lic.seat_limit = body.seat_limit
    if body.branch_limit is not None:
        lic.branch_limit = body.branch_limit
    if body.notes is not None:
        lic.notes = body.notes
    db.commit()
    audit(db, op, "update", "license", lic.id, f"{lic.plan}/{lic.status}")
    return {"ok": True, "license": _license(lic)}


# ----------------------------- support sessions ("Open ERP") -----------------------------
class SupportSessionIn(BaseModel):
    erp_product_id: str
    customer_ref_id: int
    reason: str = ""
    capabilities: str = "support:read"     # restricted by default
    minutes: int | None = None             # optional override of default lifetime


@app.get("/api/support-sessions")
def list_support_sessions(erp_product_id: str = "", active_only: bool = False,
                          db: Session = Depends(get_db), op=Depends(current_operator)):
    qy = db.query(models.SupportSession)
    if erp_product_id:
        qy = qy.filter_by(erp_product_id=erp_product_id)
    rows = [_session(s, db) for s in qy.order_by(models.SupportSession.id.desc()).all()]
    if active_only:
        rows = [r for r in rows if r["status"] in ("active", "pending_erp_integration")]
    return rows


@app.post("/api/support-sessions", status_code=201)
def open_support_session(body: SupportSessionIn,
                         db: Session = Depends(get_db), op=Depends(current_operator)):
    """Open ERP: mint a short-lived, capability-scoped, auditable, revocable support grant.

    NEVER uses a customer password (ADR-025). ERP-side consumption is not implemented yet,
    so the session is created 'pending_erp_integration': we record the grant and expose the
    registered customer ERP URL as metadata — we do NOT authenticate into the ERP.
    """
    if not db.get(models.ErpProduct, body.erp_product_id):
        raise HTTPException(404, "ERP product not found")
    cust = db.get(models.CustomerRef, body.customer_ref_id)
    if not cust or cust.erp_product_id != body.erp_product_id:
        raise HTTPException(404, "Customer not found for this ERP product")
    prt = _customer_runtime(db, body.erp_product_id)
    dep = (db.query(models.CustomerDeployment)
           .filter_by(customer_ref_id=cust.id).order_by(models.CustomerDeployment.id.desc()).first())
    rt = db.get(models.Runtime, dep.runtime_id) if (dep and dep.runtime_id) else prt
    minutes = body.minutes or settings.support_session_minutes
    minutes = max(1, min(minutes, 240))     # clamp: short-lived by design
    s = models.SupportSession(
        session_ref="sess_" + secrets.token_urlsafe(16),
        erp_product_id=body.erp_product_id, customer_ref_id=cust.id, operator_id=op.id,
        reason=body.reason, capabilities=(body.capabilities or "support:read"),
        status="pending_erp_integration", target_url=(rt.url if rt else None),
        expires_at=datetime.datetime.utcnow() + datetime.timedelta(minutes=minutes))
    db.add(s)
    db.commit()
    audit(db, op, "open_support_session", "support_session", s.id,
          f"{body.erp_product_id}/customer:{cust.external_ref}/caps:{s.capabilities}")
    return {"ok": True, "id": s.id, "session": _session(s, db),
            "note": "Pending ERP Integration — session recorded and audited; the Control Center "
                    "does not authenticate into the ERP (ERP-side consumption deferred)."}


@app.post("/api/support-sessions/{sid}/revoke")
def revoke_support_session(sid: int, db: Session = Depends(get_db), op=Depends(current_operator)):
    s = db.get(models.SupportSession, sid)
    if not s:
        raise HTTPException(404, "Support session not found")
    if s.status != "revoked":
        s.status = "revoked"
        s.revoked_at = datetime.datetime.utcnow()
        s.revoked_by = op.id
        db.commit()
        audit(db, op, "revoke_support_session", "support_session", s.id)
    return {"ok": True, "session": _session(s, db)}


# ----------------------------- fleet & audit -----------------------------
@app.get("/api/fleet")
def fleet(db: Session = Depends(get_db), op=Depends(current_operator)):
    rts = db.query(models.Runtime).all()
    by_health = {}
    for r in rts:
        s = r.last_health_state or "unknown"
        by_health[s] = by_health.get(s, 0) + 1
    return {"products": db.query(models.ErpProduct).count(),
            "runtimes": len(rts),
            "customer_runtimes": sum(1 for r in rts if r.tier == "customer"),
            "master_runtimes": sum(1 for r in rts if r.tier == "master"),
            "customers": db.query(models.CustomerRef).count(),
            "releases": db.query(models.Release).count(),
            "by_health": by_health}


@app.get("/api/audit")
def list_audit(limit: int = 200, db: Session = Depends(get_db), op=Depends(current_operator)):
    rows = (db.query(models.PlatformAuditLog)
            .order_by(models.PlatformAuditLog.id.desc()).limit(min(limit, 500)).all())
    return [_audit_row(a) for a in rows]


def _audit_row(a):
    return {"id": a.id, "actor": a.actor_operator_id, "action": a.action,
            "target_type": a.target_type, "target_id": a.target_id, "detail": a.detail,
            "result": a.result, "at": a.at.isoformat() if a.at else None}


# ----------------------------- ERP details (aggregate for the details page) -----------------------------
@app.get("/api/products/{pid}/overview")
def product_overview(pid: str, db: Session = Depends(get_db), op=Depends(current_operator)):
    p = db.get(models.ErpProduct, pid)
    if not p:
        raise HTTPException(404, "ERP product not found")
    envs = [_env(e) for e in db.query(models.MasterEnvironment)
            .filter_by(erp_product_id=pid).order_by(models.MasterEnvironment.id).all()]
    runtimes = []
    for r in db.query(models.Runtime).filter_by(erp_product_id=pid).order_by(models.Runtime.id).all():
        row = _runtime(r)
        rel = db.get(models.Release, r.current_release_id) if r.current_release_id else None
        row["current_release_version"] = rel.version if rel else None
        row["current_release_is_legacy"] = bool(rel.is_legacy_import) if rel else False
        runtimes.append(row)
    releases = [_release(r) for r in db.query(models.Release)
                .filter_by(erp_product_id=pid).order_by(models.Release.id.desc()).all()]
    cust_ids = [c.id for c in db.query(models.CustomerRef).filter_by(erp_product_id=pid).all()]
    cdeps = [_cdep(d, db) for d in db.query(models.CustomerDeployment)
             .filter(models.CustomerDeployment.customer_ref_id.in_(cust_ids or [-1])).all()]
    rt_ids = [r["id"] for r in runtimes]
    deps = [_dep(d, db) for d in db.query(models.Deployment)
            .filter(models.Deployment.runtime_id.in_(rt_ids or [-1]))
            .order_by(models.Deployment.id.desc()).all()]
    target_ids = ({pid} | {str(i) for i in rt_ids}
                  | {str(x["id"]) for x in releases} | {str(i) for i in cust_ids})
    audit = [_audit_row(a) for a in db.query(models.PlatformAuditLog)
             .filter(models.PlatformAuditLog.target_id.in_(target_ids or {"__none__"}))
             .order_by(models.PlatformAuditLog.id.desc()).limit(50).all()]
    prt = _customer_runtime(db, pid)
    customers = [_enriched_customer(c, db, prt) for c in
                 db.query(models.CustomerRef).filter_by(erp_product_id=pid)
                 .order_by(models.CustomerRef.name).all()]
    licenses = [_license(x) for x in db.query(models.License)
                .filter_by(erp_product_id=pid).order_by(models.License.id.desc()).all()]
    sessions = [_session(s, db) for s in db.query(models.SupportSession)
                .filter_by(erp_product_id=pid).order_by(models.SupportSession.id.desc()).all()]
    active_license_count = sum(1 for x in licenses if x["status"] in ("active", "trial"))
    active_session_count = sum(1 for s in sessions if s["status"] in ("active", "pending_erp_integration"))
    current_version = None
    for r in runtimes:
        if r["tier"] == "customer" and r.get("current_release_version"):
            current_version = r["current_release_version"]
            break
    summary = {
        "customers": len(customers),
        "active_licenses": active_license_count,
        "versions": len(releases),
        "current_version": current_version,
        "open_sessions": active_session_count,
        "erp_health": (prt.last_health_state if prt else "unknown"),
        "health_url": (prt.health_url if prt else None),
        "customer_url": (prt.url if prt else None),
    }
    return {"product": _product(p), "summary": summary, "environments": envs, "runtimes": runtimes,
            "releases": releases, "customers": customers, "licenses": licenses,
            "support_sessions": sessions, "customer_deployments": cdeps,
            "deployments": deps, "audit": audit}


@app.get("/api/home")
def home(db: Session = Depends(get_db), op=Depends(current_operator)):
    """Platform Home: one card per ERP Product for the 'My ERP Products' grid."""
    cards = []
    for p in db.query(models.ErpProduct).order_by(models.ErpProduct.name).all():
        prt = _customer_runtime(db, p.id)
        n_cust = db.query(models.CustomerRef).filter_by(erp_product_id=p.id).count()
        lics = (db.query(models.License).filter_by(erp_product_id=p.id)
                .filter(models.License.status.in_(["active", "trial"])).all())
        seats = [x.seat_limit for x in lics if x.seat_limit is not None]
        branches = [x.branch_limit for x in lics if x.branch_limit is not None]
        cur = None
        if prt and prt.current_release_id:
            rel = db.get(models.Release, prt.current_release_id)
            cur = rel.version if rel else None
        rt_ids = [r.id for r in db.query(models.Runtime).filter_by(erp_product_id=p.id).all()]
        last_dep = None
        if rt_ids:
            d = (db.query(models.Deployment).filter(models.Deployment.runtime_id.in_(rt_ids))
                 .order_by(models.Deployment.id.desc()).first())
            last_dep = _iso(d.observed_at) if d else None
        last_audit = (db.query(models.PlatformAuditLog)
                      .filter(models.PlatformAuditLog.target_id == p.id)
                      .order_by(models.PlatformAuditLog.id.desc()).first())
        cards.append({
            "id": p.id, "name": p.name, "description": p.description, "status": p.status,
            "customers": n_cust, "active_licenses": len(lics),
            "user_count": (sum(seats) if seats else None),
            "branch_count": (sum(branches) if branches else None),
            "current_version": cur, "erp_health": (prt.last_health_state if prt else "unknown"),
            "last_deployment": last_dep,
            "last_activity": _iso(last_audit.at) if last_audit else _iso(p.created_at),
        })
    return {"products": cards, "operator": {"id": op.id, "name": op.name, "role": op.platform_role}}


@app.get("/api/dashboard")
def dashboard_data(db: Session = Depends(get_db), op=Depends(current_operator)):
    """One call powering the Platform Dashboard widgets (API efficiency)."""
    products = home(db, op)["products"]
    rts = db.query(models.Runtime).all()
    by_health = {}
    for r in rts:
        s = r.last_health_state or "unknown"
        by_health[s] = by_health.get(s, 0) + 1
    lics = db.query(models.License).all()
    lic_by_status = {}
    for x in lics:
        lic_by_status[x.status] = lic_by_status.get(x.status, 0) + 1
    newest_customers = [_customer(c) for c in
                        db.query(models.CustomerRef).order_by(models.CustomerRef.id.desc()).limit(6).all()]
    recent_sessions = [_session(s, db) for s in
                       db.query(models.SupportSession).order_by(models.SupportSession.id.desc()).limit(6).all()]
    latest_updates = [_dep(d, db) for d in
                      db.query(models.Deployment).order_by(models.Deployment.id.desc()).limit(6).all()]
    recent_activity = [_audit_row(a) for a in
                       db.query(models.PlatformAuditLog).order_by(models.PlatformAuditLog.id.desc()).limit(8).all()]
    return {
        "fleet": {"products": len(products),
                  "customers": db.query(models.CustomerRef).count(),
                  "active_licenses": sum(1 for x in lics if x.status in ("active", "trial")),
                  "open_sessions": sum(1 for s in db.query(models.SupportSession).all()
                                       if _effective_session_status(s) in ("active", "pending_erp_integration")),
                  "by_health": by_health},
        "newest_products": products[:6],
        "newest_customers": newest_customers,
        "recent_sessions": recent_sessions,
        "latest_updates": latest_updates,
        "license_summary": {"total": len(lics), "by_status": lic_by_status},
        "recent_activity": recent_activity,
    }


@app.get("/api/search")
def search(q: str = "", db: Session = Depends(get_db), op=Depends(current_operator)):
    """Global search across platform metadata (products, customers, licenses, sessions, versions).

    Read-only and case-insensitive. Returns compact, categorised matches for the top-nav search.
    """
    ql = (q or "").strip().lower()
    if not ql:
        return {"query": q, "products": [], "customers": [], "licenses": [],
                "sessions": [], "versions": [], "audit": []}

    def _match(*vals):
        return any(ql in (str(v).lower()) for v in vals if v is not None)

    products = [{"id": p.id, "name": p.name}
                for p in db.query(models.ErpProduct).order_by(models.ErpProduct.name).all()
                if _match(p.id, p.name, p.description)][:8]
    cust_rows = db.query(models.CustomerRef).order_by(models.CustomerRef.name).all()
    customers = [{"id": c.id, "erp_product_id": c.erp_product_id, "name": c.name,
                  "external_ref": c.external_ref}
                 for c in cust_rows if _match(c.name, c.external_ref)][:8]
    cust_name = {c.id: c.name for c in cust_rows}
    licenses = [{"id": x.id, "erp_product_id": x.erp_product_id, "customer_ref_id": x.customer_ref_id,
                 "customer_name": cust_name.get(x.customer_ref_id), "plan": x.plan, "status": x.status}
                for x in db.query(models.License).order_by(models.License.id.desc()).all()
                if _match(x.plan, x.status, cust_name.get(x.customer_ref_id))][:8]
    sessions = [{"id": s.id, "erp_product_id": s.erp_product_id,
                 "customer_name": cust_name.get(s.customer_ref_id),
                 "status": _effective_session_status(s), "session_ref": s.session_ref}
                for s in db.query(models.SupportSession).order_by(models.SupportSession.id.desc()).all()
                if _match(s.session_ref, cust_name.get(s.customer_ref_id), s.capabilities)][:8]
    versions = [{"id": r.id, "erp_product_id": r.erp_product_id, "version": r.version, "status": r.status}
                for r in db.query(models.Release).order_by(models.Release.id.desc()).all()
                if _match(r.version, r.status, r.source_sha)][:8]
    audit = [{"id": a.id, "action": a.action, "target_type": a.target_type,
              "target_id": a.target_id, "detail": a.detail, "at": _iso(a.at)}
             for a in db.query(models.PlatformAuditLog)
             .order_by(models.PlatformAuditLog.id.desc()).limit(500).all()
             if _match(a.action, a.target_type, a.target_id, a.detail)][:8]
    return {"query": q, "products": products, "customers": customers,
            "licenses": licenses, "sessions": sessions, "versions": versions, "audit": audit}


# ==========================================================================================
#            FEATURE MANAGEMENT & INTERNAL TOOLS (deny-by-default) — Milestone 1
# ==========================================================================================
VISIBILITY_LEVELS = {"customer", "platform_owner_only", "internal_team", "experimental", "disabled"}
ENVIRONMENTS = {"development", "staging", "production"}
_ELEVATED_ACTORS = {"platform_owner", "internal"}


def require_internal(op=Depends(current_operator)):
    """Owner/internal tier gate for /api/platform and /api/internal (deny-by-default → 403).

    `current_operator` already requires a server-validated PFS operator token (else 401). This
    adds the tier check. Platform-Owner status is NEVER read from a query param, header, cookie
    or any client-controlled value — only from the validated operator identity.
    """
    if getattr(op, "platform_role", None) not in ("owner", "operator", "internal"):
        raise HTTPException(403, "Forbidden — internal namespace requires an internal operator")
    return op


LIFECYCLE_STAGES = ["development", "internal_testing", "staging", "pilot",
                    "production", "deprecated", "removed"]


def require_owner(op=Depends(current_operator)):
    """Developer-Mode gate — the authenticated PLATFORM OWNER only (403 for anyone else).

    Developer Mode and every internal developer tool sit behind this. It is validated
    server-side against the operator identity carried by the signed token; it is never
    granted from frontend state, a query param, a header, or a client-set value. A customer
    (who has no operator token at all) fails `current_operator` with 401 before reaching here.
    """
    if getattr(op, "platform_role", None) != "owner":
        raise HTTPException(403, "Forbidden — Developer Mode is restricted to the Platform Owner")
    return op


def _naive(dt):
    return dt.replace(tzinfo=None) if (dt is not None and dt.tzinfo is not None) else dt


def _csv(s):
    return [x.strip() for x in (s or "").split(",") if x.strip()]


def _rollout_bucket(key, ident):
    return int(hashlib.sha256(f"{key}:{ident}".encode()).hexdigest(), 16) % 100


def _flag(f):
    return {"id": f.id, "key": f.key, "name": f.name, "description": f.description,
            "erp_product_id": f.erp_product_id, "module": f.module, "visibility": f.visibility,
            "default_state": bool(f.default_state), "environment_scope": f.environment_scope or "all",
            "customer_allowlist": _csv(f.customer_allowlist), "customer_denylist": _csv(f.customer_denylist),
            "user_allowlist": _csv(f.user_allowlist), "role_requirements": _csv(f.role_requirements),
            "license_plan_requirements": _csv(f.license_plan_requirements),
            "rollout_percentage": f.rollout_percentage or 0,
            "lifecycle_stage": f.lifecycle_stage or "development", "start_date": _iso(f.start_date),
            "expiry_date": _iso(f.expiry_date), "status": f.status, "created_by": f.created_by,
            "updated_by": f.updated_by, "created_at": _iso(f.created_at), "updated_at": _iso(f.updated_at)}


def _flag_snapshot(f):
    return {k: v for k, v in _flag(f).items() if k not in ("created_at", "updated_at", "id")}


def _flag_audit_row(a):
    return {"id": a.id, "feature_key": a.feature_key, "erp_product_id": a.erp_product_id,
            "actor": a.actor_operator_id, "actor_type": a.actor_type, "customer": a.customer_ref,
            "environment": a.environment, "action": a.action,
            "before_state": json.loads(a.before_state) if a.before_state else None,
            "after_state": json.loads(a.after_state) if a.after_state else None,
            "reason": a.reason, "at": _iso(a.at)}


def _log_flag_audit(db, op, flag, action, before, after, reason="", actor_type="platform_owner",
                    customer=None, environment=None, commit=True):
    db.add(models.FeatureFlagAudit(
        feature_flag_id=(flag.id if flag else None), feature_key=(flag.key if flag else None),
        erp_product_id=(flag.erp_product_id if flag else None),
        actor_operator_id=getattr(op, "id", None), actor_type=actor_type,
        customer_ref=customer, environment=environment, action=action,
        before_state=(json.dumps(before) if before is not None else None),
        after_state=(json.dumps(after) if after is not None else None), reason=reason))
    if commit:
        db.commit()


def _valid_support_session(db, ref, erp_product_id):
    """A support session elevates owner/internal access ONLY while it is genuinely valid."""
    if not ref:
        return False, "no_session"
    s = db.query(models.SupportSession).filter_by(session_ref=ref).first()
    if not s:
        return False, "session_not_found"
    st = _effective_session_status(s)
    if st == "revoked":
        return False, "session_revoked"
    if st == "expired":
        return False, "session_expired"
    if erp_product_id and s.erp_product_id != erp_product_id:
        return False, "session_wrong_erp"
    return True, "session_valid"


def evaluate_feature(db, feat, ctx):
    """Pure deny-by-default evaluation → (allow: bool, reason: str).

    ctx: actor_type, erp_product_id, environment, customer_ref, user_id, role, license_plan,
    support_session_ref. A missing/invalid rule denies. Order: existence → status → dates →
    environment → visibility → (denylist → role → license → allowlist/rollout).
    """
    if feat is None:
        return False, "unknown_feature"
    if feat.status != "active":
        return False, "flag_archived"
    now = datetime.datetime.utcnow()
    if feat.start_date and now < _naive(feat.start_date):
        return False, "not_started"
    if feat.expiry_date and now >= _naive(feat.expiry_date):
        return False, "expired"
    env = (ctx.get("environment") or "production").lower()
    scope = (feat.environment_scope or "all").lower()
    if scope != "all":
        envs = _csv(scope)
        if envs and env not in envs:
            return False, "environment_not_in_scope"
    vis = feat.visibility
    if vis == "disabled":
        return False, "feature_disabled"
    actor = ctx.get("actor_type") or "customer"

    def elevated():
        if actor not in _ELEVATED_ACTORS:
            return False, "actor_not_elevated"
        return _valid_support_session(db, ctx.get("support_session_ref"), feat.erp_product_id)

    if vis == "platform_owner_only":
        ok, why = elevated()
        if ok and actor == "platform_owner":
            return True, "owner_session"
        return False, ("owner_only:" + why)
    if vis == "internal_team":
        ok, why = elevated()
        return (True, "internal_session") if ok else (False, "internal_only:" + why)

    # customer-facing (customer | experimental): three-gate + targeting
    cust, role, plan, user = (ctx.get("customer_ref"), ctx.get("role"),
                              ctx.get("license_plan"), ctx.get("user_id"))
    if cust and cust in _csv(feat.customer_denylist):
        return False, "customer_denylisted"
    req_roles = _csv(feat.role_requirements)
    if req_roles and role not in req_roles:
        return False, "role_not_permitted"                    # Role-Permission gate
    req_plans = _csv(feat.license_plan_requirements)
    if req_plans and plan not in req_plans:
        return False, "license_not_entitled"                  # License-Entitlement gate
    allow_c, allow_u = _csv(feat.customer_allowlist), _csv(feat.user_allowlist)
    explicit = (cust in allow_c) or (user in allow_u)
    rp = feat.rollout_percentage or 0
    within_rollout = (rp >= 100) or (rp > 0 and cust is not None and _rollout_bucket(feat.key, cust) < rp)

    if vis == "experimental":                                 # hidden by default
        ok, _why = elevated()
        if ok:
            return True, "experimental_owner"
        return (True, "experimental_enabled") if (explicit or within_rollout) else (False, "experimental_not_enabled")
    # vis == customer: Feature-Flag (released?) = default_state OR allowlist OR rollout
    if bool(feat.default_state) or explicit or within_rollout:
        return True, "granted"
    return False, "not_targeted"


class FeatureFlagIn(BaseModel):
    key: str
    name: str
    description: str = ""
    erp_product_id: str | None = None
    module: str = ""
    visibility: str = "customer"
    default_state: bool = False
    environment_scope: str = "all"
    customer_allowlist: str = ""
    customer_denylist: str = ""
    user_allowlist: str = ""
    role_requirements: str = ""
    license_plan_requirements: str = ""
    rollout_percentage: int = 0
    lifecycle_stage: str = "development"
    start_date: str | None = None
    expiry_date: str | None = None
    reason: str = ""


class FeatureFlagPatch(BaseModel):
    name: str | None = None
    description: str | None = None
    module: str | None = None
    visibility: str | None = None
    default_state: bool | None = None
    environment_scope: str | None = None
    customer_allowlist: str | None = None
    customer_denylist: str | None = None
    user_allowlist: str | None = None
    role_requirements: str | None = None
    license_plan_requirements: str | None = None
    rollout_percentage: int | None = None
    lifecycle_stage: str | None = None
    start_date: str | None = None
    expiry_date: str | None = None
    status: str | None = None
    reason: str = ""


class FeatureCheckIn(BaseModel):
    feature_key: str
    erp_product_id: str | None = None
    actor_type: str = "customer"
    environment: str = "production"
    customer_ref: str | None = None
    user_id: str | None = None
    role: str | None = None
    license_plan: str | None = None
    support_session_ref: str | None = None


def _find_flag(db, key, erp_product_id):
    q = db.query(models.FeatureFlag).filter_by(key=key)
    return (q.filter(models.FeatureFlag.erp_product_id == erp_product_id).first()
            or q.filter(models.FeatureFlag.erp_product_id.is_(None)).first())


# ------------------------- owner-only management (/api/platform/*) -------------------------
@app.get("/api/platform/products/{pid}/feature-flags")
def list_feature_flags(pid: str, q: str = "", visibility: str = "",
                       db: Session = Depends(get_db), op=Depends(require_internal)):
    if not db.get(models.ErpProduct, pid):
        raise HTTPException(404, "ERP product not found")
    rows = (db.query(models.FeatureFlag)
            .filter((models.FeatureFlag.erp_product_id == pid) | (models.FeatureFlag.erp_product_id.is_(None)))
            .order_by(models.FeatureFlag.id.desc()).all())
    out = [_flag(f) for f in rows]
    if q:
        ql = q.lower()
        out = [f for f in out if ql in (f["key"] or "").lower() or ql in (f["name"] or "").lower()]
    if visibility:
        out = [f for f in out if f["visibility"] == visibility]
    return out


@app.get("/api/platform/feature-flags/{fid}")
def get_feature_flag(fid: int, db: Session = Depends(get_db), op=Depends(require_internal)):
    f = db.get(models.FeatureFlag, fid)
    if not f:
        raise HTTPException(404, "Feature flag not found")
    return _flag(f)


@app.post("/api/platform/feature-flags", status_code=201)
def create_feature_flag(body: FeatureFlagIn, db: Session = Depends(get_db), op=Depends(require_internal)):
    if body.visibility not in VISIBILITY_LEVELS:
        raise HTTPException(422, f"visibility must be one of {sorted(VISIBILITY_LEVELS)}")
    if body.lifecycle_stage not in LIFECYCLE_STAGES:
        raise HTTPException(422, f"lifecycle_stage must be one of {LIFECYCLE_STAGES}")
    if body.erp_product_id and not db.get(models.ErpProduct, body.erp_product_id):
        raise HTTPException(404, "ERP product not found")
    if not (0 <= body.rollout_percentage <= 100):
        raise HTTPException(422, "rollout_percentage must be 0..100")
    if _find_flag(db, body.key, body.erp_product_id):
        raise HTTPException(409, "A feature flag with this key already exists for this scope")
    f = models.FeatureFlag(
        key=body.key, name=body.name, description=body.description, erp_product_id=body.erp_product_id,
        module=body.module, visibility=body.visibility, default_state=body.default_state,
        environment_scope=body.environment_scope or "all", customer_allowlist=body.customer_allowlist,
        customer_denylist=body.customer_denylist, user_allowlist=body.user_allowlist,
        role_requirements=body.role_requirements, license_plan_requirements=body.license_plan_requirements,
        rollout_percentage=body.rollout_percentage, lifecycle_stage=body.lifecycle_stage,
        start_date=_parse_dt(body.start_date),
        expiry_date=_parse_dt(body.expiry_date), status="active", created_by=op.id, updated_by=op.id)
    db.add(f)
    db.commit()
    _log_flag_audit(db, op, f, "feature_created", None, _flag_snapshot(f), body.reason)
    return {"ok": True, "id": f.id, "feature": _flag(f)}


@app.patch("/api/platform/feature-flags/{fid}")
def update_feature_flag(fid: int, body: FeatureFlagPatch,
                        db: Session = Depends(get_db), op=Depends(require_internal)):
    f = db.get(models.FeatureFlag, fid)
    if not f:
        raise HTTPException(404, "Feature flag not found")
    before = _flag_snapshot(f)
    for fld in ("name", "description", "module", "environment_scope", "customer_allowlist",
                "customer_denylist", "user_allowlist", "role_requirements",
                "license_plan_requirements", "default_state", "status"):
        v = getattr(body, fld)
        if v is not None:
            setattr(f, fld, v)
    if body.visibility is not None:
        if body.visibility not in VISIBILITY_LEVELS:
            raise HTTPException(422, f"visibility must be one of {sorted(VISIBILITY_LEVELS)}")
        f.visibility = body.visibility
    if body.rollout_percentage is not None:
        if not (0 <= body.rollout_percentage <= 100):
            raise HTTPException(422, "rollout_percentage must be 0..100")
        f.rollout_percentage = body.rollout_percentage
    f.version = (getattr(f, "version", None) or 1) + 1   # keep optimistic-concurrency token current
    if body.lifecycle_stage is not None:
        if body.lifecycle_stage not in LIFECYCLE_STAGES:
            raise HTTPException(422, f"lifecycle_stage must be one of {LIFECYCLE_STAGES}")
        f.lifecycle_stage = body.lifecycle_stage
    if body.start_date is not None:
        f.start_date = _parse_dt(body.start_date)
    if body.expiry_date is not None:
        f.expiry_date = _parse_dt(body.expiry_date)
    f.updated_by = op.id
    db.commit()
    after = _flag_snapshot(f)
    action = "feature_updated"
    if before.get("status") != after.get("status"):
        action = "feature_archived" if after.get("status") == "archived" else "feature_restored"
    elif before.get("lifecycle_stage") != after.get("lifecycle_stage"):
        bi = LIFECYCLE_STAGES.index(before.get("lifecycle_stage")) if before.get("lifecycle_stage") in LIFECYCLE_STAGES else 0
        ai = LIFECYCLE_STAGES.index(after.get("lifecycle_stage")) if after.get("lifecycle_stage") in LIFECYCLE_STAGES else 0
        action = "feature_promoted" if ai >= bi else "feature_demoted"
    elif before.get("customer_allowlist") != after.get("customer_allowlist"):
        action = "allowlist_changed"
    elif (before.get("default_state") != after.get("default_state")
          or (before.get("rollout_percentage") or 0) != (after.get("rollout_percentage") or 0)):
        widened = after.get("default_state") or (after.get("rollout_percentage") or 0) > (before.get("rollout_percentage") or 0)
        action = "feature_enabled" if widened else "feature_disabled"
    _log_flag_audit(db, op, f, action, before, after, body.reason)
    return {"ok": True, "feature": _flag(f)}


@app.get("/api/platform/feature-flags/{fid}/audit")
def feature_flag_audit(fid: int, db: Session = Depends(get_db), op=Depends(require_internal)):
    if not db.get(models.FeatureFlag, fid):
        raise HTTPException(404, "Feature flag not found")
    rows = (db.query(models.FeatureFlagAudit).filter_by(feature_flag_id=fid)
            .order_by(models.FeatureFlagAudit.id.desc()).limit(200).all())
    return [_flag_audit_row(a) for a in rows]


# ------------------------- evaluation contract (/api/internal/*) -------------------------
@app.post("/api/internal/feature-check")
def feature_check(body: FeatureCheckIn, db: Session = Depends(get_db), op=Depends(require_internal)):
    """Deny-by-default evaluation an ERP calls to decide whether a context may use a feature.

    Milestone 1: guarded by the internal-operator gate (testable). The future SmokeStack
    integration swaps this for a per-ERP signed service token (see integration contract).
    """
    feat = _find_flag(db, body.feature_key, body.erp_product_id)
    ctx = body.model_dump()
    # HARD RULE: customers always stay in Production. A non-elevated actor can never evaluate
    # against development/staging, no matter what environment the client requests.
    if ctx.get("actor_type") not in _ELEVATED_ACTORS:
        ctx["environment"] = "production"
    allow, reason = evaluate_feature(db, feat, ctx)
    if feat is not None:
        vis = feat.visibility
        if allow and vis in ("platform_owner_only", "internal_team"):
            _log_flag_audit(db, op, feat, "owner_tool_opened", None, None, reason,
                            actor_type=body.actor_type, customer=body.customer_ref, environment=body.environment)
        elif allow and vis == "experimental":
            _log_flag_audit(db, op, feat, "experimental_accessed", None, None, reason,
                            actor_type=body.actor_type, customer=body.customer_ref, environment=body.environment)
        elif (not allow and body.actor_type == "customer"
              and vis in ("platform_owner_only", "internal_team", "disabled")):
            _log_flag_audit(db, op, feat, "unauthorized_access_denied", None, None, reason,
                            actor_type=body.actor_type, customer=body.customer_ref, environment=body.environment)
    return {"allow": allow, "reason": reason,
            "visibility": (feat.visibility if feat else None),
            "feature_key": body.feature_key, "erp_product_id": body.erp_product_id}


# ==========================================================================================
#            INTERNAL DEVELOPMENT PLATFORM — Developer Mode (Platform Owner only)
# ==========================================================================================
# Every endpoint here is gated by `require_owner` → HTTP 403 for anyone who is not the
# authenticated Platform Owner, and 401 for a caller with no operator token (i.e. any
# customer). Developer Mode is validated server-side; the frontend never grants it.

DEV_TOOLS = [
    {"key": "feature_manager", "name": "Feature Manager", "icon": "flag", "status": "live",
     "desc": "Create, target, schedule, roll out and roll back feature flags."},
    {"key": "beta_features", "name": "Beta Features", "icon": "beaker", "status": "live",
     "desc": "Flags in pilot / internal-testing stages."},
    {"key": "experimental_modules", "name": "Experimental Modules", "icon": "beaker", "status": "live",
     "desc": "Experimental-visibility flags, hidden from customers by default."},
    {"key": "debug_console", "name": "Debug Console", "icon": "list", "status": "partial",
     "desc": "Recent platform audit / evaluation decisions. ERP-side request tracing is deferred."},
    {"key": "api_explorer", "name": "API Explorer", "icon": "activity", "status": "live",
     "desc": "Browse the Control-Center API surface (owner-only)."},
    {"key": "db_inspector", "name": "Database Inspector", "icon": "box", "status": "live",
     "desc": "Metadata only — table names and row counts. Never customer row data."},
    {"key": "system_diagnostics", "name": "System Diagnostics", "icon": "heart", "status": "live",
     "desc": "Service health, database check, fleet counts, versions."},
    {"key": "integration_status", "name": "Integration Status", "icon": "shield", "status": "partial",
     "desc": "State of ERP→PFS integration points (feature-check adoption is pending)."},
    {"key": "background_jobs", "name": "Background Jobs", "icon": "refresh", "status": "partial",
     "desc": "Platform jobs (e.g. fleet health polling). ERP job feeds are deferred."},
    {"key": "ai_playground", "name": "AI Playground", "icon": "rocket", "status": "integration_pending",
     "desc": "Prompt/scratch space for future ERP AI features."},
    {"key": "support_tools", "name": "Support Tools", "icon": "shield", "status": "partial",
     "desc": "Support sessions and owner overrides for the selected ERP."},
    {"key": "rollout_manager", "name": "Rollout Manager", "icon": "upload", "status": "live",
     "desc": "Percentage rollouts and gradual exposure."},
    {"key": "release_manager", "name": "Release Manager", "icon": "tag", "status": "live",
     "desc": "Advance features through the lifecycle pipeline."},
]


def _preview(p):
    return {"id": p.id, "operator_id": p.operator_id, "erp_product_id": p.erp_product_id,
            "customer_ref": p.customer_ref, "customer_name": p.customer_name,
            "environment": p.environment, "feature_profile": p.feature_profile,
            "status": p.status, "started_at": _iso(p.started_at), "ended_at": _iso(p.ended_at)}


@app.get("/api/platform/dev/context")
def dev_context(pid: str = "", db: Session = Depends(get_db), op=Depends(require_owner)):
    """Server-validated Developer-Mode context. A customer never reaches this (401/403)."""
    active = None
    if pid:
        active = (db.query(models.DevPreviewSession)
                  .filter_by(operator_id=op.id, erp_product_id=pid, status="active")
                  .order_by(models.DevPreviewSession.id.desc()).first())
    return {"developer_mode": True, "operator": {"id": op.id, "name": op.name, "role": op.platform_role},
            "feature_profile": "platform_owner", "environments": list(ENVIRONMENTS),
            "lifecycle_stages": LIFECYCLE_STAGES, "tools": DEV_TOOLS,
            "active_preview": (_preview(active) if active else None)}


class PreviewIn(BaseModel):
    erp_product_id: str
    customer_ref: str | None = None
    customer_name: str | None = None
    environment: str = "development"


@app.post("/api/platform/preview/start", status_code=201)
def preview_start(body: PreviewIn, db: Session = Depends(get_db), op=Depends(require_owner)):
    if not db.get(models.ErpProduct, body.erp_product_id):
        raise HTTPException(404, "ERP product not found")
    if body.environment not in ENVIRONMENTS:
        raise HTTPException(422, f"environment must be one of {sorted(ENVIRONMENTS)}")
    # end any prior active preview for this owner+ERP (single active preview per ERP)
    for old in (db.query(models.DevPreviewSession)
                .filter_by(operator_id=op.id, erp_product_id=body.erp_product_id, status="active").all()):
        old.status = "ended"
        old.ended_at = datetime.datetime.utcnow()
    name = body.customer_name
    if not name and body.customer_ref:
        c = (db.query(models.CustomerRef)
             .filter_by(erp_product_id=body.erp_product_id, external_ref=body.customer_ref).first())
        name = c.name if c else None
    p = models.DevPreviewSession(operator_id=op.id, erp_product_id=body.erp_product_id,
                                 customer_ref=body.customer_ref, customer_name=name,
                                 environment=body.environment, feature_profile="platform_owner",
                                 status="active")
    db.add(p)
    db.commit()
    audit(db, op, "preview_started", "dev_preview", p.id,
          f"{body.erp_product_id}/env:{body.environment}/customer:{body.customer_ref or '—'}")
    return {"ok": True, "preview": _preview(p)}


@app.post("/api/platform/preview/{pv}/environment")
def preview_switch_env(pv: int, environment: str, db: Session = Depends(get_db), op=Depends(require_owner)):
    """Instantly switch the owner's preview environment. Customers are unaffected (they never
    have a preview and are always evaluated in Production)."""
    p = db.get(models.DevPreviewSession, pv)
    if not p or p.operator_id != op.id:
        raise HTTPException(404, "Preview session not found")
    if environment not in ENVIRONMENTS:
        raise HTTPException(422, f"environment must be one of {sorted(ENVIRONMENTS)}")
    p.environment = environment
    db.commit()
    audit(db, op, "preview_environment_switched", "dev_preview", p.id, environment)
    return {"ok": True, "preview": _preview(p)}


@app.post("/api/platform/preview/{pv}/end")
def preview_end(pv: int, db: Session = Depends(get_db), op=Depends(require_owner)):
    p = db.get(models.DevPreviewSession, pv)
    if not p or p.operator_id != op.id:
        raise HTTPException(404, "Preview session not found")
    if p.status != "ended":
        p.status = "ended"
        p.ended_at = datetime.datetime.utcnow()
        db.commit()
        audit(db, op, "preview_ended", "dev_preview", p.id, p.erp_product_id)
    return {"ok": True, "preview": _preview(p)}


# ------------------------- release pipeline (owner-only) -------------------------
def _restore_flag(f, snap):
    lst = ("customer_allowlist", "customer_denylist", "user_allowlist",
           "role_requirements", "license_plan_requirements")
    for k, v in snap.items():
        if k in ("start_date", "expiry_date"):
            setattr(f, k, _parse_dt(v) if v else None)
        elif k in lst:
            setattr(f, k, ",".join(v) if isinstance(v, list) else (v or ""))
        elif hasattr(f, k):
            setattr(f, k, v)


class StageIn(BaseModel):
    lifecycle_stage: str
    reason: str = ""


@app.post("/api/platform/feature-flags/{fid}/stage")
def set_stage(fid: int, body: StageIn, db: Session = Depends(get_db), op=Depends(require_owner)):
    f = db.get(models.FeatureFlag, fid)
    if not f:
        raise HTTPException(404, "Feature flag not found")
    if body.lifecycle_stage not in LIFECYCLE_STAGES:
        raise HTTPException(422, f"lifecycle_stage must be one of {LIFECYCLE_STAGES}")
    before = _flag_snapshot(f)
    cur = LIFECYCLE_STAGES.index(f.lifecycle_stage) if f.lifecycle_stage in LIFECYCLE_STAGES else 0
    nxt = LIFECYCLE_STAGES.index(body.lifecycle_stage)
    f.lifecycle_stage = body.lifecycle_stage
    f.updated_by = op.id
    db.commit()
    _log_flag_audit(db, op, f, "feature_promoted" if nxt >= cur else "feature_demoted",
                    before, _flag_snapshot(f), body.reason)
    return {"ok": True, "feature": _flag(f)}


@app.post("/api/platform/feature-flags/{fid}/rollback")
def rollback_flag(fid: int, db: Session = Depends(get_db), op=Depends(require_owner)):
    """Instant rollback: restore the flag to the state BEFORE its most recent change."""
    f = db.get(models.FeatureFlag, fid)
    if not f:
        raise HTTPException(404, "Feature flag not found")
    last = (db.query(models.FeatureFlagAudit)
            .filter(models.FeatureFlagAudit.feature_flag_id == fid,
                    models.FeatureFlagAudit.before_state.isnot(None))
            .order_by(models.FeatureFlagAudit.id.desc()).first())
    if not last:
        raise HTTPException(409, "Nothing to roll back — no prior state recorded")
    before_now = _flag_snapshot(f)
    _restore_flag(f, json.loads(last.before_state))
    f.updated_by = op.id
    db.commit()
    _log_flag_audit(db, op, f, "feature_rolledback", before_now, _flag_snapshot(f),
                    f"rolled back over audit #{last.id} ({last.action})")
    return {"ok": True, "feature": _flag(f), "rolled_back_over": last.action}


# ------------------------- developer tools: metadata only (owner-only) -------------------------
@app.get("/api/platform/dev/diagnostics")
def dev_diagnostics(pid: str = "", db: Session = Depends(get_db), op=Depends(require_owner)):
    try:
        db.execute(sa.text("SELECT 1"))
        dbok = "ok"
    except Exception:
        dbok = "error"
    counts = {t: db.query(m).count() for t, m in (
        ("erp_products", models.ErpProduct), ("customers", models.CustomerRef),
        ("runtimes", models.Runtime), ("releases", models.Release),
        ("feature_flags", models.FeatureFlag), ("support_sessions", models.SupportSession))}
    rt = None
    if pid:
        rt = [{"name": r.name, "tier": r.tier, "health": r.last_health_state,
               "last_health_at": _iso(r.last_health_at)}
              for r in db.query(models.Runtime).filter_by(erp_product_id=pid).all()]
    return {"service": "pfs-control-center", "version": app.version, "database": dbok,
            "backend": engine.url.get_backend_name(), "counts": counts, "runtimes": rt,
            "checked_at": _iso(datetime.datetime.utcnow())}


@app.get("/api/platform/dev/schema")
def dev_schema(db: Session = Depends(get_db), op=Depends(require_owner)):
    """Database Inspector — METADATA ONLY. Table names, column names, and row counts.
    It deliberately never returns row data (no SELECT * of any table)."""
    insp = sa.inspect(db.get_bind())
    out = []
    for t in sorted(insp.get_table_names()):
        try:
            n = db.execute(sa.text(f"SELECT COUNT(*) FROM {t}")).scalar()
        except Exception:
            n = None
        cols = [c["name"] for c in insp.get_columns(t)]
        out.append({"table": t, "rows": n, "columns": cols})
    return {"tables": out, "note": "metadata only — no row data is exposed"}


@app.get("/api/platform/dev/routes")
def dev_routes(op=Depends(require_owner)):
    """API Explorer — enumerate the Control-Center route surface (owner-only)."""
    rows = []
    for r in app.routes:
        methods = sorted((getattr(r, "methods", None) or set()) - {"HEAD", "OPTIONS"})
        path = getattr(r, "path", "")
        if not path.startswith("/api") and path not in ("/health", "/auth/login"):
            continue
        ns = ("internal" if path.startswith("/api/internal") else
              "developer" if path.startswith("/api/platform/dev") or "/preview" in path else
              "platform" if path.startswith("/api/platform") else "app")
        rows.append({"path": path, "methods": methods, "namespace": ns,
                     "name": getattr(r, "name", "")})
    rows.sort(key=lambda x: x["path"])
    return {"routes": rows, "count": len(rows)}


@app.get("/api/platform/dev/integrations")
def dev_integrations(pid: str = "", db: Session = Depends(get_db), op=Depends(require_owner)):
    prod = db.get(models.ErpProduct, pid) if pid else None
    rt = (db.query(models.Runtime).filter_by(erp_product_id=pid, tier="customer").first()
          if pid else None)
    return {"integrations": [
        {"name": "Fleet health polling", "status": "active",
         "detail": (f"customer runtime: {rt.last_health_state}" if rt else "no customer runtime")},
        {"name": "Feature-check adoption (ERP→PFS)", "status": "pending",
         "detail": "SmokeStack does not call /api/internal/feature-check yet (Milestone 2)."},
        {"name": "Support-session consumption (ERP side)", "status": "pending",
         "detail": "Sessions are recorded and audited; ERP-side consumption is deferred."},
        {"name": "ERP data feed (users/branches/telemetry)", "status": "pending",
         "detail": "Populates workspace modules once the sanctioned feed is enabled."},
    ], "erp_product": (prod.id if prod else None)}


@app.get("/api/platform/dev/jobs")
def dev_jobs(db: Session = Depends(get_db), op=Depends(require_owner)):
    last = (db.query(models.PlatformAuditLog).filter_by(action="health_check_all")
            .order_by(models.PlatformAuditLog.id.desc()).first())
    return {"jobs": [
        {"name": "Fleet health poll", "kind": "on_demand", "status": "ready",
         "last_run": _iso(last.at) if last else None,
         "detail": "Triggered from the dashboard 'Run checks' action."},
        {"name": "Feature expiry sweep", "kind": "scheduled", "status": "pending",
         "last_run": None, "detail": "Auto-expiry worker is deferred; expiry is enforced at evaluation time."},
    ]}


@app.get("/api/platform/dev/debug")
def dev_debug(pid: str = "", limit: int = 40, db: Session = Depends(get_db), op=Depends(require_owner)):
    """Debug Console — recent platform actions + feature evaluation decisions (metadata)."""
    plat = (db.query(models.PlatformAuditLog)
            .order_by(models.PlatformAuditLog.id.desc()).limit(limit).all())
    fq = db.query(models.FeatureFlagAudit)
    if pid:
        fq = fq.filter(models.FeatureFlagAudit.erp_product_id == pid)
    feval = fq.order_by(models.FeatureFlagAudit.id.desc()).limit(limit).all()
    return {"platform": [{"at": _iso(a.at), "actor": a.actor_operator_id, "action": a.action,
                          "target": f"{a.target_type}:{a.target_id}", "detail": a.detail,
                          "result": a.result} for a in plat],
            "feature_decisions": [{"at": _iso(a.at), "action": a.action, "feature": a.feature_key,
                                   "actor_type": a.actor_type, "customer": a.customer_ref,
                                   "environment": a.environment, "reason": a.reason} for a in feval]}


# ==========================================================================================
#          MISSION CONTROL FOUNDATION — Typed Command Pipeline (Milestone 1 slice)
# ==========================================================================================
# The single audited path for governed mutations. Authorization is the IAM PDP (deny-by-default);
# execution re-validates authoritative state (optimistic concurrency); every outcome is
# hash-chained into the platform audit. Existing endpoints keep working; this is additive.

@commands.register("feature_flag.set_state")
def _cmd_feature_flag_set_state(db, op, cmd):
    """Enable/disable a feature flag's default state — the first mutation routed through the
    governed pipeline. Re-reads the flag and enforces the caller's expected_version."""
    fid = (cmd.target or {}).get("flag_id")
    f = db.get(models.FeatureFlag, fid) if fid is not None else None
    if not f:
        raise commands.CommandError("feature_flag_not_found", 404)
    if cmd.expected_version is not None and (getattr(f, "version", None) or 1) != cmd.expected_version:
        raise commands.CommandError("version_conflict", 409)
    if "state" not in (cmd.params or {}):
        raise commands.CommandError("state_required", 422)
    before = _flag_snapshot(f)
    new_state = bool(cmd.params.get("state"))
    f.default_state = new_state
    f.version = (getattr(f, "version", None) or 1) + 1
    f.updated_by = op.id
    db.flush()
    _log_flag_audit(db, op, f, "feature_enabled" if new_state else "feature_disabled",
                    before, _flag_snapshot(f), cmd.justification or "set via command pipeline",
                    commit=False)
    return {"flag_id": f.id, "key": f.key, "default_state": f.default_state, "version": f.version}


class CommandIn(BaseModel):
    type: str
    target: dict = {}
    tenant_context: str | None = None
    environment: str | None = None
    params: dict = {}
    justification: str = ""
    idempotency_key: str
    expected_version: int | None = None
    correlation_id: str | None = None
    approval_policy: str = "none"
    approved_by: str | None = None
    blast_radius: str | None = None
    elevation_id: int | None = None


@app.post("/api/platform/commands")
def run_command(body: CommandIn, db: Session = Depends(get_db), op=Depends(current_operator)):
    """Execute one typed command through the governed pipeline. Authentication is required here;
    per-capability authorization is decided by the IAM PDP inside dispatch (deny-by-default)."""
    try:
        cmd = commands.Command(**body.model_dump())
        return commands.dispatch(db, op, cmd)
    except commands.CommandError as e:
        raise HTTPException(e.http, e.code)


@app.get("/api/platform/audit/verify")
def verify_audit_chain(db: Session = Depends(get_db), op=Depends(require_owner)):
    """Re-walk the tamper-evident audit chain and report integrity."""
    ok, detail = audit_chain.verify(db)
    return {"ok": ok, "detail": detail}


@app.get("/api/platform/commands")
def list_commands(limit: int = 50, db: Session = Depends(get_db), op=Depends(require_internal)):
    rows = (db.query(models.CommandLog)
            .order_by(models.CommandLog.id.desc()).limit(min(limit, 200)).all())
    return [{"id": c.id, "type": c.command_type, "operator": c.operator_id, "status": c.status,
             "blast_radius": c.blast_radius, "reason": c.reason, "correlation_id": c.correlation_id,
             "idempotency_key": c.idempotency_key, "at": _iso(c.requested_at)} for c in rows]


# ==========================================================================================
#                        MISSION CONTROL M2 — foundation depth (additive)
# ==========================================================================================

# ------------------------- approvals -------------------------
class ApprovalIn(BaseModel):
    subject_type: str = "command"
    subject_ref: str | None = None
    policy: str = "single"
    quorum_required: int = 1
    reason: str
    ttl_seconds: int = 3600


class DecisionIn(BaseModel):
    decision: str
    reason: str = ""


@app.post("/api/platform/approvals", status_code=201)
def create_approval(body: ApprovalIn, db: Session = Depends(get_db), op=Depends(require_internal)):
    try:
        req = approvals.create_request(db, op.id, subject_type=body.subject_type,
                                       subject_ref=body.subject_ref, policy=body.policy,
                                       quorum_required=body.quorum_required, reason=body.reason,
                                       ttl_seconds=body.ttl_seconds)
    except approvals.ApprovalError as e:
        raise HTTPException(e.http, e.code)
    audit_chain.append(db, actor_operator_id=op.id, action="approval.requested",
                       target_type="approval_request", target_id=req.id, detail=body.reason)
    return approvals.to_dict(req)


@app.post("/api/platform/approvals/{rid}/decide")
def decide_approval(rid: int, body: DecisionIn, db: Session = Depends(get_db),
                    op=Depends(require_internal)):
    try:
        req = approvals.decide(db, op.id, rid, body.decision, body.reason)
    except approvals.ApprovalError as e:
        raise HTTPException(e.http, e.code)
    audit_chain.append(db, actor_operator_id=op.id, action=f"approval.{body.decision}",
                       target_type="approval_request", target_id=rid, detail=body.reason)
    return approvals.to_dict(req)


@app.get("/api/platform/approvals")
def list_approvals(status: str = "", db: Session = Depends(get_db), op=Depends(require_internal)):
    q = db.query(models.ApprovalRequest)
    if status:
        q = q.filter(models.ApprovalRequest.status == status)
    return [approvals.to_dict(r) for r in q.order_by(models.ApprovalRequest.id.desc()).limit(100)]


# ------------------------- break-glass -------------------------
class BreakGlassIn(BaseModel):
    capability: str
    reason: str
    quorum: int = 2


class OfflineBGIn(BaseModel):
    capability: str
    reason: str
    credential: str


@app.post("/api/platform/breakglass/request", status_code=201)
def bg_request(body: BreakGlassIn, db: Session = Depends(get_db), op=Depends(require_internal)):
    try:
        g = breakglass.request(db, op.id, body.capability, body.reason, quorum=body.quorum)
    except breakglass.BreakGlassError as e:
        raise HTTPException(e.http, e.code)
    audit_chain.append(db, actor_operator_id=op.id, action="breakglass.requested",
                       target_type="elevation_grant", target_id=g.id, detail=body.reason,
                       result="high_severity")
    return breakglass.to_dict(g)


@app.post("/api/platform/breakglass/{gid}/approve")
def bg_approve(gid: int, body: DecisionIn, db: Session = Depends(get_db),
               op=Depends(require_internal)):
    try:
        g = breakglass.approve(db, op.id, gid, body.decision, body.reason)
    except (breakglass.BreakGlassError, approvals.ApprovalError) as e:
        raise HTTPException(e.http, e.code)
    audit_chain.append(db, actor_operator_id=op.id, action=f"breakglass.{body.decision}",
                       target_type="elevation_grant", target_id=gid, detail=body.reason,
                       result="high_severity")
    return breakglass.to_dict(g)


@app.post("/api/platform/breakglass/offline", status_code=201)
def bg_offline(body: OfflineBGIn, db: Session = Depends(get_db), op=Depends(require_owner)):
    try:
        g = breakglass.open_offline(db, op.id, body.capability, body.reason, body.credential)
    except breakglass.BreakGlassError as e:
        raise HTTPException(e.http, e.code)
    audit_chain.append(db, actor_operator_id=op.id, action="breakglass.offline_opened",
                       target_type="elevation_grant", target_id=g.id, detail=body.reason,
                       result="critical")
    return breakglass.to_dict(g)


@app.post("/api/platform/breakglass/{gid}/recording-failed")
def bg_recording_failed(gid: int, db: Session = Depends(get_db), op=Depends(require_internal)):
    g = breakglass.on_recording_failure(db, gid)
    audit_chain.append(db, actor_operator_id=op.id, action="breakglass.recording_failed_terminated",
                       target_type="elevation_grant", target_id=gid, result="high_severity")
    return breakglass.to_dict(g) if g else {"ok": False}


@app.get("/api/platform/breakglass")
def bg_list(db: Session = Depends(get_db), op=Depends(require_internal)):
    return [breakglass.to_dict(g) for g in
            db.query(models.ElevationGrant).order_by(models.ElevationGrant.id.desc()).limit(100)]


# ------------------------- operator sessions -------------------------
@app.get("/api/platform/sessions")
def list_sessions(db: Session = Depends(get_db), op=Depends(require_internal)):
    return [sessions.to_dict(s) for s in
            db.query(models.OperatorSession).order_by(models.OperatorSession.id.desc()).limit(100)]


@app.post("/api/platform/sessions/{sid}/revoke")
def revoke_session(sid: int, db: Session = Depends(get_db), op=Depends(require_owner)):
    s = sessions.revoke(db, sid)
    if not s:
        raise HTTPException(404, "session_not_found")
    audit_chain.append(db, actor_operator_id=op.id, action="session.revoked",
                       target_type="operator_session", target_id=sid)
    return sessions.to_dict(s)


@app.get("/api/platform/presence")
def presence(db: Session = Depends(get_db), op=Depends(require_internal)):
    return sessions.presence(db)


# ------------------------- event backbone / outbox -------------------------
@app.post("/api/platform/outbox/relay")
def outbox_relay(limit: int = 200, db: Session = Depends(get_db), op=Depends(require_owner)):
    return events.relay(db, limit=min(limit, 500))


@app.get("/api/platform/outbox/dead-letters")
def outbox_dead(db: Session = Depends(get_db), op=Depends(require_internal)):
    return [{"id": r.id, "event_type": r.event_type, "attempts": r.attempts,
             "last_error": r.last_error, "at": _iso(r.created_at)}
            for r in events.dead_letters(db)]


# ------------------------- CQRS read model -------------------------
@app.get("/api/platform/readmodels/command-feed")
def rm_feed(limit: int = 50, db: Session = Depends(get_db), op=Depends(require_internal)):
    rows = (db.query(models.RMCommandFeed)
            .order_by(models.RMCommandFeed.id.desc()).limit(min(limit, 200)).all())
    return {"freshness": readmodels.freshness(db),
            "items": [{"id": r.id, "command_type": r.command_type, "operator": r.operator_id,
                       "status": r.status, "blast_radius": r.blast_radius,
                       "at": _iso(r.occurred_at)} for r in rows]}


@app.get("/api/platform/readmodels/command-feed/freshness")
def rm_freshness(db: Session = Depends(get_db), op=Depends(require_internal)):
    return readmodels.freshness(db)


@app.post("/api/platform/readmodels/command-feed/rebuild")
def rm_rebuild(db: Session = Depends(get_db), op=Depends(require_owner)):
    res = readmodels.rebuild(db)
    ok, detail = readmodels.validate(db)
    audit_chain.append(db, actor_operator_id=op.id, action="readmodel.rebuilt",
                       target_type="read_model", target_id=readmodels.FEED, detail=detail)
    return {**res, "valid": ok, "validation": detail}


# ------------------------- BFF (delegated identity + resilient aggregation) -------------------------
@app.get("/api/bff/obo-token")
def bff_obo(db: Session = Depends(get_db), op=Depends(current_operator)):
    """Mint a short-lived on-behalf-of token carrying the operator's identity for downstream
    (data-plane) calls — never a service principal."""
    return {"obo_token": bff.issue_obo_token(op), "token_use": "delegation",
            "expires_in": bff.OBO_TTL_SECONDS}


@app.get("/api/bff/mission-control")
def bff_mission_control(db: Session = Depends(get_db), op=Depends(current_operator)):
    """Aggregated Mission-Control view. Each source is resilient (circuit-breaker + partial
    failure ⇒ degraded flag). All reads are the operator's own (delegated identity)."""
    view = bff.gather(op, {
        "command_feed": lambda: [
            {"id": r.id, "type": r.command_type, "status": r.status, "at": _iso(r.occurred_at)}
            for r in db.query(models.RMCommandFeed)
            .order_by(models.RMCommandFeed.id.desc()).limit(10).all()],
        "read_model_freshness": lambda: readmodels.freshness(db),
        "presence": lambda: sessions.presence(db),
        "open_approvals": lambda: db.query(models.ApprovalRequest)
        .filter_by(status="pending").count(),
        "dead_letters": lambda: len(events.dead_letters(db)),
    })
    return view


# ------------------------- streaming gateway -------------------------
@app.get("/api/stream/poll")
def stream_poll(since: int = 0, limit: int = 100, db: Session = Depends(get_db),
                op=Depends(current_operator)):
    """Polling fallback: permission-filtered events after `since` (Last-Event-ID recovery)."""
    return streaming.poll(db, op, since_id=since, limit=min(limit, 200))


@app.get("/api/stream")
def stream_sse(since: int = 0, batches: int = 1, op=Depends(current_operator)):
    """SSE live stream (permission-filtered, live-revocation aware). `batches` bounds the loop
    for finite clients/tests; omit for a long-lived stream."""
    from fastapi.responses import StreamingResponse

    # a fresh short-lived Session per batch (never hold one open across awaits)
    def _factory():
        return SessionLocal()

    # resolve the operator's jti from a fresh session context is not available here; the SSE
    # generator re-checks session validity per batch via the token's jti carried on `op`.
    gen = streaming.event_stream(_factory, op, getattr(op, "_jti", None),
                                 since_id=since, interval=0.05,
                                 max_batches=(None if batches <= 0 else batches))
    return StreamingResponse(gen, media_type="text/event-stream")


# ==========================================================================================
#              MISSION CONTROL M3 — Bulk-Operations Safety Engine (additive)
# ==========================================================================================

@commands.register("customer.set_status")
def _cmd_customer_set_status(db, op, cmd):
    """Per-target executor used by bulk jobs (and standalone). Metadata-only status change with
    optimistic concurrency; reversible (rollback re-applies the prior status)."""
    cid = (cmd.target or {}).get("customer_ref_id")
    c = db.get(models.CustomerRef, cid) if cid is not None else None
    if not c:
        raise commands.CommandError("customer_not_found", 404)
    if cmd.expected_version is not None and (getattr(c, "version", None) or 1) != cmd.expected_version:
        raise commands.CommandError("version_conflict", 409)
    new = (cmd.params or {}).get("status")
    if not new:
        raise commands.CommandError("status_required", 422)
    before = c.status
    c.status = new
    c.version = (getattr(c, "version", None) or 1) + 1
    db.flush()
    return {"customer_ref_id": c.id, "status": c.status, "version": c.version, "before": before}


class SegmentIn(BaseModel):
    name: str
    description: str = ""
    filters: dict = {}


class ChangePreviewIn(BaseModel):
    command_type: str
    filters: dict = {}
    params: dict = {}


class ChangeJobIn(BaseModel):
    name: str = ""
    command_type: str
    filters: dict = {}
    params: dict = {}
    reason: str
    rate_limit_per_tick: int = 50
    error_budget: float = 0.2


@app.post("/api/platform/segments", status_code=201)
def create_segment(body: SegmentIn, db: Session = Depends(get_db), op=Depends(require_internal)):
    import json as _j
    s = models.Segment(name=body.name, description=body.description,
                       filters=_j.dumps(body.filters or {}), created_by=op.id)
    db.add(s)
    db.commit()
    prev = changes.segment_preview(db, body.filters or {})
    return {"id": s.id, "name": s.name, "preview": prev}


@app.get("/api/platform/segments")
def list_segments(db: Session = Depends(get_db), op=Depends(require_internal)):
    import json as _j
    return [{"id": s.id, "name": s.name, "filters": _j.loads(s.filters or "{}"),
             "created_by": s.created_by} for s in
            db.query(models.Segment).order_by(models.Segment.id.desc()).limit(100)]


@app.post("/api/platform/segments/resolve")
def resolve_segment(body: dict, db: Session = Depends(get_db), op=Depends(require_internal)):
    return changes.segment_preview(db, body or {})


@app.post("/api/platform/change-jobs/preview")
def change_preview(body: ChangePreviewIn, db: Session = Depends(get_db), op=Depends(require_internal)):
    """Dry-run: full plan (targets, states, conflicts, blast radius, approval, rings, risk)."""
    return changes.preview(db, body.command_type, body.filters or {}, body.params or {})


@app.post("/api/platform/change-jobs", status_code=201)
def create_change_job(body: ChangeJobIn, db: Session = Depends(get_db), op=Depends(require_internal)):
    try:
        job = changes.create_job(db, op, name=body.name, command_type=body.command_type,
                                 filters=body.filters or {}, params=body.params or {},
                                 reason=body.reason, rate_limit_per_tick=body.rate_limit_per_tick,
                                 error_budget=body.error_budget)
    except changes.ChangeError as e:
        raise HTTPException(e.http, e.code)
    audit_chain.append(db, actor_operator_id=op.id, action="change_job.created",
                       target_type="change_job", target_id=job.id, detail=body.reason,
                       correlation_id=job.correlation_id)
    return changes.to_dict(job, db)


@app.get("/api/platform/change-jobs")
def list_change_jobs(status: str = "", db: Session = Depends(get_db), op=Depends(require_internal)):
    q = db.query(models.ChangeJob)
    if status:
        q = q.filter(models.ChangeJob.status == status)
    return [changes.to_dict(j, db) for j in q.order_by(models.ChangeJob.id.desc()).limit(100)]


@app.get("/api/platform/change-jobs/{jid}")
def get_change_job(jid: int, db: Session = Depends(get_db), op=Depends(require_internal)):
    job = db.get(models.ChangeJob, jid)
    if not job:
        raise HTTPException(404, "job_not_found")
    return changes.to_dict(job, db)


@app.get("/api/platform/change-jobs/{jid}/targets")
def change_job_targets(jid: int, db: Session = Depends(get_db), op=Depends(require_internal)):
    rows = db.query(models.ChangeTarget).filter_by(job_id=jid).order_by(models.ChangeTarget.id).all()
    return [{"id": t.id, "target_ref": t.target_ref, "ring": t.ring, "status": t.status,
             "attempts": t.attempts, "error": t.error, "expected_version": t.expected_version}
            for t in rows]


@app.post("/api/platform/change-jobs/{jid}/approve")
def approve_change_job(jid: int, body: DecisionIn, db: Session = Depends(get_db),
                       op=Depends(require_internal)):
    try:
        job = changes.approve_job(db, op.id, jid, body.decision, body.reason)
    except (changes.ChangeError, approvals.ApprovalError) as e:
        raise HTTPException(e.http, e.code)
    audit_chain.append(db, actor_operator_id=op.id, action=f"change_job.{body.decision}",
                       target_type="change_job", target_id=jid, detail=body.reason)
    return changes.to_dict(job, db)


@app.post("/api/platform/change-jobs/{jid}/execute")
def execute_change_job(jid: int, db: Session = Depends(get_db), op=Depends(require_owner)):
    """Advance the job by one bounded tick (canary-ring batch). Idempotent + resumable."""
    try:
        return changes.execute_tick(db, jid)
    except changes.ChangeError as e:
        raise HTTPException(e.http, e.code)


@app.post("/api/platform/change-jobs/{jid}/run")
def run_change_job(jid: int, max_ticks: int = 50, db: Session = Depends(get_db),
                   op=Depends(require_owner)):
    try:
        return changes.run(db, jid, max_ticks=min(max_ticks, 200))
    except changes.ChangeError as e:
        raise HTTPException(e.http, e.code)


@app.post("/api/platform/change-jobs/{jid}/pause")
def pause_change_job(jid: int, db: Session = Depends(get_db), op=Depends(require_internal)):
    job = changes.pause(db, jid)
    return changes.to_dict(job, db) if job else {"ok": False}


@app.post("/api/platform/change-jobs/{jid}/resume")
def resume_change_job(jid: int, db: Session = Depends(get_db), op=Depends(require_owner)):
    job = changes.resume(db, jid)
    return changes.to_dict(job, db) if job else {"ok": False}


@app.post("/api/platform/change-jobs/{jid}/abort")
def abort_change_job(jid: int, db: Session = Depends(get_db), op=Depends(require_owner)):
    job = changes.abort(db, jid)
    audit_chain.append(db, actor_operator_id=op.id, action="change_job.aborted",
                       target_type="change_job", target_id=jid)
    return changes.to_dict(job, db) if job else {"ok": False}


@app.post("/api/platform/change-jobs/{jid}/rollback")
def rollback_change_job(jid: int, db: Session = Depends(get_db), op=Depends(require_owner)):
    try:
        res = changes.rollback(db, jid)
    except changes.ChangeError as e:
        raise HTTPException(e.http, e.code)
    audit_chain.append(db, actor_operator_id=op.id, action="change_job.rolled_back",
                       target_type="change_job", target_id=jid, detail=str(res))
    return res


@app.get("/api/platform/change-jobs-overview")
def change_jobs_overview(db: Session = Depends(get_db), op=Depends(require_internal)):
    """Mission-Control roll-up of active/attention jobs."""
    out = {"running": [], "paused": [], "halted": [], "aborted": [], "rolled_back": [],
           "awaiting_approval": [], "critical": []}
    for j in (db.query(models.ChangeJob)
              .filter(models.ChangeJob.status.in_(list(changes.NON_TERMINAL) + ["aborted", "rolled_back"]))
              .order_by(models.ChangeJob.id.desc()).limit(200)):
        bucket = {"running": "running", "paused": "paused", "halted": "halted",
                  "aborted": "aborted", "rolled_back": "rolled_back",
                  "awaiting_approval": "awaiting_approval"}.get(j.status)
        item = {"id": j.id, "name": j.name, "command_type": j.command_type,
                "status": j.status, "blast_radius": j.blast_radius, "halt_reason": j.halt_reason}
        if bucket:
            out[bucket].append(item)
        if j.status == "halted" or j.blast_radius in ("cross_region", "fleet"):
            out["critical"].append(item)
    return out

"""PFS Control Center — Milestone 1 (Foundation).

Metadata-only Control Plane. Registers and displays the fleet; read-only toward ERP
runtimes (a single outbound GET to a health URL). Does NOT deploy, orchestrate, provision
Master runtimes, access customer transactional data, or consume Enter-ERP grants (deferred).
"""
import datetime
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

import models
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
    return {"access_token": create_token(op.id), "token_type": "bearer",
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


@app.post("/api/runtimes/{rid}/health-check")
def health_check(rid: int, db: Session = Depends(get_db), op=Depends(current_operator)):
    """Read-only outbound GET to a runtime's health URL; records the last-known state."""
    rt = db.get(models.Runtime, rid)
    if not rt:
        raise HTTPException(404, "Runtime not found")
    state, detail = "unknown", ""
    if rt.health_url:
        try:
            req = urllib.request.Request(rt.health_url, headers={"User-Agent": "pfs-control-center"})
            with urllib.request.urlopen(req, timeout=8) as resp:   # noqa: S310 (operator-registered URL)
                body = resp.read().decode("utf-8", "replace")[:500]
                code = resp.status
            j = json.loads(body) if body.strip().startswith("{") else {}
            state = "ok" if (code == 200 and j.get("status") == "ok") else "degraded"
            detail = body[:200]
        except Exception as e:   # noqa: BLE001
            state, detail = "unreachable", str(e)[:200]
    rt.last_health_state = state
    rt.last_health_at = datetime.datetime.utcnow()
    rt.last_health_detail = detail
    db.commit()
    audit(db, op, "health_check", "runtime", rt.id, state)
    return {"runtime": rt.id, "health": state, "detail": detail}


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
        active_lic = (db.query(models.License).filter_by(erp_product_id=p.id)
                      .filter(models.License.status.in_(["active", "trial"])).count())
        cur = None
        if prt and prt.current_release_id:
            rel = db.get(models.Release, prt.current_release_id)
            cur = rel.version if rel else None
        last_audit = (db.query(models.PlatformAuditLog)
                      .filter(models.PlatformAuditLog.target_id == p.id)
                      .order_by(models.PlatformAuditLog.id.desc()).first())
        cards.append({
            "id": p.id, "name": p.name, "description": p.description, "status": p.status,
            "customers": n_cust, "active_licenses": active_lic,
            "current_version": cur, "erp_health": (prt.last_health_state if prt else "unknown"),
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
                "sessions": [], "versions": []}

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
    return {"query": q, "products": products, "customers": customers,
            "licenses": licenses, "sessions": sessions, "versions": versions}

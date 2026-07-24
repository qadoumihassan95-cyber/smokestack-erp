"""PFS Control Center — Milestone 1 (Foundation).

Metadata-only Control Plane. Registers and displays the fleet; read-only toward ERP
runtimes (a single outbound GET to a health URL). Does NOT deploy, orchestrate, provision
Master runtimes, access customer transactional data, or consume Enter-ERP grants (deferred).
"""
import datetime
import json
import os
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
    return {"product": _product(p), "environments": envs, "runtimes": runtimes,
            "releases": releases, "customer_deployments": cdeps, "deployments": deps, "audit": audit}

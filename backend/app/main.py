"""SmokeStack ERP backend — FastAPI application entrypoint.
Prepared for Telegram + AI + mobile: pure JSON REST, JWT bearer auth, CORS,
OpenAPI docs at /docs. Creates tables + seeds on startup."""
import json
import logging

from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from .config import settings
from .database import Base, engine, SessionLocal
from . import tenancy  # installs generic company-isolation scoping on import

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("pfs.startup")
from .routers import (auth, inventory, ledger, hr, partners, workflow, core,
                      telegram, attendance, licenses, control, assistant, users, chat, schedule)

app = FastAPI(title="SmokeStack ERP API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins or ["*"],
    allow_credentials=True, allow_methods=["*"], allow_headers=["*"],
)

for r in (auth.router, core.router, inventory.router, ledger.router, hr.router,
          partners.router, workflow.router, telegram.router, attendance.router,
          licenses.router, control.router, assistant.router, users.router, chat.router,
          schedule.router):
    app.include_router(r)

# PFS Control Center — mounted as a fully decoupled sub-application (its own
# router, auth realm, permissions, service + data-access seam). This is the ONE
# touchpoint between the ERP and the Control Center: the ERP never imports PFS
# internals. Setting PFS_ENABLED=false removes it entirely; the same sub-app can
# later be served as its own service/domain with no refactor (see app/pfs).
from .pfs import mount_pfs
mount_pfs(app)

@app.get("/api/health")
def health():
    """Liveness + readiness: verifies the process, DATABASE connectivity, and the
    core application registry, and surfaces any quarantined plugins / failed
    bootstraps. Returns 503 (not a silent OK) when a hard dependency is down, so a
    broken deploy is never marked healthy."""
    checks = {}
    healthy = True

    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as e:  # noqa: BLE001
        checks["database"] = f"error: {e.__class__.__name__}"
        healthy = False

    try:
        from .apps import load_failures
        from .platform import registry
        from .platform.seed import bootstrap_failures
        checks["applications"] = len(registry.applications())
        if not registry.applications():
            checks["registry"] = "empty"      # core registry failed to load
            healthy = False
        pf = load_failures()
        bf = bootstrap_failures()
        if pf:
            checks["plugin_failures"] = pf     # quarantined apps (not fatal)
        if bf:
            checks["bootstrap_failures"] = bf
    except Exception as e:  # noqa: BLE001
        checks["registry"] = f"error: {e.__class__.__name__}"
        healthy = False

    body = {"status": "ok" if healthy else "degraded",
            "service": "smokestack-erp-api", "version": "1.0.0", "checks": checks}
    if not healthy:
        return Response(content=json.dumps(body), status_code=503,
                        media_type="application/json")
    return body

@app.on_event("startup")
def on_startup():
    # create_all + tenant/platform seed are the CRITICAL core path — a failure
    # here should surface (health goes 503) rather than loop silently.
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    # Startup seeding + platform bootstrap is deliberate privileged global
    # maintenance — mark the session as an explicit system context so it is an
    # audited bypass of tenant scoping, not an accidental untagged one.
    tenancy.use_system_context(db)
    try:
        if settings.seed_on_start:
            from .seed import seed
            seed(db)
        # Applications self-register (a broken OPTIONAL plugin is quarantined, not
        # fatal); then the business-agnostic platform seed upserts apps + modules
        # and runs each app's ISOLATED bootstrap. Idempotent + additive.
        from .apps import load_apps
        load_apps()
        from .platform.seed import seed_platform
        seed_platform(db)
    except Exception:  # noqa: BLE001
        log.exception("startup seeding failed")
        raise
    finally:
        db.close()

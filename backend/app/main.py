"""SmokeStack ERP backend — FastAPI application entrypoint.
Prepared for Telegram + AI + mobile: pure JSON REST, JWT bearer auth, CORS,
OpenAPI docs at /docs. Creates tables + seeds on startup."""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from .config import settings
from .database import Base, engine, SessionLocal
from . import tenancy  # installs generic company-isolation scoping on import
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
    return {"status": "ok", "service": "smokestack-erp-api", "version": "1.0.0"}

@app.on_event("startup")
def on_startup():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        if settings.seed_on_start:
            from .seed import seed
            seed(db)
        # PFS Platform: applications self-register, then the (business-agnostic)
        # platform seed upserts apps + modules and runs each app's bootstrap.
        # Idempotent and additive — safe on every boot, tenant data untouched.
        from .apps import load_apps
        load_apps()
        from .platform.seed import seed_platform
        seed_platform(db)
    finally:
        db.close()

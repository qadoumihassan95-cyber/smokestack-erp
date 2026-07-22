"""SmokeStack ERP backend — FastAPI application entrypoint.
Prepared for Telegram + AI + mobile: pure JSON REST, JWT bearer auth, CORS,
OpenAPI docs at /docs. Creates tables + seeds on startup."""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from .config import settings
from .database import Base, engine, SessionLocal
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

@app.get("/api/health")
def health():
    return {"status": "ok", "service": "smokestack-erp-api", "version": "1.0.0"}

@app.on_event("startup")
def on_startup():
    Base.metadata.create_all(bind=engine)
    if settings.seed_on_start:
        from .seed import seed
        db = SessionLocal()
        try:
            seed(db)
        finally:
            db.close()

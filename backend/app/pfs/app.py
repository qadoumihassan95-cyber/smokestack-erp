"""Builds the Control Center as a STANDALONE FastAPI application.

Co-hosted, this object is mounted onto the ERP app; extracted, the exact same
object is served at root by its own entrypoint — no code change, just a
different launcher. It carries its own title, docs, OpenAPI schema and CORS, so
nothing about it depends on the ERP being present.
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .router import router
from .seed import bootstrap_super_admin
from .db import SessionLocal


def create_pfs_app() -> FastAPI:
    app = FastAPI(title="PFS Control Center", version="1.0.0",
                  description="Super-admin platform for PFS. Decoupled from any "
                              "ERP application; runs co-hosted or standalone.")
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_credentials=True,
        allow_methods=["*"], allow_headers=["*"],
    )
    app.include_router(router)

    @app.on_event("startup")
    def _startup():
        # Fires when the Control Center runs as its own service. When mounted,
        # the parent invokes the same bootstrap (mounted sub-app lifespans are
        # not reliably propagated by Starlette). Idempotent either way.
        db = SessionLocal()
        try:
            bootstrap_super_admin(db)
        finally:
            db.close()

    return app

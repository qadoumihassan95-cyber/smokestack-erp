"""Standalone entrypoint for running the Control Center as its OWN service.

Today the Control Center is mounted onto the ERP app. When the deployment model
evolves — its own Render service or its own domain (pfs.mydomain.com) — point a
new service's start command at this module and nothing else changes:

    uvicorn app.pfs.standalone:app --host 0.0.0.0 --port $PORT

Because the app self-seeds its first Super Admin from PFS_ROOT_USER /
PFS_ROOT_PASSWORD and reaches data through PFS_DATABASE_URL (or the shared DB),
extraction is configuration, not code.
"""
from .app import create_pfs_app

app = create_pfs_app()

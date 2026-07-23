"""PFS Control Center — DECOUPLING guarantees.

The Control Center must be able to move to its own service / domain later with no
major refactor. These tests enforce, in CI, the boundary that makes that
possible:

  1. The ERP never imports anything from app/pfs (except the composition root,
     main.py, which is expected to wire it).
  2. app/pfs never imports ERP internals (routers, security, permissions, seed,
     apps, business modules); it may share ONLY database + models (the shared
     schema) — nothing else.
  3. The Control Center is a SEPARATE application: its own OpenAPI, its own auth
     realm. An ERP token is rejected by the Control Center. The tenant OpenAPI
     never exposes the platform surface.
  4. It is mounted and functional: health is public, everything else is gated,
     and a seeded Super Admin can log in and read /auth/me.
"""
import ast
import os
import tempfile

_DB = os.path.join(tempfile.gettempdir(), f"smokestack_pfs_dc_{os.getpid()}.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["SEED_ON_START"] = "true"
os.environ["JWT_SECRET"] = "erp-secret-long-enough-for-tests"
os.environ["PFS_JWT_SECRET"] = "pfs-secret-DIFFERENT-from-erp"
os.environ["TELEGRAM_BOT_TOKEN"] = "test-bot-token"

from fastapi.testclient import TestClient

from app.main import app
from app.database import SessionLocal
from app.pfs.config import pfs_config
from app.pfs import security as pfs_security
from app.pfs.repository import PlatformRepository

_APP_DIR = os.path.join(os.path.dirname(__file__), "..", "app")

# Enter the app lifespan once so startup runs (create_all + tenant seed + the
# PFS self-seed), then reuse a plain client against the populated DB.
with TestClient(app):
    pass
client = TestClient(app)


# ------------------------------------------------------------------ helpers
def _imports_of(path):
    """Return the set of module strings imported by a python file (both
    `import x` and `from x import y`, preserving relative dots)."""
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            out.append(("." * (node.level or 0)) + (node.module or ""))
    return out


def _py_files(*rel_dirs):
    for rel in rel_dirs:
        d = os.path.join(_APP_DIR, *rel.split("/")) if rel else _APP_DIR
        if not os.path.isdir(d):
            continue
        for root, _dirs, files in os.walk(d):
            if "__pycache__" in root:
                continue
            for fn in files:
                if fn.endswith(".py"):
                    yield os.path.join(root, fn)


# --------------------------------------------------- (1) ERP never imports PFS
def test_erp_code_never_imports_the_control_center():
    offenders = []
    for path in _py_files("routers", "platform", "apps"):
        for mod in _imports_of(path):
            if "pfs" in mod.split("."):
                offenders.append(os.path.relpath(path, _APP_DIR))
    # top-level app/*.py too, except main.py (the composition root)
    for fn in os.listdir(_APP_DIR):
        if not fn.endswith(".py") or fn == "main.py":
            continue
        for mod in _imports_of(os.path.join(_APP_DIR, fn)):
            if "pfs" in mod.split("."):
                offenders.append(fn)
    assert not offenders, "ERP must not import the Control Center: " + ", ".join(sorted(set(offenders)))


def test_only_main_wires_the_control_center():
    # main.py is allowed (and expected) to import the mount helper — that is the
    # single composition touchpoint.
    mods = _imports_of(os.path.join(_APP_DIR, "main.py"))
    assert any("pfs" in m.split(".") for m in mods), "main.py should mount PFS"


# --------------------------------- (2) PFS never imports ERP internals
def test_control_center_never_imports_erp_internals():
    # Allowed shared-infrastructure parent imports (relative, from within app/pfs).
    allowed_parent = {"..database", "..models", ".."}
    forbidden_hits = []
    for path in _py_files("pfs"):
        for mod in _imports_of(path):
            if not mod.startswith(".."):
                continue  # only reaching OUT of the pfs package matters
            base = mod.split(".import")[0]
            # normalise "..models" style; anything reaching a sibling ERP module
            # that is not database/models is forbidden.
            if base not in allowed_parent and not base.startswith("..database") \
               and not base.startswith("..models"):
                forbidden_hits.append(f"{os.path.relpath(path, _APP_DIR)} -> {mod}")
    assert not forbidden_hits, ("Control Center may only share database+models with "
                                "the ERP; found: " + ", ".join(forbidden_hits))


def test_control_center_does_not_import_erp_security_or_permissions_by_name():
    # Even absolute imports of the ERP's security/permissions/routers are banned.
    banned = ("app.security", "app.permissions", "app.routers", "app.seed", "app.apps")
    hits = []
    for path in _py_files("pfs"):
        for mod in _imports_of(path):
            if mod.startswith(banned):
                hits.append(f"{os.path.relpath(path, _APP_DIR)} -> {mod}")
    assert not hits, "Control Center imported ERP internals: " + ", ".join(hits)


# ----------------------------------------- (3) separate app + separate realm
def test_control_center_has_its_own_openapi_and_is_not_in_the_tenant_api():
    # tenant OpenAPI must not expose the platform surface
    tenant_paths = client.get("/openapi.json").json()["paths"]
    assert not any(p.startswith("/pfs") for p in tenant_paths)
    assert not any(p.startswith("/api/pfs") for p in tenant_paths)
    # the Control Center serves its OWN OpenAPI (proves it's a separate app)
    own = client.get("/pfs/openapi.json")
    assert own.status_code == 200
    assert own.json().get("info", {}).get("title") == "PFS Control Center"


def test_erp_token_is_rejected_by_the_control_center():
    # a valid ERP tenant token must NOT authenticate against the Control Center
    r = client.post("/api/auth/login", data={"username": "U-owner", "password": "demo1234"})
    assert r.status_code == 200
    erp_token = r.json()["access_token"]
    denied = client.get("/pfs/auth/me", headers={"Authorization": "Bearer " + erp_token})
    assert denied.status_code == 401


# --------------------------------------------- (4) mounted + gated + working
def test_health_is_public_and_stamps_the_pfs_realm():
    r = client.get("/pfs/health")
    assert r.status_code == 200 and r.json()["realm"] == "pfs"


def test_control_center_endpoints_are_gated():
    assert client.get("/pfs/auth/me").status_code == 401
    assert client.get("/pfs/overview").status_code == 401


def test_super_admin_can_log_in_and_read_me():
    db = SessionLocal()
    try:
        repo = PlatformRepository(db)
        if not repo.get_super_admin_by_username("root-tester"):
            repo.create_super_admin(id="SA-test", username="root-tester",
                                    name="Root Tester",
                                    password_hash=pfs_security.hash_pw("s3cret-pass"))
    finally:
        db.close()
    r = client.post("/pfs/auth/login", data={"username": "root-tester", "password": "s3cret-pass"})
    assert r.status_code == 200, r.text
    tok = r.json()["access_token"]
    me = client.get("/pfs/auth/me", headers={"Authorization": "Bearer " + tok})
    assert me.status_code == 200
    body = me.json()
    assert body["username"] == "root-tester"
    assert "system.read" in body["capabilities"]
    # and this Control Center token must be meaningless to the ERP
    erp = client.get("/api/auth/me", headers={"Authorization": "Bearer " + tok})
    assert erp.status_code == 401


def test_realm_isolation_does_not_depend_on_secrets_matching():
    """Even if the ERP and Control Center shared a signing secret, a token minted
    without realm="pfs" must be rejected — isolation is enforced by the realm
    claim, so it holds regardless of secret configuration or test ordering."""
    from jose import jwt as jose_jwt
    from datetime import datetime, timedelta, timezone
    exp = datetime.now(timezone.utc) + timedelta(minutes=5)
    # a token signed with the SAME secret the Control Center uses, but no pfs realm
    forged = jose_jwt.encode({"sub": "SA-test", "role": "owner", "exp": exp},
                             pfs_config.jwt_secret, algorithm=pfs_config.jwt_alg)
    denied = client.get("/pfs/auth/me", headers={"Authorization": "Bearer " + forged})
    assert denied.status_code == 401
    # config still declares its own realm
    assert pfs_config.jwt_realm == "pfs"

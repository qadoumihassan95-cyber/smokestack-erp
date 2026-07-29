"""Branch display_name — display-only rename, keys and relations preserved.

Verifies: the /branches contract still returns internal keys; /branches/labels maps
keys -> business names; the seed backfills the three names; dashboard/kpi expose
branch_display; and a branch with a NULL display_name safely falls back to its key.
"""
import os
import tempfile

_DB = os.path.join(tempfile.gettempdir(), f"smokestack_bd_{os.getpid()}.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["SEED_ON_START"] = "true"
os.environ["JWT_SECRET"] = "test-secret"

from fastapi.testclient import TestClient
from app.main import app
from app import models
from app.database import SessionLocal

client = TestClient(app)
PW = "demo1234"

EXPECTED = {
    "Store A": "GM Tobacco Duncanville",
    "Store B": "GM Tobacco Lancaster",
    "Store C": "Smoke Depot Waco",
}


def tok(uid):
    r = client.post("/api/auth/login", data={"username": uid, "password": PW})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["access_token"]}


def test_branches_contract_unchanged_keys():
    with TestClient(app):
        keys = client.get("/api/branches", headers=tok("U-owner")).json()
        assert set(keys) == {"Store A", "Store B", "Store C"}  # keys/relations untouched


def test_labels_endpoint_maps_keys_to_business_names():
    with TestClient(app):
        labels = client.get("/api/branches/labels", headers=tok("U-owner")).json()
        assert labels == EXPECTED


def test_labels_are_permission_scoped():
    with TestClient(app):
        labels = client.get("/api/branches/labels", headers=tok("U-cash")).json()
        assert labels == {"Store A": "GM Tobacco Duncanville"}  # cashier sees only their branch


def test_seed_backfilled_display_name_in_db():
    with TestClient(app):
        db = SessionLocal()
        try:
            for key, name in EXPECTED.items():
                b = db.get(models.Branch, key)
                assert b is not None and b.name == key           # key preserved
                assert b.display_name == name                    # label applied
        finally:
            db.close()


def test_dashboard_and_kpi_expose_branch_display():
    with TestClient(app):
        h = tok("U-owner")
        d = client.get("/api/reports/dashboard?branch=Store A", headers=h).json()
        assert d["branch"] == "Store A" and d["branch_display"] == "GM Tobacco Duncanville"
        k = client.get("/api/reports/kpi?branch=Store C", headers=h).json()
        assert k["branch"] == "Store C" and k["branch_display"] == "Smoke Depot Waco"
        allb = client.get("/api/reports/dashboard?branch=all", headers=h).json()
        assert allb["branch_display"] == "All branches"


def test_missing_display_name_falls_back_to_key():
    # Verified at the helper level so no shared state is mutated (the suite shares one engine).
    from app import security as S
    with TestClient(app):
        db = SessionLocal()
        try:
            assert S.branch_label(db, "Ghost Branch") == "Ghost Branch"   # unknown key → itself
            b = db.get(models.Branch, "Store A")
            b.display_name = None
            db.flush()                                                    # in-session only
            assert S.branch_labels(db).get("Store A") == "Store A"        # NULL label → key
            db.rollback()                                                 # never persist the clear
        finally:
            db.close()


def test_assistant_sales_by_branch_sentence_uses_business_names():
    """The assistant's per-branch answer keeps internal keys in `branch`/`best`/`weakest`
    but exposes business names in `*_label`, and the composed sentence never leaks a key."""
    from app.assistant import tools as T, engine as E
    with TestClient(app):
        db = SessionLocal()
        try:
            user = db.get(models.User, "U-owner")
            data = T.run("sales.by_branch", db, user, period_kind="year")
            for row in data["rows"]:
                assert row["branch"] in EXPECTED                        # internal key preserved
                assert row["branch_label"] == EXPECTED[row["branch"]]   # label applied
            assert data["best"] in EXPECTED
            assert data["best_label"] == EXPECTED[data["best"]]
            sentence = E.summarise("sales.by_branch", data)
            assert "Store " not in sentence                             # no raw key in prose
            assert data["best_label"] in sentence                       # business name shown
        finally:
            db.close()


def test_assistant_global_search_subtitle_shows_business_name():
    """Search cards show the store's business name, not the internal key (seed: Sam Rivera @ Store A)."""
    from app.assistant import tools as T
    with TestClient(app):
        db = SessionLocal()
        try:
            user = db.get(models.User, "U-owner")
            data = T.run("search.global", db, user, q="Rivera")
            subtitles = [r["subtitle"] for g in data["groups"] for r in g["rows"]]
            joined = " | ".join(subtitles)
            assert "GM Tobacco Duncanville" in joined                   # label present
            assert "Store A" not in joined                              # key not leaked
        finally:
            db.close()

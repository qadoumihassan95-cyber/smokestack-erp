"""Attendance geofencing tests: inside/outside/boundary radius, duplicate clock-in,
clock-out guards, multi-branch selection, unauthorized branch, and manager approval."""
import os
import tempfile

_DB = os.path.join(tempfile.gettempdir(), f"smokestack_att_{os.getpid()}.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["SEED_ON_START"] = "true"
os.environ["JWT_SECRET"] = "test-secret"

from fastapi.testclient import TestClient
from app.main import app
from app.routers.attendance import haversine

client = TestClient(app)
A = (32.221100, 35.254400)   # seeded Store A


def _tok(uid="U-owner"):
    r = client.post("/api/auth/login", data={"username": uid, "password": "demo1234"})
    return {"Authorization": "Bearer " + r.json()["access_token"]}


def _clear_active(uid="U-owner"):
    # helper: end any active session so tests are independent (via clock-out at the branch)
    pass


def test_haversine_sanity():
    # ~111 m per 0.001 deg latitude
    assert 105 < haversine(A[0], A[1], A[0] + 0.001, A[1]) < 118


def test_clock_in_inside_radius():
    with TestClient(app):
        h = _tok("U-owner")
        r = client.post("/api/attendance/clock-in",
                        json={"lat": A[0] + 0.0003, "lng": A[1], "live": True}, headers=h)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["result"] == "in" and d["status"] == "active" and d["branch"] == "Store A"
        assert d["distance"] < 150
        # duplicate clock-in blocked
        r2 = client.post("/api/attendance/clock-in", json={"lat": A[0] + 0.0003, "lng": A[1]}, headers=h)
        assert r2.status_code == 409
        # clock-out success
        r3 = client.post("/api/attendance/clock-out", json={"lat": A[0] + 0.0003, "lng": A[1]}, headers=h)
        assert r3.status_code == 200 and r3.json()["result"] == "out"
        assert r3.json()["worked_minutes"] >= 0


def test_clock_out_without_clock_in():
    with TestClient(app):
        r = client.post("/api/attendance/clock-out", json={"lat": A[0], "lng": A[1]}, headers=_tok("U-acct"))
        assert r.status_code == 409


def test_clock_in_outside_radius_creates_pending():
    with TestClient(app):
        h = _tok("U-bm")   # branch_manager, has Store A/B, allow_override default True
        r = client.post("/api/attendance/clock-in",
                        json={"lat": A[0] + 0.01, "lng": A[1], "live": True, "reason": "Delivery"}, headers=h)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["result"] == "pending" and d["distance"] > 150
        # a manager can see + approve it
        pend = client.get("/api/attendance/pending", headers=_tok("U-owner")).json()
        aid = next(a["id"] for a in pend if a["approval"] == "pending")
        ap = client.post(f"/api/attendance/{aid}/approve", headers=_tok("U-owner"))
        assert ap.status_code == 200 and ap.json()["status"] == "active"


def test_boundary_exact_radius_accepted():
    with TestClient(app):
        h = _tok("U-inv")   # inventory_manager, Store A only
        # place a point, set the branch radius to exactly that distance
        p = (A[0] + 0.0009, A[1])
        d = int(round(haversine(A[0], A[1], p[0], p[1])))
        client.put("/api/attendance/branch/Store A", headers=_tok("U-owner"), json={"radius_m": d})
        r = client.post("/api/attendance/clock-in", json={"lat": p[0], "lng": p[1], "live": True}, headers=h)
        assert r.status_code == 200 and r.json()["result"] == "in"   # dist == radius -> accepted
        client.post("/api/attendance/clock-out", json={"lat": p[0], "lng": p[1]}, headers=h)
        client.put("/api/attendance/branch/Store A", headers=_tok("U-owner"), json={"radius_m": 150})


def test_multi_branch_within_range_offers_choice():
    with TestClient(app):
        # move Store B next to Store A so both are within range
        client.put("/api/attendance/branch/Store B", headers=_tok("U-owner"),
                   json={"lat": A[0] + 0.0002, "lng": A[1], "radius_m": 300})
        client.put("/api/attendance/branch/Store A", headers=_tok("U-owner"), json={"radius_m": 300})
        r = client.post("/api/attendance/clock-in", json={"lat": A[0], "lng": A[1], "live": True},
                        headers=_tok("U-owner"))
        assert r.status_code == 200 and r.json()["result"] == "choose"
        names = {c["branch"] for c in r.json()["candidates"]}
        assert {"Store A", "Store B"} <= names
        # pick one explicitly -> clocks in there
        r2 = client.post("/api/attendance/clock-in",
                         json={"lat": A[0], "lng": A[1], "live": True, "branch": "Store B"}, headers=_tok("U-owner"))
        assert r2.status_code == 200 and r2.json()["branch"] == "Store B"
        client.post("/api/attendance/clock-out", json={"lat": A[0], "lng": A[1]}, headers=_tok("U-owner"))
        client.put("/api/attendance/branch/Store A", headers=_tok("U-owner"), json={"radius_m": 150})


def test_unauthorized_branch_blocked():
    with TestClient(app):
        # U-inv only has Store A; asking for Store B must be refused
        r = client.post("/api/attendance/clock-in",
                        json={"lat": A[0], "lng": A[1], "live": True, "branch": "Store B"}, headers=_tok("U-inv"))
        assert r.status_code == 403


def test_forwarded_location_rejected():
    with TestClient(app):
        r = client.post("/api/attendance/clock-in",
                        json={"lat": A[0], "lng": A[1], "live": False}, headers=_tok("U-acct"))
        assert r.status_code == 422


def test_branch_settings_roundtrip():
    with TestClient(app):
        client.put("/api/attendance/branch/Store C", headers=_tok("U-owner"),
                   json={"lat": 31.5, "lng": 34.46, "radius_m": 200, "allow_override": False})
        g = client.get("/api/attendance/branch/Store C", headers=_tok("U-owner")).json()
        assert g["radius_m"] == 200 and g["allow_override"] is False

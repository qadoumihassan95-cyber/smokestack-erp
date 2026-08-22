"""Live-endpoint proof for BF-CC-01, run against whatever DATABASE_URL is set.

Reproduces the exact scenario from the original finding: seed known rows, call
the real /api/control/validate over HTTP, and print the Accounting section plus
the headline verdict. Run once with clean source and once with `_sum` mutated in
core.py SOURCE; the verdict must differ.
"""
import os
import sys
from datetime import date
from decimal import Decimal

os.environ.setdefault("SEED_ON_START", "true")
os.environ.setdefault("JWT_SECRET", "bfcc01-proof-secret-long-enough")

from fastapi.testclient import TestClient          # noqa: E402
from app.main import app                           # noqa: E402
from app.database import SessionLocal              # noqa: E402
from app import models                             # noqa: E402

MARK = "BFCC01-PROOF"


def test_endpoint_proof():
    """Run under pytest so the production-secret guard applies its test-mode path
    rather than being circumvented."""
    with TestClient(app) as c:
        db = SessionLocal()
        try:
            db.query(models.Ledger).filter(models.Ledger.memo == MARK).delete(
                synchronize_session=False)
            db.query(models.Purchase).filter(models.Purchase.vendor == MARK).delete(
                synchronize_session=False)
            db.commit()
            brs = [b.name for b in db.query(models.Branch).all()]
            a, b = brs[0], brs[1]
            today = date.today()
            first = today.replace(day=1)
            for r in [
                models.Ledger(branch=a, type="sale", amount=Decimal("1000.10"),
                              tax=Decimal("80.01"), entry_date=today, company_id=1, memo=MARK),
                models.Ledger(branch=b, type="sale", amount=Decimal("500.15"),
                              tax=Decimal("40.04"), entry_date=today, company_id=1, memo=MARK),
                models.Ledger(branch=a, type="expense", amount=Decimal("200.02"),
                              tax=0, entry_date=today, company_id=1, memo=MARK),
            ]:
                db.add(r)
            db.add(models.Purchase(id="P-proof", vendor=MARK, branch=a,
                                   amount=Decimal("400.20"), status="approved",
                                   purchase_date=first, company_id=1))
            db.commit()
            real_sales = db.query(models.Ledger).filter(
                models.Ledger.memo == MARK, models.Ledger.type == "sale").count()
        finally:
            db.close()

        r = c.post("/api/auth/login", data={"username": "U-owner", "password": "demo1234"})
        if r.status_code != 200:
            raise AssertionError("login failed %s %s" % (r.status_code, r.text[:200]))
        h = {"Authorization": "Bearer " + r.json()["access_token"]}
        rep = c.get("/api/control/validate", headers=h).json()

        print("PERSISTED (this probe's rows): sales 1500.25 across %d rows, "
              "expense 200.02, purchases 400.20" % real_sales)
        print()
        print("HEADLINE  score=%s  label=%s  severity=%s" %
              (rep.get("score"), rep.get("label"), rep.get("severity")))
        print("TOTALS    %s" % (rep.get("totals"),))
        print()
        for sec in rep["sections"]:
            if sec["module"] not in ("Accounting", "Dashboard"):
                continue
            print("%s:" % sec["module"].upper())
            for ch in sec["checks"]:
                print("  [%-11s] %-58s value=%s"
                      % (ch["status"], ch["check"][:58], ch["value"]))
            print()
        # Real assertions, not just a printout: a report carrying unverifiable
        # mandatory capabilities may never read "Healthy", and the declared
        # unsupported capabilities must not be counted as passing.
        assert rep["label"] != "Healthy", (
            "unsupported capabilities are declared, so Healthy is not available")
        assert rep["totals"]["unsupported"] >= 1, rep["totals"]

        db2 = SessionLocal()
        try:
            db2.query(models.Ledger).filter(models.Ledger.memo == MARK).delete(
                synchronize_session=False)
            db2.query(models.Purchase).filter(models.Purchase.vendor == MARK).delete(
                synchronize_session=False)
            db2.commit()
        finally:
            db2.close()

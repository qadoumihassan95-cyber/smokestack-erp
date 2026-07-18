"""PHASE 4/5/6/8 — volume data generation, independent accounting reconciliation,
performance timing and database integrity audit. Runs against an ISOLATED local
database (never production)."""
import os, sys, tempfile, random, time
from datetime import date, timedelta

_DB = os.path.join(tempfile.gettempdir(), "smokestack_volume.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["SEED_ON_START"] = "true"
os.environ["JWT_SECRET"] = "vol-secret"

from fastapi.testclient import TestClient
from sqlalchemy import func
from app.main import app
from app.database import SessionLocal
from app import models

random.seed(42)
FAIL = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond else f"  -> {detail}"))
    if not cond:
        FAIL.append(name)

with TestClient(app):
    db = SessionLocal()
    today = date.today()

    # ---------------- PHASE 4: generate realistic volume ----------------
    t0 = time.time()
    BRANCHES = [b.name for b in db.query(models.Branch).all()]
    SKUS = [p.sku for p in db.query(models.Product).all()]
    ACCTS = ["Cash", "Checking", "Business Bank", "Credit Card"]
    CATS = ["Utilities", "Rent", "Supplies", "Marketing", "Payroll", "Other"]

    N_SALES, N_PURCH, N_EXP, N_MOVE = 10000, 3000, 2000, 20000
    rows = []
    for i in range(N_SALES):
        d = today - timedelta(days=random.randint(0, 364))
        amt = round(random.uniform(5, 900), 2)
        taxable = random.random() < 0.85
        tax = round(amt * 0.0825 / 1.0825, 2) if taxable else 0.0   # tax-inclusive extraction
        rows.append(models.Ledger(branch=random.choice(BRANCHES), type="sale", amount=amt, tax=tax,
                                  account=random.choice(ACCTS), entry_date=d, created_by="U-owner"))
    for i in range(N_EXP):
        d = today - timedelta(days=random.randint(0, 364))
        c = random.choice(CATS)
        rows.append(models.Ledger(branch=random.choice(BRANCHES), type="expense",
                                  amount=round(random.uniform(10, 600), 2), tax=0, category=c,
                                  custom_description=("Misc " + str(i)) if c == "Other" else None,
                                  account=random.choice(ACCTS), entry_date=d, created_by="U-owner"))
    for i in range(400):
        d = today - timedelta(days=random.randint(0, 364))
        rows.append(models.Ledger(branch=random.choice(BRANCHES), type="payroll",
                                  amount=round(random.uniform(800, 4000), 2), tax=0,
                                  account="Checking", entry_date=d, created_by="U-owner"))
    db.bulk_save_objects(rows)

    purch = []
    for i in range(N_PURCH):
        d = today - timedelta(days=random.randint(0, 364))
        purch.append(models.Purchase(id=f"PO-V{i}", vendor=f"Vendor {i%500}",
                                     branch=random.choice(BRANCHES),
                                     amount=round(random.uniform(50, 5000), 2),
                                     status=random.choice(["approved", "approved", "pending_approval"]),
                                     purchase_date=d))
    db.bulk_save_objects(purch)

    movs, stock = [], {}
    for i in range(N_MOVE):
        sku, br = random.choice(SKUS), random.choice(BRANCHES)
        key = (sku, br); cur = stock.get(key, 0)
        if random.random() < 0.5 or cur <= 0:
            chg = random.randint(1, 40); typ = "receive"
        else:
            chg = -random.randint(1, min(cur, 20)); typ = "sale"
        after = cur + chg; stock[key] = after
        movs.append(models.Movement(ref=f"MV-V{i}", sku=sku, branch=br, type=typ,
                                    qty_before=cur, qty_change=chg, qty_after=after,
                                    unit_cost=round(random.uniform(0.5, 8), 2), user_id="U-owner",
                                    moved_at=today - timedelta(days=random.randint(0, 364))))
    db.bulk_save_objects(movs)
    for (sku, br), q in stock.items():
        st = db.query(models.Stock).filter_by(sku=sku, branch=br).first()
        if st: st.qty = q
        else: db.add(models.Stock(sku=sku, branch=br, qty=q))
    db.commit()
    gen = time.time() - t0
    total_rows = N_SALES + N_EXP + 400 + N_PURCH + N_MOVE
    print(f"\nPHASE 4 — generated {total_rows:,} records in {gen:.1f}s "
          f"({N_SALES} sales, {N_PURCH} purchases, {N_EXP} expenses, {N_MOVE} movements)\n")

    # ---------------- PHASE 3/8: independent reconciliation ----------------
    def tok(uid="U-owner"):
        r = TestClient(app).post("/api/auth/login", data={"username": uid, "password": "demo1234"})
        return {"Authorization": "Bearer " + r.json()["access_token"]}
    c = TestClient(app); H = tok()

    def sql_sum(typ, d0, d1, col="amount"):
        colx = models.Ledger.amount if col == "amount" else models.Ledger.tax
        return float(db.query(func.coalesce(func.sum(colx), 0)).filter(
            models.Ledger.type == typ, models.Ledger.entry_date >= d0,
            models.Ledger.entry_date <= d1).scalar() or 0)

    def sql_purch(d0, d1):
        return float(db.query(func.coalesce(func.sum(models.Purchase.amount), 0)).filter(
            models.Purchase.status != "rejected", models.Purchase.purchase_date >= d0,
            models.Purchase.purchase_date <= d1).scalar() or 0)

    print("PHASE 3/8 — accounting reconciliation (API vs raw SQL)")
    for period, d0 in [("today", today), ("week", today - timedelta(days=today.weekday())),
                       ("month", today.replace(day=1)), ("year", today.replace(month=1, day=1))]:
        t = time.time()
        k = c.get(f"/api/reports/kpi?period={period}&branch=all", headers=H).json()
        ms = (time.time() - t) * 1000
        rev, tax = sql_sum("sale", d0, today), sql_sum("sale", d0, today, "tax")
        cogs = sql_purch(d0, today) + sql_sum("purchase", d0, today)
        opex, pay = sql_sum("expense", d0, today), sql_sum("payroll", d0, today)
        exp_costs, exp_profit = cogs + opex + pay, rev - tax - (cogs + opex + pay)
        check(f"{period}: costs API={k['costs']['current']} == SQL={round(exp_costs,2)}",
              abs(k["costs"]["current"] - exp_costs) < 0.02, f"delta {k['costs']['current']-exp_costs}")
        check(f"{period}: profit API={k['profit']['current']} == SQL={round(exp_profit,2)}",
              abs(k["profit"]["current"] - exp_profit) < 0.02, f"delta {k['profit']['current']-exp_profit}")
        check(f"{period}: cogs breakdown matches", abs(k["costs"]["breakdown"]["cogs"] - cogs) < 0.02)
        check(f"{period}: kpi responds < 2000ms ({ms:.0f}ms)", ms < 2000, f"{ms:.0f}ms")

    # dashboard vs kpi(today) vs daily report must agree exactly
    d = c.get("/api/reports/dashboard?branch=all", headers=H).json()
    k = c.get("/api/reports/kpi?period=today&branch=all", headers=H).json()
    rep = c.get("/api/reports/daily?branch=all", headers=H).json()
    rrows = {r[0]: r[1] for r in rep["rows"]}
    check("dashboard.profit == kpi.profit", abs(d["profit_today"] - k["profit"]["current"]) < 0.02,
          f"{d['profit_today']} vs {k['profit']['current']}")
    check("daily report profit == dashboard profit", abs(rrows["Gross profit"] - d["profit_today"]) < 0.02)
    check("daily report sales == dashboard sales", abs(rrows["Sales"] - d["sales_today"]) < 0.02)

    # analytics + comparisons internal consistency
    t = time.time(); an = c.get("/api/reports/analytics?branch=all", headers=H).json()
    an_ms = (time.time() - t) * 1000
    check(f"analytics responds < 4000ms ({an_ms:.0f}ms)", an_ms < 4000, f"{an_ms:.0f}ms")
    m0 = today.replace(day=1)
    br_profit = sum(b["profit"] for b in an["branch_comparison"])
    rev_m, tax_m = sql_sum("sale", m0, today), sql_sum("sale", m0, today, "tax")
    cogs_m = sql_purch(m0, today) + sql_sum("purchase", m0, today)
    prof_m = rev_m - tax_m - (cogs_m + sql_sum("expense", m0, today) + sql_sum("payroll", m0, today))
    check("branch comparison profit sums to total month profit", abs(br_profit - prof_m) < 0.5,
          f"{br_profit} vs {prof_m}")
    exp_cat = sum(x["value"] for x in an["expenses_by_category"])
    check("expenses-by-category sums to month expenses",
          abs(exp_cat - sql_sum("expense", m0, today)) < 0.02)

    t = time.time(); cmp_ = c.get("/api/reports/comparisons?branch=all", headers=H).json()
    check(f"comparisons responds < 4000ms ({(time.time()-t)*1000:.0f}ms)", (time.time()-t)*1000 < 4000)
    check("forecast labelled as calculated, not guaranteed",
          "not a guaranteed" in cmp_["forecast"]["disclaimer"].lower())

    # branch scoping: per-branch figures must sum to the all-branches figure
    tot = 0.0
    for b in BRANCHES:
        kb = c.get(f"/api/reports/kpi?period=year&branch={b}", headers=H).json()
        tot += kb["costs"]["current"]
    kall = c.get("/api/reports/kpi?period=year&branch=all", headers=H).json()
    check("per-branch costs sum to all-branches costs", abs(tot - kall["costs"]["current"]) < 0.5,
          f"{tot} vs {kall['costs']['current']}")

    # ---------------- PHASE 6: database integrity audit ----------------
    print("\nPHASE 6 — database integrity")
    bad_mov = db.query(models.Movement).filter(
        models.Movement.qty_before + models.Movement.qty_change != models.Movement.qty_after).count()
    check("movement ledger invariant (before+change==after)", bad_mov == 0, f"{bad_mov} broken rows")
    neg = db.query(models.Stock).filter(models.Stock.qty < 0).count()
    check("no negative stock", neg == 0, f"{neg} rows")
    orphan_stock = db.query(models.Stock).filter(
        ~models.Stock.sku.in_(db.query(models.Product.sku))).count()
    check("no orphan stock rows", orphan_stock == 0, f"{orphan_stock}")
    orphan_mov = db.query(models.Movement).filter(
        ~models.Movement.sku.in_(db.query(models.Product.sku))).count()
    check("no orphan movements", orphan_mov == 0, f"{orphan_mov}")
    neg_amt = db.query(models.Ledger).filter(models.Ledger.amount < 0).count()
    check("no negative ledger amounts", neg_amt == 0, f"{neg_amt}")
    bad_tax = db.query(models.Ledger).filter(models.Ledger.tax > models.Ledger.amount).count()
    check("no ledger row with tax > amount", bad_tax == 0, f"{bad_tax}")
    dup_pk = db.query(models.Purchase.id, func.count()).group_by(models.Purchase.id).having(func.count() > 1).count()
    check("no duplicate purchase ids", dup_pk == 0, f"{dup_pk}")
    orphan_ub = db.query(models.UserBranch).filter(
        ~models.UserBranch.user_id.in_(db.query(models.User.id))).count()
    check("no orphan user_branch rows", orphan_ub == 0, f"{orphan_ub}")
    ledger_n = db.query(models.Ledger).count(); mov_n = db.query(models.Movement).count()
    print(f"  (ledger rows: {ledger_n:,} · movements: {mov_n:,} · purchases: {db.query(models.Purchase).count():,})")

    db.close()

print("\n" + "="*60)
print(f"VOLUME/RECONCILIATION AUDIT: {'ALL PASS' if not FAIL else str(len(FAIL))+' FAILURES'}")
if FAIL:
    for f in FAIL: print("  - " + f)
print("="*60)
sys.exit(1 if FAIL else 0)

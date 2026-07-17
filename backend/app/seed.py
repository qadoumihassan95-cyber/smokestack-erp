"""Idempotent seed — branches, one user per role (password: 'demo1234'),
products + stock + a small movement history, employees, partners, sample ledger.
Runs on first boot when SEED_ON_START=true. Safe to run repeatedly."""
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from . import models
from .config import settings
from .security import hash_pw

BRANCHES = ["Store A", "Store B", "Store C"]
USERS = [
    ("U-owner", "owner", "Owner", None),
    ("U-admin", "admin", "Admin", None),
    ("U-bm", "branch_manager", "Branch Manager", ["Store A", "Store B"]),
    ("U-inv", "inventory_manager", "Inventory Mgr", ["Store A"]),
    ("U-acct", "accountant", "Accountant", None),
    ("U-cash", "cashier", "Cashier", ["Store A"]),
    ("U-emp", "employee", "Employee", ["Store A"]),
]
PRODUCTS = [
    ("MRB-GLD", "012345678905", "Marlboro Gold", "Philip Morris", 6.2, 9.5, 20, {"Store A": 140, "Store B": 60, "Store C": 12}),
    ("ZYN-CM", "028900005107", "Zyn Cool Mint", "Swedish Match", 3.1, 5.5, 30, {"Store A": 18, "Store B": 44, "Store C": 70}),
    ("RAW-CLS", "716165174233", "RAW Classic", "HBI", 0.9, 2.5, 40, {"Store A": 5, "Store B": 0, "Store C": 22}),
    ("GRP-GRN", "810011300019", "Grinder Green", "HBI", 2.0, 6.0, 10, {"Store A": 30, "Store B": 25, "Store C": 8}),
]

def seed(db: Session):
    if db.query(models.Branch).count():
        return  # already seeded
    # Short demo password from env (defaults to 'demo1234'). hash_pw validates
    # the 72-byte bcrypt limit and will raise a clear error on an over-long value.
    seed_pw_hash = hash_pw(settings.seed_password)
    # Seed branch coordinates so attendance geofencing works out of the box (admins
    # can change these in the web app → Settings → Branches → Attendance Location).
    GEO = {"Store A": (32.221100, 35.254400), "Store B": (31.905000, 35.208000),
           "Store C": (31.501700, 34.466800)}
    for b in BRANCHES:
        lat, lng = GEO.get(b, (None, None))
        db.add(models.Branch(name=b, lat=lat, lng=lng, radius_m=150, timezone="UTC",
                             loc_verify=True, grace_min=10, allow_override=True, attendance_active=True))
    for uid, role, name, branches in USERS:
        db.add(models.User(id=uid, name=name, role=role, email=f"{role}@smokestack.local",
                           password_hash=seed_pw_hash))
        for br in (branches or []):
            db.add(models.UserBranch(user_id=uid, branch=br))
    db.flush()
    seq = 0
    for sku, bc, name, sup, cost, price, mn, stock in PRODUCTS:
        db.add(models.Product(sku=sku, barcode=bc, name=name, supplier=sup, cost=cost, price=price, min_level=mn))
        # Flush the product row BEFORE its stock/movements. Postgres enforces the
        # products FK immediately, so the parent row must exist first. (SQLite
        # doesn't enforce FKs by default, which is why this only bit in prod.)
        db.flush()
        for br, q in stock.items():
            db.add(models.Stock(sku=sku, branch=br, qty=q))
            # a believable history that ends at current qty
            running = 0
            for days, chg in [(60, round(q * 0.7)), (30, round(q * 0.5)), (10, -round(q * 0.2))]:
                before = running; running += chg
                seq += 1
                db.add(models.Movement(ref=f"MV-{1000+seq}", sku=sku, branch=br,
                                       type="receive" if chg >= 0 else "sale",
                                       qty_before=before, qty_change=chg, qty_after=running,
                                       unit_cost=cost, user_id="U-owner",
                                       moved_at=datetime.utcnow() - timedelta(days=days)))
            if running != q:
                seq += 1
                db.add(models.Movement(ref=f"MV-{1000+seq}", sku=sku, branch=br, type="adjust",
                                       qty_before=running, qty_change=q - running, qty_after=q,
                                       unit_cost=cost, user_id="U-inv",
                                       moved_at=datetime.utcnow() - timedelta(days=2)))
    for eid, nm, br, sal in [("EMP-1001", "Sam Rivera", "Store A", 3200), ("EMP-1002", "Ana Gomez", "Store B", 2600), ("EMP-1003", "Dev Patel", "Store C", 2900)]:
        db.add(models.Employee(id=eid, name=nm, branch=br, title="Staff", salary=sal, active=True))
    db.add(models.Customer(id="C-01", name="Downtown Vape Co", balance=1240.5))
    db.add(models.Supplier(id="S-01", name="Philip Morris", balance=-4820))
    for br, amt in [("Store A", 8420), ("Store B", 5100), ("Store C", 4780)]:
        db.add(models.Ledger(branch=br, type="sale", amount=amt, tax=round(amt * 0.0825, 2), account="Cash"))
    db.add(models.Ledger(branch="Store A", type="expense", amount=320, category="Utilities", account="Checking"))
    db.commit()

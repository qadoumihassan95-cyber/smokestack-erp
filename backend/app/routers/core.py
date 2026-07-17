from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import func
from datetime import date
from ..database import get_db
from .. import models, security as S, permissions as P

router = APIRouter(prefix="/api", tags=["core"])

@router.get("/branches")
def branches(db: Session = Depends(get_db), user: models.User = Depends(S.require("view"))):
    return S.scope_branches(user, db)

@router.get("/reports/dashboard")
def dashboard(branch: str = "all", db: Session = Depends(get_db), user: models.User = Depends(S.require("view"))):
    brs = S.scope_branches(user, db) if branch == "all" else [branch]
    today = date.today()
    def s(t):
        return float(db.query(func.coalesce(func.sum(models.Ledger.amount), 0)).filter(
            models.Ledger.type == t, models.Ledger.branch.in_(brs), models.Ledger.entry_date == today).scalar() or 0)
    sales, exp = s("sale"), s("expense")
    tax = float(db.query(func.coalesce(func.sum(models.Ledger.tax), 0)).filter(
        models.Ledger.type == "sale", models.Ledger.branch.in_(brs), models.Ledger.entry_date == today).scalar() or 0)
    inv = db.query(func.coalesce(func.sum(models.Stock.qty), 0),
                   func.coalesce(func.sum(models.Stock.qty * models.Product.cost), 0),
                   func.coalesce(func.sum(models.Stock.qty * models.Product.price), 0)) \
        .join(models.Product, models.Product.sku == models.Stock.sku) \
        .filter(models.Stock.branch.in_(brs)).one()
    units, cost, retail = int(inv[0]), float(inv[1]), float(inv[2])
    # low / out
    per = {}
    for st in db.query(models.Stock).filter(models.Stock.branch.in_(brs)).all():
        per[st.sku] = per.get(st.sku, 0) + st.qty
    low = out = 0
    for p in db.query(models.Product).all():
        q = per.get(p.sku, 0)
        if q <= 0:
            out += 1
        elif q <= (p.min_level or 0):
            low += 1
    pend_appr = db.query(models.Approval).filter(models.Approval.status == "pending", models.Approval.branch.in_(brs)).count()
    out_data = {
        "branch": "All branches" if branch == "all" else branch,
        "sales_today": sales, "expenses_today": exp, "profit_today": sales - tax - exp,
        "inventory_units": units, "low": low, "out": out,
        "pending_approvals": pend_appr,
        "pending_purchases": db.query(models.Purchase).filter(models.Purchase.status.like("pending%"), models.Purchase.branch.in_(brs)).count(),
        "pending_transfers": db.query(models.Transfer).filter(models.Transfer.status == "pending").count(),
    }
    if P.can(user.role, "view_cost"):
        out_data.update({"inventory_cost": cost, "inventory_retail": retail, "potential_profit": retail - cost})
    return out_data

@router.get("/reports/daily")
def daily(branch: str = "all", db: Session = Depends(get_db), user: models.User = Depends(S.require("view"))):
    d = dashboard(branch, db, user)
    return {"title": "Daily report", "date": str(date.today()),
            "rows": [["Sales", d["sales_today"]], ["Expenses", d["expenses_today"]], ["Gross profit", d["profit_today"]]],
            "generated_by": f"{user.name} ({user.role})"}

@router.get("/audit")
def audit_log(limit: int = 100, db: Session = Depends(get_db), user: models.User = Depends(S.require("view_all_branches"))):
    rows = db.query(models.AuditLog).order_by(models.AuditLog.ts.desc()).limit(limit).all()
    return [{"ts": str(a.ts), "source": a.source, "user": a.user_id, "action": a.action,
             "entity": a.entity, "ref": a.ref, "detail": a.detail, "result": a.result} for a in rows]

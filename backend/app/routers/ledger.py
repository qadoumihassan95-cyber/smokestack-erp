from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from ..database import get_db
from .. import models, security as S
from ..schemas import ExpenseIn, PurchaseIn, SaleIn
from datetime import datetime

router = APIRouter(prefix="/api", tags=["ledger"])

def _row(x):
    return {"id": x.id, "branch": x.branch, "type": x.type, "amount": float(x.amount or 0),
            "tax": float(x.tax or 0), "category": x.category, "vendor": x.vendor,
            "account": x.account, "product": x.product, "employee": x.employee,
            "memo": x.memo, "custom_description": x.custom_description,
            # what to show as the human label: the free-text detail for "Other", else the category
            "label": (x.custom_description or x.category) if x.type == "expense" else x.category,
            "date": str(x.entry_date)}

@router.get("/sales")
def sales(branch: str = "all", db: Session = Depends(get_db), user: models.User = Depends(S.require("view"))):
    brs = S.scope_branches(user, db) if branch == "all" else [branch]
    q = db.query(models.Ledger).filter(models.Ledger.type == "sale", models.Ledger.branch.in_(brs))
    return [_row(x) for x in q.order_by(models.Ledger.id.desc()).limit(200).all()]

def _check_amount(amount, tax=None):
    """Money guards: no negative/zero postings, and sales tax can never exceed
    the gross amount it was collected on."""
    if amount is None or float(amount) <= 0:
        raise HTTPException(422, "Amount must be greater than zero.")
    if tax is not None:
        if float(tax) < 0:
            raise HTTPException(422, "Tax cannot be negative.")
        if float(tax) > float(amount):
            raise HTTPException(422, "Sales tax cannot exceed the sale amount.")


@router.post("/sales", status_code=201)
def add_sale(body: SaleIn, db: Session = Depends(get_db), user: models.User = Depends(S.require("create"))):
    S.assert_branch(user, db, body.branch)
    _check_amount(body.amount, body.tax)
    r = models.Ledger(branch=body.branch, type="sale", amount=body.amount, tax=body.tax,
                      account=body.account, product=body.product, employee=body.employee, created_by=user.id)
    db.add(r); db.commit()
    S.audit(db, user, "create", "sale", r.id, f"{body.amount} @ {body.branch}")
    return _row(r)

@router.get("/expenses")
def expenses(branch: str = "all", db: Session = Depends(get_db), user: models.User = Depends(S.require("view"))):
    brs = S.scope_branches(user, db) if branch == "all" else [branch]
    q = db.query(models.Ledger).filter(models.Ledger.type == "expense", models.Ledger.branch.in_(brs))
    return [_row(x) for x in q.order_by(models.Ledger.id.desc()).limit(200).all()]

@router.post("/expenses", status_code=201)
def add_expense(body: ExpenseIn, db: Session = Depends(get_db), user: models.User = Depends(S.require("create"))):
    S.assert_branch(user, db, body.branch)
    _check_amount(body.amount)
    # "Other" requires a specific free-text description — never store only "Other".
    desc = (body.custom_description or "").strip()
    if (body.category or "").strip().lower() == "other" and not desc:
        raise HTTPException(422, "A description is required when the category is 'Other'.")
    custom = desc if (body.category or "").strip().lower() == "other" else None
    r = models.Ledger(branch=body.branch, type="expense", amount=body.amount, category=body.category,
                      account=body.account, memo=body.memo, custom_description=custom, created_by=user.id)
    db.add(r); db.commit()
    label = custom or body.category
    S.audit(db, user, "create", "expense", r.id, f"{label} {body.amount} @ {body.branch}")
    return _row(r)

@router.get("/purchases")
def purchases(branch: str = "all", db: Session = Depends(get_db), user: models.User = Depends(S.require("view"))):
    brs = S.scope_branches(user, db) if branch == "all" else [branch]
    q = db.query(models.Purchase).filter(models.Purchase.branch.in_(brs))
    return [{"id": p.id, "vendor": p.vendor, "branch": p.branch, "amount": float(p.amount or 0),
             "status": p.status, "date": str(p.purchase_date)} for p in q.order_by(models.Purchase.purchase_date.desc()).all()]

@router.post("/purchases", status_code=201)
def add_purchase(body: PurchaseIn, db: Session = Depends(get_db), user: models.User = Depends(S.require("create"))):
    S.assert_branch(user, db, body.branch)
    _check_amount(body.amount)
    pid = f"PO-{int(datetime.utcnow().timestamp())}"
    p = models.Purchase(id=pid, vendor=body.vendor, branch=body.branch, amount=body.amount, status="pending_approval")
    db.add(p)
    db.add(models.Approval(id=f"AP-{pid}", kind="purchase", ref=pid, branch=body.branch, amount=body.amount,
                           requested_by=user.name, summary=f"Purchase {pid} · {body.vendor} · ${body.amount:.0f}"))
    db.commit()
    S.audit(db, user, "create", "purchase", pid, f"{body.vendor} {body.amount}")
    return {"id": pid, "status": "pending_approval"}

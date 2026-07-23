from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from ..database import get_db
from .. import models, security as S, partners_repo as PR

router = APIRouter(prefix="/api", tags=["partners"])

@router.get("/customers")
def customers(db: Session = Depends(get_db), user: models.User = Depends(S.require("view"))):
    return [{"id": c.id, "name": c.name, "balance": float(c.balance or 0)} for c in PR.list_customers(db)]

@router.get("/customers/{cid}")
def customer(cid: str, db: Session = Depends(get_db), user: models.User = Depends(S.require("view"))):
    # tenant-scoped lookup by (company_id, business id) — works in both B-B phases
    c = PR.get_customer(db, cid)
    if not c:
        raise HTTPException(404, "Not found")
    return {"id": c.id, "name": c.name, "balance": float(c.balance or 0)}

@router.get("/suppliers")
def suppliers(db: Session = Depends(get_db), user: models.User = Depends(S.require("view"))):
    return [{"id": s.id, "name": s.name, "balance": float(s.balance or 0)} for s in PR.list_suppliers(db)]

@router.get("/suppliers/{sid}")
def supplier(sid: str, db: Session = Depends(get_db), user: models.User = Depends(S.require("view"))):
    # tenant-scoped lookup by (company_id, business id) — works in both B-B phases
    s = PR.get_supplier(db, sid)
    if not s:
        raise HTTPException(404, "Not found")
    pos = db.query(models.Purchase).filter(models.Purchase.vendor == s.name).order_by(models.Purchase.purchase_date.desc()).all()
    return {"id": s.id, "name": s.name, "balance": float(s.balance or 0),
            "purchases": [{"id": p.id, "amount": float(p.amount or 0), "date": str(p.purchase_date)} for p in pos]}

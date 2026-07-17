from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import func
from datetime import datetime, timedelta
from ..database import get_db
from .. import models, security as S, permissions as P
from ..schemas import ProductIn, ProductUpdate, StockOp

router = APIRouter(prefix="/api/inventory", tags=["inventory"])

def _prod_out(db, p, branches):
    rows = {s.branch: s.qty for s in db.query(models.Stock).filter(models.Stock.sku == p.sku).all()}
    stock = {b: int(rows.get(b, 0)) for b in branches}
    return {"sku": p.sku, "name": p.name, "barcode": p.barcode, "category": p.category,
            "supplier": p.supplier, "cost": float(p.cost or 0), "price": float(p.price or 0),
            "min": int(p.min_level or 0), "uom": p.uom, "shelf": p.shelf, "status": p.status,
            "stock": stock, "total": sum(stock.values())}

@router.get("/products")
def products(q: str = "", branch: str = "all", db: Session = Depends(get_db),
             user: models.User = Depends(S.require("view"))):
    branches = [branch] if branch != "all" and branch in S.scope_branches(user, db) else S.scope_branches(user, db)
    query = db.query(models.Product).filter(models.Product.status != "deleted")
    if q:
        like = f"%{q.lower()}%"
        query = query.filter(func.lower(models.Product.name).like(like) |
                             func.lower(models.Product.sku).like(like) |
                             models.Product.barcode.like(f"%{q}%"))
    return [_prod_out(db, p, branches) for p in query.order_by(models.Product.name).all()]

@router.get("/barcode/{code}")
def by_barcode(code: str, db: Session = Depends(get_db), user: models.User = Depends(S.require("view"))):
    p = db.query(models.Product).filter(models.Product.barcode == code).first()
    if not p:
        raise HTTPException(404, "No product with that barcode")
    out = _prod_out(db, p, S.scope_branches(user, db))
    if not P.can(user.role, "view_cost"):
        out["cost"] = None
    out["fifo_cost"] = out["cost"]
    return out

@router.post("/products", status_code=201)
def create_product(body: ProductIn, db: Session = Depends(get_db), user: models.User = Depends(S.require("create", "edit"))):
    if db.get(models.Product, body.sku):
        raise HTTPException(409, "SKU already exists")
    if body.barcode and db.query(models.Product).filter(models.Product.barcode == body.barcode).first():
        raise HTTPException(409, "Barcode already exists")
    p = models.Product(sku=body.sku, name=body.name, barcode=body.barcode, category=body.category,
                       brand=body.brand, supplier=body.supplier, cost=body.cost, price=body.price,
                       min_level=body.min_level, uom=body.uom, shelf=body.shelf, status="active")
    db.add(p)
    for b in S.all_branch_names(db):
        db.add(models.Stock(sku=p.sku, branch=b, qty=0))
    db.commit()
    S.audit(db, user, "create", "product", p.sku, f"{p.name}")
    return _prod_out(db, p, S.scope_branches(user, db))

@router.patch("/products/{sku}")
def update_product(sku: str, body: ProductUpdate, db: Session = Depends(get_db), user: models.User = Depends(S.require("edit"))):
    p = db.get(models.Product, sku)
    if not p:
        raise HTTPException(404, "Not found")
    for f in ("name", "barcode", "category", "brand", "supplier", "cost", "price", "min_level", "uom", "shelf"):
        v = getattr(body, f, None)
        if v is not None:
            setattr(p, f, v)
    db.commit()
    S.audit(db, user, "edit", "product", sku, "updated")
    return _prod_out(db, p, S.scope_branches(user, db))

@router.post("/products/{sku}/deactivate")
def deactivate_product(sku: str, db: Session = Depends(get_db), user: models.User = Depends(S.require("edit"))):
    p = db.get(models.Product, sku)
    if not p:
        raise HTTPException(404, "Not found")
    p.status = "inactive"
    db.commit()
    S.audit(db, user, "deactivate", "product", sku)
    return {"ok": True, "status": "inactive"}

@router.post("/products/{sku}/reactivate")
def reactivate_product(sku: str, db: Session = Depends(get_db), user: models.User = Depends(S.require("edit"))):
    p = db.get(models.Product, sku)
    if not p:
        raise HTTPException(404, "Not found")
    p.status = "active"
    db.commit()
    S.audit(db, user, "reactivate", "product", sku)
    return {"ok": True, "status": "active"}

def _write_movement(db, user, sku, branch, mtype, change, notes=""):
    st = db.query(models.Stock).filter_by(sku=sku, branch=branch).first()
    if not st:
        st = models.Stock(sku=sku, branch=branch, qty=0); db.add(st); db.flush()
    before = int(st.qty or 0); after = max(0, before + int(change)); st.qty = after
    p = db.get(models.Product, sku)
    db.add(models.Movement(ref=f"MV-{int(datetime.utcnow().timestamp())}", sku=sku, branch=branch,
                           type=mtype, qty_before=before, qty_change=int(change), qty_after=after,
                           unit_cost=(p.cost if p else 0), user_id=user.id, notes=notes))
    db.commit()
    return after

@router.post("/receive")
def receive(body: StockOp, db: Session = Depends(get_db), user: models.User = Depends(S.require("continuous_receiving"))):
    S.assert_branch(user, db, body.branch)
    if not db.get(models.Product, body.sku):
        raise HTTPException(404, "Product not found")
    after = _write_movement(db, user, body.sku, body.branch, "receive", abs(body.qty))
    S.audit(db, user, "receive", "product", body.sku, f"+{abs(body.qty)} @ {body.branch}")
    return {"ok": True, "sku": body.sku, "branch": body.branch, "new_stock": after}

@router.post("/adjust")
def adjust(body: StockOp, db: Session = Depends(get_db), user: models.User = Depends(S.require("adjust_stock"))):
    S.assert_branch(user, db, body.branch)
    if not body.reason:
        raise HTTPException(422, "A reason is required to adjust stock")
    if not db.get(models.Product, body.sku):
        raise HTTPException(404, "Product not found")
    after = _write_movement(db, user, body.sku, body.branch, "adjust", body.qty, body.reason)
    S.audit(db, user, "adjust", "product", body.sku, f"{body.qty:+d} @ {body.branch}: {body.reason}")
    return {"ok": True, "sku": body.sku, "branch": body.branch, "new_stock": after}

@router.get("/movements")
def movements(branch: str = "all", start: str = "", end: str = "", limit: int = 300,
              db: Session = Depends(get_db), user: models.User = Depends(S.require("view_inventory_history"))):
    branches = S.scope_branches(user, db) if branch == "all" else [branch]
    q = db.query(models.Movement).filter(models.Movement.branch.in_(branches))
    if start:
        q = q.filter(models.Movement.moved_at >= start)
    if end:
        q = q.filter(models.Movement.moved_at <= end + " 23:59:59")
    rows = q.order_by(models.Movement.moved_at.desc()).limit(limit).all()
    return [{"id": m.id, "ref": m.ref, "sku": m.sku, "branch": m.branch, "type": m.type,
             "before": m.qty_before, "change": m.qty_change, "after": m.qty_after,
             "value": float((m.unit_cost or 0) * abs(m.qty_change or 0)), "user": m.user_id,
             "date": str(m.moved_at)} for m in rows]

def as_of_qty(db, sku, branch, date_iso):
    """Historical qty from the immutable ledger: qty after last move on/before the
    date; else the before of the first later move; else current stock."""
    cut = date_iso + " 23:59:59"
    last = (db.query(models.Movement).filter_by(sku=sku, branch=branch)
            .filter(models.Movement.moved_at <= cut).order_by(models.Movement.moved_at.desc()).first())
    if last:
        return int(last.qty_after)
    nxt = (db.query(models.Movement).filter_by(sku=sku, branch=branch)
           .filter(models.Movement.moved_at > cut).order_by(models.Movement.moved_at.asc()).first())
    if nxt:
        return int(nxt.qty_before)
    st = db.query(models.Stock).filter_by(sku=sku, branch=branch).first()
    return int(st.qty) if st else 0

@router.get("/asof")
def asof(date: str = Query(...), branch: str = "all", db: Session = Depends(get_db),
         user: models.User = Depends(S.require("view_asof"))):
    branches = S.scope_branches(user, db) if branch == "all" else [branch]
    rows = []
    for p in db.query(models.Product).filter(models.Product.status != "deleted").all():
        per = {b: as_of_qty(db, p.sku, b, date) for b in branches}
        tot = sum(per.values()); cost = tot * float(p.cost or 0); retail = tot * float(p.price or 0)
        rows.append({"sku": p.sku, "name": p.name, "per_branch": per, "qty": tot,
                     "cost": cost, "retail": retail, "profit": retail - cost,
                     "status": "out" if tot <= 0 else "low" if tot <= (p.min_level or 0) else "ok"})
    return {"ds": date, "branches": branches, "rows": rows,
            "units": sum(r["qty"] for r in rows), "cost_value": sum(r["cost"] for r in rows),
            "retail": sum(r["retail"] for r in rows), "profit": sum(r["profit"] for r in rows),
            "low": sum(1 for r in rows if r["status"] == "low"),
            "out": sum(1 for r in rows if r["status"] == "out")}

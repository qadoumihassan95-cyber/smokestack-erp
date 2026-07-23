from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from datetime import datetime
from ..database import get_db
from .. import models, security as S, partners_repo as PR
from ..schemas import TransferIn, ApprovalDecision, ClockIn

router = APIRouter(prefix="/api", tags=["workflow"])

@router.get("/transfers")
def transfers(db: Session = Depends(get_db), user: models.User = Depends(S.require("view"))):
    brs = S.scope_branches(user, db)
    q = db.query(models.Transfer).filter((models.Transfer.from_branch.in_(brs)) | (models.Transfer.to_branch.in_(brs)))
    return [{"id": t.id, "sku": t.sku, "from": t.from_branch, "to": t.to_branch, "qty": t.qty, "status": t.status} for t in q.all()]

@router.post("/transfers", status_code=201)
def create_transfer(body: TransferIn, db: Session = Depends(get_db), user: models.User = Depends(S.require("transfer_stock"))):
    S.assert_branch(user, db, body.from_branch)
    if int(body.qty) <= 0:
        raise HTTPException(422, "Transfer quantity must be greater than zero.")
    if body.from_branch == body.to_branch:
        raise HTTPException(422, "Source and destination branches must differ.")
    if not db.get(models.Product, body.sku):
        raise HTTPException(404, "Product not found")
    src = db.query(models.Stock).filter_by(sku=body.sku, branch=body.from_branch).first()
    have = int(src.qty or 0) if src else 0
    if have < int(body.qty):
        raise HTTPException(422, f"Insufficient stock: {body.from_branch} holds {have} × {body.sku}.")
    tid = f"TR-{int(datetime.utcnow().timestamp())}"
    db.add(models.Transfer(id=tid, sku=body.sku, from_branch=body.from_branch, to_branch=body.to_branch, qty=body.qty, status="pending"))
    db.add(models.Approval(id=f"AP-{tid}", kind="transfer", ref=tid, branch=body.from_branch, amount=0,
                           requested_by=user.name, summary=f"Transfer {body.qty}× {body.sku} · {body.from_branch}→{body.to_branch}"))
    db.commit()
    S.audit(db, user, "create", "transfer", tid)
    return {"id": tid, "status": "pending"}

@router.get("/approvals")
def approvals(db: Session = Depends(get_db), user: models.User = Depends(S.require("approve"))):
    brs = S.scope_branches(user, db)
    q = db.query(models.Approval).filter(models.Approval.status == "pending", models.Approval.branch.in_(brs))
    return [{"id": a.id, "kind": a.kind, "ref": a.ref, "branch": a.branch, "amount": float(a.amount or 0),
             "by": a.requested_by, "summary": a.summary} for a in q.all()]

def _decide(db, user, aid, status, comment):
    a = PR.get_approval(db, aid)
    if not a:
        raise HTTPException(404, "Not found")
    if a.status != "pending":
        raise HTTPException(409, f"This request was already {a.status}.")
    S.assert_branch(user, db, a.branch)
    # Approving must actually complete the underlying work. Previously the
    # approval row flipped to "approved" but the transfer never moved stock and
    # the purchase stayed pending forever.
    if a.kind == "transfer":
        t = PR.get_transfer(db, a.ref)
        if t:
            if status == "approved":
                if t.status != "pending":
                    raise HTTPException(409, f"Transfer already {t.status}.")
                from .inventory import _write_movement
                _write_movement(db, user, t.sku, t.from_branch, "transfer_out", -int(t.qty),
                                notes=f"Transfer {t.id} -> {t.to_branch}")
                _write_movement(db, user, t.sku, t.to_branch, "transfer_in", int(t.qty),
                                notes=f"Transfer {t.id} <- {t.from_branch}")
            t.status = status
    elif a.kind == "purchase":
        p = PR.get_purchase(db, a.ref)
        if p:
            p.status = status
    a.status = status; a.decided_by = user.name; a.comment = comment
    db.commit()
    S.audit(db, user, status, "approval", aid, comment or "")
    return {"ok": True, "summary": a.summary, "status": status}

@router.post("/approvals/{aid}/approve")
def approve(aid: str, body: ApprovalDecision, db: Session = Depends(get_db), user: models.User = Depends(S.require("approve"))):
    return _decide(db, user, aid, "approved", body.comment)

@router.post("/approvals/{aid}/reject")
def reject(aid: str, body: ApprovalDecision, db: Session = Depends(get_db), user: models.User = Depends(S.require("approve"))):
    return _decide(db, user, aid, "rejected", body.comment)

@router.post("/clock")
def clock(body: ClockIn, db: Session = Depends(get_db), user: models.User = Depends(S.require("close_shift"))):
    S.assert_branch(user, db, body.branch)
    db.add(models.ClockEvent(employee=body.employee, branch=body.branch, direction=body.direction))
    db.commit()
    S.audit(db, user, "clock_" + body.direction, "employee", body.employee, body.branch)
    return {"ok": True}

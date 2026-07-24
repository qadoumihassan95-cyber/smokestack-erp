"""Shared transfer-completion service (TD-002).

The ONE execution path that completes an approved stock transfer, callable from
every surface (HTTP approval, Telegram worker, future automation). Guarantees:

  * single database transaction, exactly one commit (ADR-008 — caller owns commit);
  * compare-and-swap status claim, so concurrent or replayed approvals apply at most
    once (the loser gets 409);
  * both stock legs mutated atomically via the canonical-lock-order helper
    (deadlock-free), each a commit-free guarded update;
  * audit written INSIDE the transaction (state + audit commit together — no missing
    audit for a successful transfer);
  * any failure rolls the whole unit back — no partial inventory, no half ledger,
    the transfer stays ``pending`` and is legitimately retryable.

Isolation assumption (documented, per the design review): PostgreSQL READ COMMITTED is
sufficient here because correctness rests on the atomic compare-and-swap claim and the
atomic guarded row UPDATE — there is no non-atomic read-then-write on stock.
"""
from fastapi import HTTPException
from sqlalchemy import update as sa_update

from . import models, security as S
from .locking import apply_ordered_movements


def execute_approved_transfer(db, user, transfer, approval, comment):
    """Complete an approved transfer as one atomic, race-safe transaction."""
    cid = getattr(user, "_company_id", 1)

    # Compare-and-swap: atomically claim pending -> approved. A concurrent or replayed
    # approval updates 0 rows and is rejected. Core UPDATE bypasses the SELECT-only
    # scoping event, so scope by company_id explicitly. This claim (the control row) is
    # acquired BEFORE the stock rows — the canonical cross-resource lock order.
    claimed = db.execute(
        sa_update(models.Transfer)
        .where(models.Transfer.row_id == transfer.row_id,
               models.Transfer.company_id == cid,
               models.Transfer.status == "pending")
        .values(status="approved")
    ).rowcount
    if claimed != 1:
        raise HTTPException(409, "Transfer already processed.")

    # Both legs, canonical (branch, sku) lock order, commit-free. Insufficient stock at
    # the source raises here and the whole transaction (incl. the claim above) rolls back.
    apply_ordered_movements(db, user, [
        {"sku": transfer.sku, "branch": transfer.from_branch, "mtype": "transfer_out",
         "change": -int(transfer.qty), "notes": f"Transfer {transfer.id} -> {transfer.to_branch}"},
        {"sku": transfer.sku, "branch": transfer.to_branch, "mtype": "transfer_in",
         "change": int(transfer.qty), "notes": f"Transfer {transfer.id} <- {transfer.from_branch}"},
    ])

    approval.status = "approved"
    approval.decided_by = user.name
    approval.comment = comment
    # In-transaction audit — commits together with the state change below.
    S.audit(db, user, "approved", "approval", approval.id, comment or "", commit=False)

    db.commit()   # THE single commit: claim + both stock rows + both movements + counters + audit
    return {"ok": True, "summary": approval.summary, "status": "approved"}

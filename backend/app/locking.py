"""Global lock-ordering policy (Engineering Phase 6).

Any transaction that mutates MORE THAN ONE stock row must acquire those rows in a
single canonical order, so two concurrent transactions can never hold locks in the
opposite order and deadlock. There is exactly ONE ordering function and ONE apply
helper — no locking logic is duplicated per call site.

Canonical order for stock rows: ascending ``(branch, sku)``.

Today the only multi-row stock path is an inventory transfer (two movements:
transfer_out from the source branch, transfer_in to the destination). Routing it
through ``apply_ordered_movements`` guarantees both directions of a transfer between
the same two branches lock rows in the same order.

As of TD-002 the underlying ``_write_movement`` is COMMIT-FREE and a transfer runs in a
single transaction, so this canonical ordering is what prevents opposing transfers between
the same two branches from ever acquiring their row locks in reverse order (deadlock-free).
"""


def stock_lock_key(sku, branch):
    """Canonical lock-ordering key for a stock row."""
    return (str(branch), str(sku))


def apply_ordered_movements(db, user, ops):
    """Apply a batch of stock movements in canonical lock order.

    ``ops`` = iterable of dicts: {sku, branch, mtype, change, notes?, unit_cost?}.
    Movements are applied sorted by ``stock_lock_key`` so row locks are always taken
    in the same order regardless of the caller's logical order.
    """
    from .routers.inventory import _write_movement  # lazy import avoids a cycle
    for op in sorted(ops, key=lambda o: stock_lock_key(o["sku"], o["branch"])):
        _write_movement(
            db, user, op["sku"], op["branch"], op["mtype"], op["change"],
            notes=op.get("notes", ""), unit_cost=op.get("unit_cost"),
        )

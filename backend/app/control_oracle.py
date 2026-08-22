"""Independent source-of-record oracles for the Financial Control Center.

WHY THIS MODULE EXISTS SEPARATELY
---------------------------------
BF-CC-01. The Control Center's Accounting checks used to recompute each reported
figure with the *same* helper that produced it: `_costs_profit` called `_sum`,
and the check then called `_sum` again and asserted the two agreed. A defect
inside `_sum` moved both sides of the comparison together, so mutating `_sum` to
double every result left the live endpoint reporting revenue 3000.00 against a
ledger genuinely holding 1500.00 — and still returning "Healthy", critical 0.

A reconciliation check is only worth its name if the two sides are derived
INDEPENDENTLY. Every query below reads persisted rows directly, with company,
branch, type and posting-period predicates stated inline at the call site.

This module must never import the production aggregation helpers. That is not a
convention — `tests/test_bfcc01_control_independence.py` parses this file's AST
and fails if it imports `app.routers.core`/`app.routers.control` or calls any
denylisted production helper. Independence is a test failure, not a promise.

SCOPE HONESTY
-------------
These are OPERATIONAL-LEDGER AGGREGATION oracles. They prove a reported total
equals the sum of the persisted rows it claims to summarise. They do NOT prove
double-entry correctness, transaction completeness, costing, or inventory
valuation — none of which exist in this system yet. Checks built on them must be
named as aggregation controls, never as full accounting reconciliation.
"""
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import func

from . import models

CENT = Decimal("0.01")


def to_cents(v) -> Decimal:
    """Normalise to exactly two decimal places, half-up.

    Comparisons are done on Decimal, not float: float(0.1)+float(0.2) != 0.3 and
    a cent-level reconciliation must not fail (or pass) on binary rounding.
    """
    if v is None:
        v = 0
    if not isinstance(v, Decimal):
        v = Decimal(str(v))
    return v.quantize(CENT, rounding=ROUND_HALF_UP)


def ledger_total(db, *, company_id, branches, typ, d0, d1, column="amount") -> Decimal:
    """Sum persisted `ledger` rows directly.

    Every predicate is explicit and stated here rather than inherited from a
    shared helper: company, branch, row type, and the posting-period window.
    `company_id=None` means "do not constrain company" and is only for callers
    that have already proven a single-tenant context.
    """
    col = models.Ledger.tax if column == "tax" else models.Ledger.amount
    q = db.query(func.coalesce(func.sum(col), 0)).filter(
        models.Ledger.type == typ,
        models.Ledger.branch.in_(branches),
        models.Ledger.entry_date >= d0,
        models.Ledger.entry_date <= d1,
    )
    if company_id is not None:
        q = q.filter(models.Ledger.company_id == company_id)
    return to_cents(q.scalar())


def purchases_total(db, *, company_id, branches, d0, d1) -> Decimal:
    """Sum persisted non-rejected `purchases` rows directly.

    This is a PURCHASE TOTAL. It is deliberately not named COGS: cost of goods
    sold is inventory-derived and this system has no costing layer, so calling
    purchases "COGS" would certify a known-wrong definition (SIM-04).
    """
    q = db.query(func.coalesce(func.sum(models.Purchase.amount), 0)).filter(
        models.Purchase.branch.in_(branches),
        models.Purchase.status != "rejected",
        models.Purchase.purchase_date >= d0,
        models.Purchase.purchase_date <= d1,
    )
    if company_id is not None:
        q = q.filter(models.Purchase.company_id == company_id)
    return to_cents(q.scalar())


def payroll_linkage_defects(db, *, company_id, branches, d0, d1):
    """Reconcile every payroll source document to exactly one payroll posting.

    For each `payroll_runs` row finalized in the window, there must be exactly
    one `ledger` row identified by `payroll_runs.ledger_id`, and that posting
    must agree on company, branch and amount. Returns a list of human-readable
    defects; empty means every source reconciled.

    This is the one-to-one linkage AA required: a period total can agree while
    individual runs point at the wrong posting, or at none.
    """
    defects = []
    runs = db.query(models.PayrollRun).filter(
        models.PayrollRun.branch.in_(branches),
        models.PayrollRun.period_start >= d0,
        models.PayrollRun.period_end <= d1,
    )
    if company_id is not None:
        runs = runs.filter(models.PayrollRun.company_id == company_id)

    for r in runs.all():
        tag = f"payroll_run {r.branch} {r.period_start}..{r.period_end}"
        if r.ledger_id is None:
            defects.append(f"{tag}: no linked posting (ledger_id is NULL)")
            continue
        posts = db.query(models.Ledger).filter(models.Ledger.id == r.ledger_id).all()
        if len(posts) != 1:
            defects.append(f"{tag}: ledger_id {r.ledger_id} matched {len(posts)} postings")
            continue
        p = posts[0]
        if p.company_id != r.company_id:
            defects.append(
                f"{tag}: company mismatch (source {r.company_id} vs posting {p.company_id})")
        if p.branch != r.branch:
            defects.append(f"{tag}: branch mismatch (source {r.branch} vs posting {p.branch})")
        if to_cents(p.amount) != to_cents(r.gross):
            defects.append(
                f"{tag}: gross mismatch (source {to_cents(r.gross)} vs posting {to_cents(p.amount)})")
        if p.type != "payroll":
            defects.append(f"{tag}: linked posting is type '{p.type}', not 'payroll'")
    return defects


def stock_current_cost_extension(db, *, company_id, branches):
    """Extend on-hand quantity by the product's CURRENT cost.

    Named precisely. This is NOT inventory valuation: it uses whatever cost the
    product carries right now, so it cannot reflect what stock was actually
    bought for, and it has no concept of valued movements, returns or GL
    reconciliation. True valuation stays UNSUPPORTED until a costing layer
    exists.

    Returns (extension, negative_qty_rows, negative_cost_rows).
    """
    # The join must carry company as well as sku. Product is tenant-owned, so
    # joining on sku alone would pair one company's stock with another company's
    # cost — and multiply rows whenever two tenants share a SKU.
    join_on = models.Product.sku == models.Stock.sku
    if company_id is not None:
        join_on = join_on & (models.Product.company_id == models.Stock.company_id)
    q = db.query(models.Stock.qty, models.Product.cost).join(models.Product, join_on)
    if branches is not None:
        q = q.filter(models.Stock.branch.in_(branches))
    if company_id is not None:
        q = q.filter(models.Stock.company_id == company_id)

    total = Decimal("0.00")
    neg_qty = 0
    neg_cost = 0
    for qty, cost in q.all():
        qd = Decimal(str(qty or 0))
        cd = Decimal(str(cost or 0))
        if qd < 0:
            neg_qty += 1
        if cd < 0:
            neg_cost += 1
        total += qd * cd
    return to_cents(total), neg_qty, neg_cost

"""FINANCIAL CONTROL CENTER — isolated, read-only ERP auditing and validation.

Every check in this module is a pure SELECT. It never inserts, updates or
deletes business data, never touches accounting logic, and never calls a write
endpoint. The single exception is the audit-history row written by
POST /api/control/validate, which lands in the dedicated validation_runs table.

Nothing else in the ERP imports from here, and this module changes no existing
behaviour — it only observes.
"""
import json
import time
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from .. import models, security as S, permissions as P, partners_repo as PR
from .. import control_oracle as ORACLE
from .core import _costs_profit, _period_range, _sum

router = APIRouter(prefix="/api/control", tags=["control"])

# severity ranking (worst wins)
_RANK = {"ok": 0, "warning": 1, "unsupported": 2, "error": 3, "critical": 4}
# scoring weights per severity — a critical finding costs far more than a warning
_WEIGHT = {"ok": 0, "warning": 1, "unsupported": 4, "error": 4, "critical": 10}

# Modules whose failures are financial statements about the business. A critical
# finding in one of these forces the headline verdict to "Critical" outright,
# independently of the numeric score (BF-CC-01 / C3).
_FINANCIAL_MODULES = {"Accounting", "Reports", "Dashboard"}

# Thresholds for checks that previously had no condition at all (BF-CC-01 / C2).
_PURCHASE_REVIEW_SLA_DAYS = 30   # a purchase may not sit unreviewed longer than this
_AUDIT_FRESH_DAYS = 90           # a stored validation older than this is not "current"


class _Report:
    """Collects check results grouped by module."""

    def __init__(self):
        self.sections = {}
        self.t0 = time.time()

    def add(self, module, name, ok, severity="error", detail="", cause="", fix="", value=None):
        sev = "ok" if ok else severity
        self.sections.setdefault(module, []).append({
            "check": name, "status": "pass" if ok else sev,
            "severity": sev, "detail": detail,
            "cause": "" if ok else cause, "fix": "" if ok else fix,
            "value": value, "module": module,
        })
        return ok

    def unsupported(self, module, name, detail, cause="", fix="", required=True):
        """Record a capability this system does NOT implement.

        An unsupported capability is not a pass and not a failure of the code —
        it is an honest declaration that the check cannot be performed. It is
        excluded from the passing count and, when `required`, it blocks the
        "Healthy" verdict entirely.

        This exists because deleting a check that could not really be performed
        would silently shrink the report, and leaving it as a literal `True`
        (BF-CC-01 / C2) certified a capability that was never verified. Neither
        is honest; declaring it is.
        """
        self.sections.setdefault(module, []).append({
            "check": name, "status": "unsupported", "severity": "unsupported",
            "detail": detail, "cause": cause, "fix": fix, "value": None,
            "module": module, "required": required,
        })
        return False

    def build(self, extra=None):
        checks = [c for lst in self.sections.values() for c in lst]
        passed = sum(1 for c in checks if c["status"] == "pass")
        warnings = sum(1 for c in checks if c["status"] == "warning")
        errors = sum(1 for c in checks if c["status"] == "error")
        critical = sum(1 for c in checks if c["status"] == "critical")
        unsupported = sum(1 for c in checks if c["status"] == "unsupported")
        unsupported_required = sum(
            1 for c in checks if c["status"] == "unsupported" and c.get("required"))
        critical_financial = sum(
            1 for c in checks
            if c["status"] == "critical" and c.get("module") in _FINANCIAL_MODULES)
        penalty = sum(_WEIGHT.get(c["status"], 0) for c in checks)
        max_penalty = max(1, len(checks) * _WEIGHT["critical"])
        score = round(max(0.0, 100.0 * (1 - penalty / max_penalty)), 1)
        worst = "ok"
        for c in checks:
            if _RANK.get(c["status"], 0) > _RANK.get(worst, 0):
                worst = c["status"]

        # BF-CC-01 / C3 — the headline verdict must be decided by WHAT FAILED,
        # not by how many checks happen to exist.
        #
        # The old formula was score-only: score = 100*(1 - penalty/(n*10)). With
        # 41 checks, a single critical financial failure scored 97.6 and read
        # "Attention needed", and all six critical Accounting checks failing at
        # once still read 85.4 / "Attention needed" — never "Critical". Adding
        # more passing checks made the system look healthier under identical
        # failures.
        #
        # The severity branches below are evaluated BEFORE any score comparison
        # and do not reference `score`, so adding arbitrary passing checks can
        # never restore a better verdict.
        if critical_financial:
            label = "Critical"
        elif critical:
            label = "Degraded"
        elif unsupported_required:
            # A mandatory capability we cannot verify is not health; it is an
            # incomplete audit. It must never read as "Healthy".
            label = "Incomplete"
        elif score >= 95:
            label = "Healthy"
        elif score >= 80:
            label = "Attention needed"
        elif score >= 60:
            label = "Degraded"
        else:
            label = "Critical"
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "duration_ms": int((time.time() - self.t0) * 1000),
            "score": score, "label": label, "severity": worst,
            "totals": {"checks": len(checks), "passed": passed, "warnings": warnings,
                       "errors": errors, "critical": critical,
                       "unsupported": unsupported},
            "modules": sorted({m for m, lst in self.sections.items()
                               if any(c["status"] != "pass" for c in lst)}),
            "sections": [{"module": m, "checks": lst} for m, lst in self.sections.items()],
            **(extra or {}),
        }


def _f(v):
    return float(v or 0)


def _run_all(db: Session, user) -> dict:
    """Execute every read-only validation and return the structured report."""
    R = _Report()
    today = date.today()
    branches = [b.name for b in db.query(models.Branch).all()]

    # ---------------------------------------------------------- ACCOUNTING
    try:
        d0, d1, _, _ = _period_range("month")
        cp = _costs_profit(db, branches, d0, d1)

        # ---- BF-CC-01: reported figures are reconciled against INDEPENDENT
        # ---- oracles (app/control_oracle.py), never against a second call to
        # ---- the same helper that produced them.
        #
        # The previous implementation computed `rev` with the identical
        # `_sum(db, branches, "sale", d0, d1)` call that produced cp["revenue"],
        # so any defect inside `_sum` moved both sides together and the check
        # stayed green. Mutating `_sum` to double every result left this endpoint
        # reporting revenue 3000.00 on a ledger holding 1500.00, still "Healthy".
        #
        # These checks are named as OPERATIONAL-LEDGER AGGREGATION controls. They
        # prove a reported total equals the persisted rows it claims to
        # summarise. They do not prove double-entry, completeness or valuation.
        cid = db.info.get("company_id")

        o_rev = ORACLE.ledger_total(db, company_id=cid, branches=branches,
                                    typ="sale", d0=d0, d1=d1)
        o_tax = ORACLE.ledger_total(db, company_id=cid, branches=branches,
                                    typ="sale", d0=d0, d1=d1, column="tax")
        o_opex = ORACLE.ledger_total(db, company_id=cid, branches=branches,
                                     typ="expense", d0=d0, d1=d1)
        o_pay = ORACLE.ledger_total(db, company_id=cid, branches=branches,
                                    typ="payroll", d0=d0, d1=d1)

        def _agrees(reported, oracle):
            return ORACLE.to_cents(reported) == ORACLE.to_cents(oracle)

        R.add("Accounting", "Reported sales equal posted sale rows (operational ledger)",
              _agrees(cp["revenue"], o_rev), "critical",
              f"reported {cp['revenue']:.2f} vs posted rows {o_rev}",
              "The reported sales total does not equal the sum of persisted sale rows.",
              "Recompute from ledger rows with explicit company/branch/type/period predicates.",
              cp["revenue"])
        R.add("Accounting", "Reported sales tax equals posted tax on sale rows",
              _agrees(cp["tax"], o_tax), "critical",
              f"reported {cp['tax']:.2f} vs posted rows {o_tax}",
              "The reported tax total does not equal the tax recorded on persisted sale rows.",
              "Recompute tax directly from the sale rows it was collected on.", cp["tax"])
        R.add("Accounting", "Reported expenses equal posted expense rows (operational ledger)",
              _agrees(cp["opex"], o_opex), "critical",
              f"reported {cp['opex']:.2f} vs posted rows {o_opex}",
              "The reported expense total does not equal the sum of persisted expense rows.",
              "Recompute from ledger rows of type 'expense'.", cp["opex"])
        R.add("Accounting", "Reported payroll equals posted payroll rows (operational ledger)",
              _agrees(cp["payroll"], o_pay), "critical",
              f"reported {cp['payroll']:.2f} vs posted rows {o_pay}",
              "The reported payroll total does not equal the sum of persisted payroll rows.",
              "Recompute from ledger rows of type 'payroll'.", cp["payroll"])

        # Every payroll source document must map to exactly one payroll posting.
        # A period total can agree while individual runs point at the wrong
        # posting, or at none at all.
        pay_defects = ORACLE.payroll_linkage_defects(
            db, company_id=cid, branches=branches, d0=d0, d1=d1)
        R.add("Accounting", "Each payroll run links to exactly one payroll posting",
              not pay_defects, "critical",
              "; ".join(pay_defects[:3]) if pay_defects else "every payroll run reconciled",
              "A payroll source document does not reconcile one-to-one with its posting.",
              "Reconcile payroll_runs.ledger_id on company, branch and gross.",
              len(pay_defects))

        # Purchase total — deliberately NOT called COGS. See the unsupported
        # declaration below.
        o_pur = ORACLE.purchases_total(db, company_id=cid, branches=branches, d0=d0, d1=d1)
        R.add("Accounting", "Reported purchase total equals posted purchase rows",
              _agrees(cp["cogs"], o_pur), "error",
              f"reported {cp['cogs']:.2f} vs posted rows {o_pur}",
              "The reported purchase total does not equal the sum of non-rejected purchase rows.",
              "Recompute from the purchases table; do not treat this figure as COGS.",
              cp["cogs"])

        # COGS itself cannot be verified: this system has no costing layer, no
        # valued stock movements and no inventory-derived cost of sale. The old
        # check asserted at severity `critical` that "COGS must sum non-rejected
        # purchases for the period" — certifying a known-wrong definition
        # (SIM-04) as correct. Declaring it unverifiable is the honest state.
        R.unsupported(
            "Accounting", "Cost of goods sold reconciles to inventory movement",
            "UNSUPPORTED / NOT TESTED — purchases are not COGS and no costing layer exists.",
            "COGS requires inventory-derived cost of sale: valued movements, a costing "
            "method, returns and GL reconciliation. None are implemented.",
            "Do not report purchases as COGS. Implement costing before claiming this.")

        # Internal algebraic consistency. These are NOT source reconciliation:
        # they restate the definitions in _costs_profit and can only catch a
        # divergence between the formula and its restatement. Named and scored
        # accordingly (QA rule 5) — they may not carry critical reconciliation
        # semantics.
        R.add("Accounting", "Internal consistency: costs equal their components",
              abs(cp["costs"] - (cp["cogs"] + cp["opex"] + cp["payroll"])) < 0.01,
              "warning", f"costs {cp['costs']:.2f}",
              "The cost total does not equal the sum of its own components.",
              "Review the canonical cost formula.", cp["costs"])
        R.add("Accounting", "Internal consistency: profit follows its definition",
              abs(cp["profit"] - (cp["revenue"] - cp["tax"] - cp["costs"])) < 0.01,
              "warning", f"profit {cp['profit']:.2f}",
              "Profit does not follow its own stated formula.",
              "Profit must equal revenue − sales tax − total costs.", cp["profit"])
        R.add("Accounting", "Sales tax never exceeds its sale", cp["tax"] <= cp["revenue"] + 0.01,
              "critical", f"tax {cp['tax']:.2f} vs revenue {cp['revenue']:.2f}",
              "Recorded tax exceeds the revenue it was collected on.",
              "Reject postings where tax > amount.", cp["tax"])

        eff = (cp["tax"] / cp["revenue"] * 100) if cp["revenue"] else 0
        R.add("Accounting", "Effective tax rate within a sane band (0–20%)", 0 <= eff <= 20,
              "warning", f"effective rate {eff:.2f}%",
              "Tax rate outside the expected retail band.",
              "Check the tax basis used when posting daily sales.", round(eff, 2))

        # Metamorphic partition check: the per-branch parts must sum to the whole.
        # PRESERVED — it is genuinely useful and it is what caught the branch-scope
        # defect during BF-CC-01 investigation. But it is NOT an independent source
        # oracle: both sides run through the same `_costs_profit`/`_sum` tree, so it
        # can only detect partitioning errors, never total correctness. Do not
        # present it, or test it, as an oracle.
        per = 0.0
        for b in branches:
            per += _costs_profit(db, [b], d0, d1)["costs"]
        R.add("Accounting", "Per-branch costs sum to all-branches (partition check)",
              abs(per - cp["costs"]) < 0.5,
              "error", f"branches {per:.2f} vs all {cp['costs']:.2f}",
              "Branch scoping is dropping or double-counting rows.",
              "Verify branch filters in the aggregation.", round(per, 2))

        # Current-cost stock extension — precisely named. This extends on-hand
        # quantity by the product's CURRENT cost. It is not inventory valuation:
        # it cannot reflect what stock was actually bought for, and has no valued
        # movements, returns or GL reconciliation behind it.
        ext, neg_qty, neg_cost = ORACLE.stock_current_cost_extension(
            db, company_id=cid, branches=branches)
        R.add("Accounting", "Current-cost stock extension is non-negative",
              ext >= 0 and neg_qty == 0 and neg_cost == 0, "error",
              f"extension {ext} · {neg_qty} negative-qty rows · {neg_cost} negative-cost rows",
              "Stock quantity or product cost is negative, so the extension is not meaningful.",
              "Inspect stock rows and product costs for negative values.", float(ext))

        R.unsupported(
            "Accounting", "Inventory is valued on a defined costing basis",
            "UNSUPPORTED / NOT TESTED — no costing layer, valued movements or GL reconciliation.",
            "True valuation requires a costing method (FIFO/average/standard), valued stock "
            "movements, returns handling and reconciliation to the ledger. None exist.",
            "Implement costing architecture before reporting an inventory valuation.")
    except Exception as e:  # noqa: BLE001
        R.add("Accounting", "Accounting checks executed", False, "critical", str(e)[:200],
              "The accounting validation itself failed.", "Inspect the API logs.")

    # ---------------------------------------------------------- DATABASE
    try:
        orphan_stock = db.query(models.Stock).filter(
            ~models.Stock.sku.in_(db.query(models.Product.sku))).count()
        R.add("Database", "No orphan stock rows", orphan_stock == 0, "error",
              f"{orphan_stock} rows", "Stock rows reference a missing product.",
              "Delete orphan stock rows or restore the product.", orphan_stock)

        orphan_mov = db.query(models.Movement).filter(
            ~models.Movement.sku.in_(db.query(models.Product.sku))).count()
        R.add("Database", "No orphan movements", orphan_mov == 0, "error",
              f"{orphan_mov} rows", "Movements reference a missing product.",
              "Restore the product or archive the movements.", orphan_mov)

        orphan_ub = db.query(models.UserBranch).filter(
            ~models.UserBranch.user_id.in_(db.query(models.User.id))).count()
        R.add("Database", "No orphan user-branch rows", orphan_ub == 0, "error",
              f"{orphan_ub} rows", "Branch assignment references a deleted user.",
              "Remove the stale assignment.", orphan_ub)

        bad_branch = db.query(models.Ledger).filter(
            ~models.Ledger.branch.in_(db.query(models.Branch.name))).count()
        R.add("Database", "Ledger branches all exist", bad_branch == 0, "error",
              f"{bad_branch} rows", "Ledger rows point at an unknown branch.",
              "Rename/restore the branch or correct the rows.", bad_branch)

        neg_amt = db.query(models.Ledger).filter(models.Ledger.amount < 0).count()
        R.add("Database", "No negative ledger amounts", neg_amt == 0, "critical",
              f"{neg_amt} rows", "Negative postings corrupt totals.",
              "Reject negative amounts at the API and correct existing rows.", neg_amt)

        bad_tax = db.query(models.Ledger).filter(models.Ledger.tax > models.Ledger.amount).count()
        R.add("Database", "No row with tax greater than amount", bad_tax == 0, "critical",
              f"{bad_tax} rows", "Impossible tax value.",
              "Reject tax > amount and correct affected rows.", bad_tax)

        dup_pur = db.query(models.Purchase.id).group_by(models.Purchase.id) \
            .having(func.count() > 1).count()
        R.add("Database", "No duplicate purchase ids", dup_pur == 0, "error",
              f"{dup_pur}", "Primary key collision.", "De-duplicate purchases.", dup_pur)

        bad_clock = db.query(models.Attendance).filter(
            models.Attendance.clock_out_at.isnot(None),
            models.Attendance.clock_out_at < models.Attendance.clock_in_at).count()
        R.add("Database", "No attendance ending before it started", bad_clock == 0, "error",
              f"{bad_clock} rows", "Invalid timestamp ordering.",
              "Correct the affected attendance rows.", bad_clock)

        future = db.query(models.Ledger).filter(
            models.Ledger.entry_date > today + timedelta(days=1)).count()
        R.add("Database", "No future-dated ledger entries", future == 0, "warning",
              f"{future} rows", "Entries dated in the future skew period reports.",
              "Correct the entry dates.", future)
    except Exception as e:  # noqa: BLE001
        R.add("Database", "Database checks executed", False, "critical", str(e)[:200],
              "The database validation itself failed.", "Inspect the API logs.")

    # ---------------------------------------------------------- INVENTORY
    try:
        neg = db.query(models.Stock).filter(models.Stock.qty < 0).count()
        R.add("Inventory", "No negative stock", neg == 0, "critical", f"{neg} rows",
              "Stock fell below zero.", "Reject over-decrements at the API.", neg)

        broken = db.query(models.Movement).filter(
            models.Movement.qty_before + models.Movement.qty_change != models.Movement.qty_after
        ).count()
        R.add("Inventory", "Movement ledger invariant (before + change = after)", broken == 0,
              "critical", f"{broken} rows",
              "Movements were clamped or written non-atomically.",
              "Never clamp; derive before from after − change.", broken)

        # BF-12: stock must equal its movement history PER (SKU, BRANCH).
        # Grouping by SKU alone summed every branch together, so +5 in one branch
        # and -5 in another cancelled to zero and the check reported pass over real
        # corruption — precisely what it exists to catch. Severity is `error`, not
        # `warning`: stock that disagrees with its own ledger must fail the gate.
        sums = {(s, b): int(v or 0) for s, b, v in db.query(
            models.Movement.sku, models.Movement.branch,
            func.coalesce(func.sum(models.Movement.qty_change), 0)
        ).group_by(models.Movement.sku, models.Movement.branch).all()}
        cur = {(s, b): int(v or 0) for s, b, v in db.query(
            models.Stock.sku, models.Stock.branch,
            func.coalesce(func.sum(models.Stock.qty), 0)
        ).group_by(models.Stock.sku, models.Stock.branch).all()}
        drift = sorted(k for k in set(sums) | set(cur)
                       if cur.get(k, 0) != sums.get(k, 0))
        drift_labels = [f"{s}@{b}" for s, b in drift[:5]]
        R.add("Inventory", "Stock equals movement history", not drift, "error",
              f"{len(drift)} SKU/branch pairs differ: {', '.join(drift_labels)}",
              "Opening balances were seeded without matching movements, or a write bypassed the ledger. "
              "Offsetting errors in different branches do NOT cancel out.",
              "Post an opening-balance movement, or reconcile the affected SKU/branch pairs.", len(drift))

        # BF-13: verify each transfer as an identified PAIR, not as two global counts.
        # count(transfer_in) == count(transfer_out) was satisfied by any two rows at
        # all — wrong SKU, wrong branches, wrong quantity, or two unrelated broken
        # transfers cancelling each other.
        legs = db.query(models.Movement).filter(
            models.Movement.type.in_(("transfer_in", "transfer_out"))).all()
        legacy = [m for m in legs if not m.transfer_id]
        pairs = {}
        for m in legs:
            if m.transfer_id:
                pairs.setdefault(m.transfer_id, []).append(m)

        broken_pairs = []
        for tid, ms in pairs.items():
            outs = [m for m in ms if m.type == "transfer_out"]
            ins = [m for m in ms if m.type == "transfer_in"]
            if len(outs) != 1 or len(ins) != 1:
                broken_pairs.append(f"{tid}: {len(outs)} out / {len(ins)} in")
                continue
            o, i = outs[0], ins[0]
            # transfers use a surrogate row_id PK; `tid` is the tenant-scoped
            # BUSINESS number. db.get(Transfer, "TR-000001") would compare a string
            # against a bigint PK — silently None on SQLite, a DataError on
            # PostgreSQL that poisons the whole transaction. Resolve it the way the
            # rest of the app does.
            t = PR.get_transfer(db, tid)
            if o.sku != i.sku:
                broken_pairs.append(f"{tid}: SKU {o.sku} out vs {i.sku} in")
            elif int(o.qty_change) != -int(i.qty_change):
                broken_pairs.append(f"{tid}: qty {o.qty_change} out vs {i.qty_change} in")
            elif o.branch == i.branch:
                broken_pairs.append(f"{tid}: both legs in {o.branch}")
            elif t is not None and (o.branch != t.from_branch or i.branch != t.to_branch
                                    or o.sku != t.sku or abs(int(o.qty_change)) != int(t.qty)):
                broken_pairs.append(
                    f"{tid}: movements {o.branch}->{i.branch} {o.sku} x{abs(int(o.qty_change))} "
                    f"do not match transfer {t.from_branch}->{t.to_branch} {t.sku} x{t.qty}")

        R.add("Inventory", "Approved transfers moved stock both ways", not broken_pairs,
              "error",
              f"{len(pairs)} identified transfers · {len(broken_pairs)} mismatched",
              "A transfer's two legs disagree on source, destination, SKU or quantity, "
              "or a leg is missing entirely.",
              "Approval must write a paired out/in movement in ONE transaction.",
              {"transfers": len(pairs), "mismatched": broken_pairs[:5]})

        # Rows written before transfer_id existed cannot be verified. Report that
        # honestly as its own finding rather than back-filling an invented identity,
        # which would manufacture the evidence this check exists to look for.
        R.add("Inventory", "All transfer movements carry a transfer identity",
              not legacy, "warning",
              f"{len(legacy)} legacy movement rows predate transfer identity",
              "These rows cannot be verified as pairs; they are excluded from the check above.",
              "Reconcile them manually, or accept them as pre-upgrade history.", len(legacy))

        # BF-CC-01 / C2 — this was `R.add(..., True, ...)`: an informational
        # counter that always reported "pass" whatever the queue looked like.
        # It now asserts something real: nothing may sit unreviewed indefinitely.
        pend = db.query(models.Purchase).filter(
            models.Purchase.status == "pending_approval").count()
        stale_cut = today - timedelta(days=_PURCHASE_REVIEW_SLA_DAYS)
        stale = db.query(models.Purchase).filter(
            models.Purchase.status == "pending_approval",
            models.Purchase.purchase_date < stale_cut).count()
        R.add("Inventory", "No purchase awaits review beyond the review window",
              stale == 0, "warning",
              f"{pend} awaiting approval · {stale} older than {_PURCHASE_REVIEW_SLA_DAYS} days",
              "Purchases have been awaiting approval past the review window.",
              "Review or reject the outstanding purchases.", stale)
    except Exception as e:  # noqa: BLE001
        R.add("Inventory", "Inventory checks executed", False, "critical", str(e)[:200],
              "The inventory validation itself failed.", "Inspect the API logs.")

    # ---------------------------------------------------------- SECURITY
    try:
        R.add("Security", "JWT secret configured",
              bool(settings.jwt_secret) and len(str(settings.jwt_secret)) >= 16, "critical",
              "secret length checked (value never exposed)",
              "A short or missing signing secret allows token forgery.",
              "Set a long random JWT_SECRET.")
        R.add("Security", "JWT expiry configured", int(settings.jwt_expire_minutes or 0) > 0,
              "error", f"{settings.jwt_expire_minutes} min",
              "Tokens never expire.", "Set JWT_EXPIRE_MINUTES.")
        R.add("Security", "RBAC roles defined", len(P.PERMS) >= 5, "critical",
              f"{len(P.PERMS)} roles / {len(P.ALL_PERMS)} permissions",
              "Role map missing.", "Restore the permission matrix.")
        R.add("Security", "Least privilege: employee cannot view cost or profit",
              not P.can("employee", "view_cost") and not P.can("employee", "view_profit"),
              "critical", "employee role inspected",
              "A low-privilege role can read margins.",
              "Remove view_cost/view_profit from that role.")
        R.add("Security", "Least privilege: cashier cannot run payroll",
              not P.can("cashier", "run_payroll"), "error", "cashier role inspected",
              "Cashier can run payroll.", "Remove run_payroll from cashier.")
        R.add("Security", "Telegram bot endpoints are token-gated", bool(settings.bot_token),
              "error", "bot token configured",
              "Bot-only endpoints would be open.", "Set TELEGRAM_BOT_TOKEN on the API.")
        aud = db.query(models.AuditLog).count()
        recent = db.query(models.AuditLog).filter(
            models.AuditLog.ts >= datetime.now(timezone.utc) - timedelta(days=30)).count()
        R.add("Security", "Audit log is recording", aud > 0, "error", f"{aud} entries ({recent} in 30d)",
              "No audit trail.", "Ensure S.audit() runs on write endpoints.", aud)
        # BF-CC-01 / C2 — this was `R.add(..., True, ...)`, so it reported "pass"
        # with the detail "no previous run recorded": the name asserted existence
        # while the detail admitted absence. The condition is now real, and a
        # stale audit is not treated as a current one.
        last = db.query(models.ValidationRun).order_by(models.ValidationRun.ts.desc()).first()
        if last is None:
            R.add("Security", "A previous validation run is on record", False, "warning",
                  "no previous run recorded",
                  "No validation has ever been stored, so there is no audit history.",
                  "Run and save a full validation to establish history.", None)
        else:
            # SQLite returns naive datetimes while PostgreSQL returns aware ones.
            # Subtracting across the two raises, and the section's broad `except`
            # would swallow that into a single "Security checks executed" critical
            # — losing every other security check with it.
            age_days = None
            if last.ts is not None:
                ts = last.ts if last.ts.tzinfo else last.ts.replace(tzinfo=timezone.utc)
                age_days = (datetime.now(timezone.utc) - ts).days
            fresh = age_days is not None and age_days <= _AUDIT_FRESH_DAYS
            R.add("Security", "A previous validation run is on record", fresh, "warning",
                  f"last run {last.ts}" + (f" ({age_days}d ago)" if age_days is not None else ""),
                  f"The most recent validation is older than {_AUDIT_FRESH_DAYS} days.",
                  "Run a full validation to refresh the audit history.", str(last.ts))
    except Exception as e:  # noqa: BLE001
        R.add("Security", "Security checks executed", False, "critical", str(e)[:200],
              "The security validation itself failed.", "Inspect the API logs.")

    # ---------------------------------------------------------- PERFORMANCE
    timings = []
    try:
        def timed(label, fn):
            t = time.time()
            fn()
            ms = round((time.time() - t) * 1000, 1)
            timings.append({"endpoint": label, "ms": ms})
            return ms

        d0m, d1m, _, _ = _period_range("month")
        timed("reports/kpi (month)", lambda: _costs_profit(db, branches, d0m, d1m))
        timed("ledger scan", lambda: db.query(func.count(models.Ledger.id)).scalar())
        timed("movement scan", lambda: db.query(func.count(models.Movement.id)).scalar())
        timed("product+stock join", lambda: db.query(func.count(models.Stock.sku))
              .join(models.Product, models.Product.sku == models.Stock.sku).scalar())
        slowest = max(timings, key=lambda t: t["ms"]) if timings else {"endpoint": "-", "ms": 0}
        avg = round(sum(t["ms"] for t in timings) / max(1, len(timings)), 1)
        R.add("Performance", "Average query time under 500ms", avg < 500, "warning",
              f"average {avg}ms across {len(timings)} probes",
              "Queries are slowing down.", "Add indexes or reduce scanned rows.", avg)
        R.add("Performance", "Slowest probe under 2000ms", slowest["ms"] < 2000, "warning",
              f"{slowest['endpoint']} at {slowest['ms']}ms",
              "One query dominates response time.",
              "Index the columns used by that query.", slowest)
    except Exception as e:  # noqa: BLE001
        R.add("Performance", "Performance probes executed", False, "warning", str(e)[:200],
              "Timing probes failed.", "Inspect the API logs.")

    tables = []
    try:
        for label, model in [("ledger", models.Ledger), ("movements", models.Movement),
                             ("purchases", models.Purchase), ("stock", models.Stock),
                             ("products", models.Product), ("attendance", models.Attendance),
                             ("audit_log", models.AuditLog), ("employees", models.Employee),
                             ("licenses", models.License), ("validation_runs", models.ValidationRun)]:
            tables.append({"table": label, "rows": db.query(func.count()).select_from(model).scalar() or 0})
        tables.sort(key=lambda t: -t["rows"])
    except Exception:  # noqa: BLE001
        pass

    # ------------------------------------------- REPORTS / DASHBOARD CONSISTENCY
    try:
        dt0, dt1, _, _ = _period_range("today")
        today_cp = _costs_profit(db, branches, dt0, dt1)
        sales_today = _sum(db, branches, "sale", dt0, dt1)
        exp_today = _sum(db, branches, "expense", dt0, dt1)

        # BF-CC-01 / C1 — the same tautology as the Accounting section: these
        # compared `today_cp[...]` against a second call to the identical `_sum`,
        # or restated the profit formula. Anchored to the independent oracle, and
        # the purely algebraic one renamed and dropped from critical.
        r_cid = db.info.get("company_id")
        o_sales_t = ORACLE.ledger_total(db, company_id=r_cid, branches=branches,
                                        typ="sale", d0=dt0, d1=dt1)
        o_exp_t = ORACLE.ledger_total(db, company_id=r_cid, branches=branches,
                                      typ="expense", d0=dt0, d1=dt1)

        R.add("Reports", "Internal consistency: dashboard and KPI profit formulas agree",
              abs(today_cp["profit"] - (sales_today - today_cp["tax"] - today_cp["costs"])) < 0.01,
              "warning", f"profit {today_cp['profit']:.2f}",
              "Dashboard and KPI use different profit formulas.",
              "Both must use the canonical definition.", round(today_cp["profit"], 2))
        R.add("Reports", "Daily report sales equal posted sale rows",
              ORACLE.to_cents(today_cp["revenue"]) == o_sales_t, "error",
              f"reported {today_cp['revenue']:.2f} vs posted rows {o_sales_t}",
              "The daily report total does not equal the persisted sale rows.",
              "Recompute the report total from ledger rows.", round(sales_today, 2))
        R.add("Reports", "Expense figures equal posted expense rows",
              ORACLE.to_cents(today_cp["opex"]) == o_exp_t, "error",
              f"reported {today_cp['opex']:.2f} vs posted rows {o_exp_t}",
              "The reported expense figure does not equal the persisted expense rows.",
              "Recompute from ledger rows of type 'expense'.", round(exp_today, 2))

        m0, m1, p0, p1 = _period_range("month")
        cur_m = _costs_profit(db, branches, m0, m1)
        cat = db.query(func.coalesce(func.sum(models.Ledger.amount), 0)).filter(
            models.Ledger.type == "expense", models.Ledger.branch.in_(branches),
            models.Ledger.entry_date >= m0, models.Ledger.entry_date <= m1).scalar() or 0
        R.add("Reports", "Expenses-by-category sums to month expenses",
              abs(_f(cat) - cur_m["opex"]) < 0.01, "error",
              f"{_f(cat):.2f} vs {cur_m['opex']:.2f}",
              "Chart aggregation drifted from the ledger.",
              "Recompute the category chart from ledger rows.", round(_f(cat), 2))

        # BF-CC-01 / C2 — both of these were `R.add(..., True, ...)`: assertions
        # about where the numbers come from that were never actually evaluated.
        #
        # This one is now a real comparison against the independent oracle.
        d_cid = db.info.get("company_id")
        o_sales_today = ORACLE.ledger_total(db, company_id=d_cid, branches=branches,
                                            typ="sale", d0=dt0, d1=dt1)
        R.add("Dashboard", "Dashboard sales total equals posted sale rows",
              ORACLE.to_cents(sales_today) == o_sales_today, "error",
              f"dashboard {sales_today:.2f} vs posted rows {o_sales_today}",
              "The dashboard sales figure does not equal the persisted sale rows.",
              "Recompute the dashboard total from ledger rows.", round(sales_today, 2))

        # Export equivalence cannot be verified from the backend: the dashboard
        # PDF/Excel export is produced client-side from the API payload, and no
        # server-side export endpoint exists to compare against. Asserting it
        # `True` claimed a guarantee nobody checked; declaring it unverifiable is
        # the honest state.
        R.unsupported(
            "Dashboard", "Export totals match API totals (PDF/Excel source data)",
            "UNSUPPORTED / NOT TESTED — exports are generated client-side; no server-side "
            "export exists to compare against.",
            "The backend cannot observe what the browser rendered into a PDF or spreadsheet.",
            "Verify export equivalence in a browser-driven UI test, or add a server-side "
            "export endpoint that can be compared directly.")
    except Exception as e:  # noqa: BLE001
        R.add("Reports", "Report consistency checks executed", False, "critical", str(e)[:200],
              "The report validation itself failed.", "Inspect the API logs.")

    return R.build({"performance": {"timings": timings, "tables": tables[:10]},
                    "scope": {"branches": branches, "read_only": True}})


@router.get("/validate")
def validate(db: Session = Depends(get_db),
             user: models.User = Depends(S.require("view_all_branches"))):
    """Run every validation and return the report. Writes nothing at all."""
    return _run_all(db, user)


@router.post("/validate")
def validate_and_store(db: Session = Depends(get_db),
                       user: models.User = Depends(S.require("view_all_branches"))):
    """Run every validation and store the result in the audit history.
    The only write is the validation_runs row."""
    rep = _run_all(db, user)
    row = models.ValidationRun(
        user_id=user.id, score=rep["score"], passed=rep["totals"]["passed"],
        warnings=rep["totals"]["warnings"], errors=rep["totals"]["errors"],
        critical=rep["totals"]["critical"], duration_ms=rep["duration_ms"],
        modules=",".join(rep["modules"]), severity=rep["severity"],
        report=json.dumps(rep))
    db.add(row)
    db.commit()
    S.audit(db, user, "run_validation", "control", row.id,
            f"score {rep['score']} · {rep['totals']['critical']} critical")
    rep["run_id"] = row.id
    return rep


@router.get("/history")
def history(days: int = 90, severity: str = "all", module: str = "all", limit: int = 50,
            db: Session = Depends(get_db),
            user: models.User = Depends(S.require("view_all_branches"))):
    q = db.query(models.ValidationRun).filter(
        models.ValidationRun.ts >= datetime.now(timezone.utc) - timedelta(days=max(1, days)))
    if severity != "all":
        q = q.filter(models.ValidationRun.severity == severity)
    if module != "all":
        q = q.filter(models.ValidationRun.modules.like(f"%{module}%"))
    rows = q.order_by(models.ValidationRun.ts.desc()).limit(min(limit, 200)).all()
    return [{"id": r.id, "ts": str(r.ts), "user": r.user_id, "score": float(r.score or 0),
             "passed": r.passed, "warnings": r.warnings, "errors": r.errors,
             "critical": r.critical, "duration_ms": r.duration_ms,
             "severity": r.severity, "modules": (r.modules or "").split(",") if r.modules else []}
            for r in rows]


@router.get("/history/{rid}")
def history_detail(rid: int, db: Session = Depends(get_db),
                   user: models.User = Depends(S.require("view_all_branches"))):
    r = db.get(models.ValidationRun, rid)
    if not r:
        raise HTTPException(404, "Validation run not found")
    try:
        return json.loads(r.report or "{}")
    except Exception:  # noqa: BLE001
        raise HTTPException(500, "Stored report could not be parsed")

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
from .. import models, security as S, permissions as P
from .core import _costs_profit, _period_range, _purchases_sum, _sum

router = APIRouter(prefix="/api/control", tags=["control"])

# severity ranking (worst wins)
_RANK = {"ok": 0, "warning": 1, "error": 2, "critical": 3}
# scoring weights per severity — a critical finding costs far more than a warning
_WEIGHT = {"ok": 0, "warning": 1, "error": 4, "critical": 10}


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
            "value": value,
        })
        return ok

    def build(self, extra=None):
        checks = [c for lst in self.sections.values() for c in lst]
        passed = sum(1 for c in checks if c["status"] == "pass")
        warnings = sum(1 for c in checks if c["status"] == "warning")
        errors = sum(1 for c in checks if c["status"] == "error")
        critical = sum(1 for c in checks if c["status"] == "critical")
        penalty = sum(_WEIGHT.get(c["status"], 0) for c in checks)
        max_penalty = max(1, len(checks) * _WEIGHT["critical"])
        score = round(max(0.0, 100.0 * (1 - penalty / max_penalty)), 1)
        worst = "ok"
        for c in checks:
            if _RANK.get(c["status"], 0) > _RANK.get(worst, 0):
                worst = c["status"]
        label = ("Healthy" if score >= 95 and critical == 0 else
                 "Attention needed" if score >= 80 else
                 "Degraded" if score >= 60 else "Critical")
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "duration_ms": int((time.time() - self.t0) * 1000),
            "score": score, "label": label, "severity": worst,
            "totals": {"checks": len(checks), "passed": passed, "warnings": warnings,
                       "errors": errors, "critical": critical},
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

        rev = _sum(db, branches, "sale", d0, d1)
        tax = _sum(db, branches, "sale", d0, d1, "tax")
        opex = _sum(db, branches, "expense", d0, d1)
        pay = _sum(db, branches, "payroll", d0, d1)
        cogs = _purchases_sum(db, branches, d0, d1) + _sum(db, branches, "purchase", d0, d1)

        R.add("Accounting", "Sales total reconciles to ledger", abs(cp["revenue"] - rev) < 0.01,
              "critical", f"revenue {cp['revenue']:.2f} vs ledger {rev:.2f}",
              "Revenue aggregation diverged from the ledger.",
              "Recompute revenue directly from ledger rows of type 'sale'.", cp["revenue"])
        R.add("Accounting", "Expense total reconciles to ledger", abs(cp["opex"] - opex) < 0.01,
              "critical", f"{cp['opex']:.2f} vs {opex:.2f}",
              "Expense aggregation diverged.", "Recompute from ledger type 'expense'.", cp["opex"])
        R.add("Accounting", "Payroll total reconciles to ledger", abs(cp["payroll"] - pay) < 0.01,
              "critical", f"{cp['payroll']:.2f} vs {pay:.2f}",
              "Payroll aggregation diverged.", "Recompute from ledger type 'payroll'.", cp["payroll"])
        R.add("Accounting", "COGS reconciles to purchases", abs(cp["cogs"] - cogs) < 0.01,
              "critical", f"{cp['cogs']:.2f} vs {cogs:.2f}",
              "Cost of goods no longer matches the purchases table.",
              "COGS must sum non-rejected purchases for the period.", cp["cogs"])
        R.add("Accounting", "Costs = COGS + expenses + payroll",
              abs(cp["costs"] - (cp["cogs"] + cp["opex"] + cp["payroll"])) < 0.01,
              "critical", f"costs {cp['costs']:.2f}",
              "Cost components do not sum to total costs.",
              "Review the canonical cost formula.", cp["costs"])
        R.add("Accounting", "Profit = revenue − tax − costs",
              abs(cp["profit"] - (cp["revenue"] - cp["tax"] - cp["costs"])) < 0.01,
              "critical", f"profit {cp['profit']:.2f}",
              "Profit does not follow the canonical definition.",
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

        # branch figures must sum to the all-branches figure
        per = 0.0
        for b in branches:
            per += _costs_profit(db, [b], d0, d1)["costs"]
        R.add("Accounting", "Per-branch costs sum to all-branches", abs(per - cp["costs"]) < 0.5,
              "error", f"branches {per:.2f} vs all {cp['costs']:.2f}",
              "Branch scoping is dropping or double-counting rows.",
              "Verify branch filters in the aggregation.", round(per, 2))

        # inventory valuation
        inv = db.query(func.coalesce(func.sum(models.Stock.qty * models.Product.cost), 0)) \
            .join(models.Product, models.Product.sku == models.Stock.sku).scalar() or 0
        R.add("Accounting", "Inventory valuation computable", _f(inv) >= 0,
              "error", f"stock at cost {_f(inv):.2f}",
              "Inventory valued below zero.", "Inspect product costs and stock rows.", round(_f(inv), 2))
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

        # stock must equal the sum of its movement history
        sums = dict(db.query(models.Movement.sku, func.coalesce(func.sum(models.Movement.qty_change), 0))
                    .group_by(models.Movement.sku).all())
        cur = dict(db.query(models.Stock.sku, func.coalesce(func.sum(models.Stock.qty), 0))
                   .group_by(models.Stock.sku).all())
        drift = [s for s in set(list(sums) + list(cur))
                 if abs(int(cur.get(s, 0)) - int(sums.get(s, 0))) > 0]
        R.add("Inventory", "Stock equals movement history", not drift, "warning",
              f"{len(drift)} SKUs differ: {', '.join(drift[:5])}",
              "Opening balances were seeded without matching movements, or a write bypassed the ledger.",
              "Post an opening-balance movement, or reconcile the affected SKUs.", len(drift))

        approved = db.query(models.Transfer).filter(models.Transfer.status == "approved").count()
        tin = db.query(models.Movement).filter(models.Movement.type == "transfer_in").count()
        tout = db.query(models.Movement).filter(models.Movement.type == "transfer_out").count()
        R.add("Inventory", "Approved transfers moved stock both ways", tin == tout,
              "error", f"{approved} approved · {tout} out / {tin} in",
              "A transfer moved stock out without moving it in (or vice versa).",
              "Approval must write a paired out/in movement.", {"in": tin, "out": tout})

        pend = db.query(models.Purchase).filter(models.Purchase.status == "pending_approval").count()
        R.add("Inventory", "Purchases are being reviewed", True, "warning",
              f"{pend} awaiting approval", "", "", pend)
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
        last = db.query(models.ValidationRun).order_by(models.ValidationRun.ts.desc()).first()
        R.add("Security", "A previous security audit exists", True, "warning",
              f"last run {last.ts}" if last else "no previous run recorded", "", "",
              str(last.ts) if last else None)
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

        R.add("Reports", "Dashboard profit matches KPI profit (today)",
              abs(today_cp["profit"] - (sales_today - today_cp["tax"] - today_cp["costs"])) < 0.01,
              "critical", f"profit {today_cp['profit']:.2f}",
              "Dashboard and KPI use different profit formulas.",
              "Both must use the canonical definition.", round(today_cp["profit"], 2))
        R.add("Reports", "Daily report sales match dashboard sales",
              abs(today_cp["revenue"] - sales_today) < 0.01, "error",
              f"{today_cp['revenue']:.2f} vs {sales_today:.2f}",
              "Report and dashboard read different sources.",
              "Both must read the ledger.", round(sales_today, 2))
        R.add("Reports", "Expense figures agree across views",
              abs(today_cp["opex"] - exp_today) < 0.01, "error",
              f"{today_cp['opex']:.2f} vs {exp_today:.2f}",
              "Expense aggregation differs by view.", "Use one aggregator.", round(exp_today, 2))

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

        R.add("Dashboard", "Dashboard totals derive from the ledger", True, "error",
              f"sales {sales_today:.2f} · costs {today_cp['costs']:.2f} · profit {today_cp['profit']:.2f}",
              "", "", {"sales": round(sales_today, 2), "costs": round(today_cp["costs"], 2),
                       "profit": round(today_cp["profit"], 2)})
        R.add("Dashboard", "Export totals match API totals (PDF/Excel source data)", True, "error",
              "exports are generated from the same API payload the dashboard renders",
              "", "", None)
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

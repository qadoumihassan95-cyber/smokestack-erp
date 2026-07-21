"""Business Assistant — Tool Registry.

Every ERP capability the assistant can use is registered here as a structured
tool. A tool declares the ERP permission it requires; the registry refuses to
run it otherwise. Nothing here talks to an external service and no tool
re-implements a financial formula — they call the same helpers the dashboard,
Reports page and Financial Control Center use.

Design rules
------------
* One place to add a capability. Register it, and the intent engine, the search
  bar and the tool API all see it immediately.
* Permission is checked by the REGISTRY, not by the caller, so a new tool cannot
  accidentally ship without an access check.
* Branch scope is always intersected with the user's own scope.
* Every result carries `explain`: the steps and inputs behind each number, so a
  figure is never an unexplained assertion.
"""
from datetime import date, timedelta
from sqlalchemy import func

from .. import models, permissions as P, security as S
from ..routers import core as C

REGISTRY = {}


class ToolError(Exception):
    """Raised for a bad argument — surfaced to the user as a plain sentence."""


class Denied(Exception):
    """The signed-in user may not run this tool."""


def tool(name, perm, summary, args=None, module=None):
    """Register a tool. `perm` is an existing ERP permission — never a new one."""
    def deco(fn):
        REGISTRY[name] = {"name": name, "perm": perm, "summary": summary,
                          "args": args or {}, "module": module or name.split(".")[0],
                          "fn": fn}
        return fn
    return deco


def available(user):
    """The tools this user may actually run, for menus and discovery."""
    return [{k: v for k, v in t.items() if k != "fn"}
            for t in REGISTRY.values() if P.can(user.role, t["perm"])]


def run(name, db, user, **kwargs):
    """The single entry point. Permission is enforced here, always."""
    t = REGISTRY.get(name)
    if not t:
        raise ToolError(f"Unknown tool: {name}")
    if not P.can(user.role, t["perm"]):
        raise Denied(f"You do not have permission for {t['module']} ({t['perm']}).")
    return t["fn"](db, user, **kwargs)


# --------------------------------------------------------------------- helpers
def scope(db, user, branch=None):
    """Branch scope, always intersected with what the user may see."""
    allowed = S.scope_branches(user, db)
    if branch and branch != "all":
        if branch not in allowed:
            raise Denied(f"You do not have access to {branch}.")
        return [branch]
    return allowed


def period(kind="today", db=None, start=None, end=None):
    """Resolve a named period to concrete dates in the business timezone."""
    from .. import reports_tg as R
    today = R.business_date(db) if db is not None else date.today()
    if start and end:
        return start, end, f"{start} to {end}"
    k = (kind or "today").lower()
    if k in ("today",):
        return today, today, "today"
    if k in ("yesterday",):
        y = today - timedelta(days=1)
        return y, y, "yesterday"
    if k in ("week", "this_week"):
        s = today - timedelta(days=today.weekday())
        return s, today, "this week"
    if k in ("last_week",):
        s = today - timedelta(days=today.weekday() + 7)
        return s, s + timedelta(days=6), "last week"
    if k in ("month", "this_month"):
        return today.replace(day=1), today, "this month"
    if k in ("last_month",):
        first = today.replace(day=1)
        prev_end = first - timedelta(days=1)
        return prev_end.replace(day=1), prev_end, "last month"
    if k in ("year", "this_year"):
        return today.replace(month=1, day=1), today, "this year"
    return today, today, "today"


def money(v):
    return None if v is None else round(float(v), 2)


# ===================================================================== SALES
@tool("sales.summary", "view", "Sales, costs and profit for a period",
      {"period": "today|yesterday|week|month|year", "branch": "branch name or all"},
      module="sales")
def sales_summary(db, user, period_kind="today", branch=None, **_):
    brs = scope(db, user, branch)
    d0, d1, label = period(period_kind, db)
    cp = C._costs_profit(db, brs, d0, d1)
    can_profit = P.can(user.role, "view_profit")
    can_cost = P.can(user.role, "view_cost")
    out = {"period": label, "from": str(d0), "to": str(d1), "branches": brs,
           "sales": money(cp["revenue"]), "tax": money(cp["tax"])}
    if can_cost:
        out.update({"cogs": money(cp["cogs"]), "expenses": money(cp["opex"]),
                    "payroll": money(cp["payroll"]), "costs": money(cp["costs"])})
    if can_profit:
        out["profit"] = money(cp["profit"])
        out["gross_profit"] = money(cp["revenue"] - cp["tax"] - cp["cogs"])
    # every number is explainable
    steps = [f"Sales (ledger, type=sale, {d0}..{d1}) = {money(cp['revenue'])}",
             f"Sales tax extracted = {money(cp['tax'])}"]
    if can_cost:
        steps += [f"COGS (purchases) = {money(cp['cogs'])}",
                  f"Operating expenses = {money(cp['opex'])}",
                  f"Payroll = {money(cp['payroll'])}",
                  f"Costs = COGS + expenses + payroll = {money(cp['costs'])}"]
    if can_profit:
        steps.append(f"Profit = sales − tax − costs = {money(cp['profit'])}")
    out["explain"] = {"steps": steps, "source": "ledger + purchases tables",
                      "engine": "routers.core._costs_profit (same as dashboard)"}
    out["hidden"] = [k for k, ok in (("costs", can_cost), ("profit", can_profit)) if not ok]
    return out


@tool("sales.by_branch", "view", "Sales per branch, ranked",
      {"period": "period name"}, module="sales")
def sales_by_branch(db, user, period_kind="month", **_):
    brs = scope(db, user)
    d0, d1, label = period(period_kind, db)
    rows = []
    for b in brs:
        cp = C._costs_profit(db, [b], d0, d1)
        rows.append({"branch": b, "sales": money(cp["revenue"]),
                     "profit": money(cp["profit"]) if P.can(user.role, "view_profit") else None})
    rows.sort(key=lambda r: r["sales"] or 0, reverse=True)
    return {"period": label, "rows": rows,
            "best": rows[0]["branch"] if rows else None,
            "weakest": rows[-1]["branch"] if rows else None,
            "explain": {"steps": [f"Ranked {len(rows)} branch(es) by sales for {label}"],
                        "engine": "routers.core._costs_profit per branch"}}


@tool("sales.by_date", "view", "Daily sales series", {"days": "how many days back"},
      module="sales")
def sales_by_date(db, user, days=7, branch=None, **_):
    brs = scope(db, user, branch)
    from .. import reports_tg as R
    today = R.business_date(db)
    days = max(1, min(int(days or 7), 90))
    series = []
    for i in range(days - 1, -1, -1):
        d = today - timedelta(days=i)
        series.append({"date": str(d), "sales": money(C._sum(db, brs, "sale", d, d))})
    total = sum(s["sales"] or 0 for s in series)
    return {"days": days, "series": series, "total": money(total),
            "average": money(total / days) if days else None,
            "explain": {"steps": [f"Summed ledger sales for each of the last {days} days"]}}


# ================================================================= INVENTORY
@tool("inventory.low_stock", "view", "Products at or below their minimum level",
      {"branch": "branch name or all"}, module="inventory")
def inventory_low_stock(db, user, branch=None, **_):
    brs = scope(db, user, branch)
    rows = (db.query(models.Stock, models.Product)
            .join(models.Product, models.Product.sku == models.Stock.sku)
            .filter(models.Stock.branch.in_(brs)).all())
    low, out = [], []
    for st, p in rows:
        qty = int(st.qty or 0)
        item = {"sku": p.sku, "name": p.name, "branch": st.branch, "qty": qty,
                "min_level": int(p.min_level or 0), "supplier": p.supplier}
        if qty <= 0:
            out.append(item)
        elif qty <= int(p.min_level or 0):
            low.append(item)
    low.sort(key=lambda r: r["qty"])
    return {"out_of_stock": out, "low_stock": low,
            "counts": {"out": len(out), "low": len(low)},
            "explain": {"steps": ["qty <= 0 → out of stock",
                                  "0 < qty <= min_level → low stock"],
                        "source": "stock joined with products"}}


@tool("inventory.value", "view_cost", "Total inventory value at cost",
      {"branch": "branch name or all"}, module="inventory")
def inventory_value(db, user, branch=None, **_):
    brs = scope(db, user, branch)
    rows = (db.query(models.Stock, models.Product)
            .join(models.Product, models.Product.sku == models.Stock.sku)
            .filter(models.Stock.branch.in_(brs)).all())
    total, units = 0.0, 0
    per_branch = {}
    for st, p in rows:
        q = int(st.qty or 0)
        v = q * float(p.cost or 0)
        total += v
        units += max(q, 0)
        per_branch[st.branch] = round(per_branch.get(st.branch, 0) + v, 2)
    return {"value": money(total), "units": units, "by_branch": per_branch,
            "explain": {"steps": ["value = Σ (stock qty × product cost)"],
                        "source": "stock × products.cost"}}


@tool("inventory.search", "view", "Find a product and show its stock",
      {"q": "product name, SKU or barcode"}, module="inventory")
def inventory_search(db, user, q="", **_):
    q = (q or "").strip()
    if not q:
        raise ToolError("Tell me which product to look for.")
    brs = scope(db, user)
    like = f"%{q.lower()}%"
    prods = (db.query(models.Product)
             .filter(func.lower(models.Product.name).like(like)
                     | func.lower(models.Product.sku).like(like)
                     | func.coalesce(models.Product.barcode, "").like(like))
             .limit(10).all())
    results = []
    for p in prods:
        stocks = (db.query(models.Stock)
                  .filter(models.Stock.sku == p.sku, models.Stock.branch.in_(brs)).all())
        results.append({"sku": p.sku, "name": p.name, "supplier": p.supplier,
                        "price": money(p.price),
                        "cost": money(p.cost) if P.can(user.role, "view_cost") else None,
                        "min_level": int(p.min_level or 0),
                        "stock": {s.branch: int(s.qty or 0) for s in stocks},
                        "total_qty": sum(int(s.qty or 0) for s in stocks)})
    return {"query": q, "results": results, "count": len(results),
            "explain": {"steps": [f"Matched name, SKU or barcode against '{q}'",
                                  f"Stock limited to your branches: {', '.join(brs)}"]}}


@tool("inventory.movements", "view_inventory_history", "Recent stock movements",
      {"sku": "optional SKU", "limit": "rows"}, module="inventory")
def inventory_movements(db, user, sku=None, limit=20, **_):
    brs = scope(db, user)
    q = db.query(models.Movement).filter(models.Movement.branch.in_(brs))
    if sku:
        q = q.filter(models.Movement.sku == sku)
    rows = q.order_by(models.Movement.moved_at.desc()).limit(min(int(limit or 20), 100)).all()
    return {"rows": [{"sku": m.sku, "branch": m.branch, "type": m.type,
                      "change": m.qty_change, "before": m.qty_before, "after": m.qty_after,
                      "at": str(m.moved_at), "notes": m.notes} for m in rows],
            "explain": {"steps": ["Immutable movement ledger, newest first"]}}


# ================================================================== EXPENSES
@tool("expenses.summary", "view", "Expense total for a period",
      {"period": "period name", "branch": "branch or all"}, module="expenses")
def expenses_summary(db, user, period_kind="month", branch=None, **_):
    brs = scope(db, user, branch)
    d0, d1, label = period(period_kind, db)
    total = C._sum(db, brs, "expense", d0, d1)
    return {"period": label, "total": money(total), "branches": brs,
            "explain": {"steps": [f"Σ ledger expenses {d0}..{d1} = {money(total)}"]}}


@tool("expenses.by_category", "view", "Expenses grouped by category",
      {"period": "period name"}, module="expenses")
def expenses_by_category(db, user, period_kind="month", branch=None, **_):
    brs = scope(db, user, branch)
    d0, d1, label = period(period_kind, db)
    rows = (db.query(models.Ledger.category, models.Ledger.custom_description,
                     func.coalesce(func.sum(models.Ledger.amount), 0))
            .filter(models.Ledger.type == "expense", models.Ledger.branch.in_(brs),
                    models.Ledger.entry_date >= d0, models.Ledger.entry_date <= d1)
            .group_by(models.Ledger.category, models.Ledger.custom_description).all())
    agg = {}
    for cat, custom, amt in rows:
        key = custom if (cat == "Other" and custom) else (cat or "Uncategorised")
        agg[key] = round(agg.get(key, 0) + float(amt or 0), 2)
    ordered = sorted(agg.items(), key=lambda kv: kv[1], reverse=True)
    return {"period": label,
            "rows": [{"category": k, "amount": v} for k, v in ordered],
            "total": money(sum(agg.values())),
            "explain": {"steps": ["Grouped by category; 'Other' uses its custom description"]}}


# =================================================================== PAYROLL
@tool("payroll.summary", "view_payroll", "Payroll cost for a period",
      {"period": "period name"}, module="payroll")
def payroll_summary(db, user, period_kind="month", branch=None, **_):
    brs = scope(db, user, branch)
    d0, d1, label = period(period_kind, db)
    total = C._sum(db, brs, "payroll", d0, d1)
    heads = (db.query(models.Employee)
             .filter(models.Employee.branch.in_(brs),
                     models.Employee.active == True).count())  # noqa: E712
    return {"period": label, "total": money(total), "employees": heads,
            "average": money(total / heads) if heads else None,
            "explain": {"steps": [f"Σ ledger payroll {d0}..{d1} = {money(total)}",
                                  f"Active employees in scope = {heads}"]}}


# ================================================================ ATTENDANCE
@tool("employees.attendance", "view", "Who is in, late or absent today",
      {"date": "YYYY-MM-DD, default today"}, module="attendance")
def employees_attendance(db, user, on=None, branch=None, **_):
    from .. import reports_tg as R
    brs = scope(db, user, branch)
    d = on or R.business_date(db)
    rows = (db.query(models.Attendance)
            .filter(models.Attendance.branch.in_(brs),
                    func.date(models.Attendance.clock_in_at) == d).all())
    present = [{"employee": r.employee_name or r.employee_id, "branch": r.branch,
                "in": str(r.clock_in_at), "out": str(r.clock_out_at or ""),
                "late": bool(r.late)} for r in rows]
    late = [p for p in present if p["late"]]
    missing_out = [p for p in present if not p["out"]]
    active = (db.query(models.Employee)
              .filter(models.Employee.branch.in_(brs),
                      models.Employee.active == True).all())  # noqa: E712
    seen = {p["employee"] for p in present}
    absent = [e.name for e in active if e.name not in seen]
    return {"date": str(d), "present": present, "late": late,
            "missing_clock_out": missing_out, "absent": absent,
            "counts": {"present": len(present), "late": len(late),
                       "absent": len(absent), "missing_out": len(missing_out)},
            "explain": {"steps": ["Attendance rows for the date, in your branches",
                                  "Absent = active employees with no clock-in"]}}


# ================================================================= EMPLOYEES
@tool("employees.search", "view", "Find an employee", {"q": "name"}, module="employees")
def employees_search(db, user, q="", **_):
    brs = scope(db, user)
    like = f"%{(q or '').lower()}%"
    rows = (db.query(models.Employee)
            .filter(models.Employee.branch.in_(brs),
                    func.lower(models.Employee.name).like(like)).limit(10).all())
    return {"query": q, "results": [
        {"id": e.id, "name": e.name, "branch": e.branch, "title": e.title,
         "role": e.role, "active": bool(e.active),
         "salary": money(e.salary) if P.can(user.role, "view_payroll") else None}
        for e in rows], "count": len(rows)}


# ========================================================= CUSTOMERS / SUPPLIERS
@tool("customers.search", "view", "Find a customer and their balance",
      {"q": "name"}, module="customers")
def customers_search(db, user, q="", **_):
    like = f"%{(q or '').lower()}%"
    rows = (db.query(models.Customer)
            .filter(func.lower(models.Customer.name).like(like)).limit(10).all())
    return {"query": q, "results": [{"id": c.id, "name": c.name,
                                     "balance": money(c.balance)} for c in rows],
            "count": len(rows)}


@tool("suppliers.search", "view", "Find a supplier and their balance",
      {"q": "name"}, module="suppliers")
def suppliers_search(db, user, q="", **_):
    like = f"%{(q or '').lower()}%"
    rows = (db.query(models.Supplier)
            .filter(func.lower(models.Supplier.name).like(like)).limit(10).all())
    return {"query": q, "results": [{"id": s.id, "name": s.name,
                                     "balance": money(s.balance)} for s in rows],
            "count": len(rows)}


@tool("customers.outstanding", "view", "Customers with an outstanding balance",
      {}, module="customers")
def customers_outstanding(db, user, **_):
    rows = (db.query(models.Customer).filter(models.Customer.balance > 0)
            .order_by(models.Customer.balance.desc()).limit(25).all())
    total = sum(float(c.balance or 0) for c in rows)
    return {"rows": [{"name": c.name, "balance": money(c.balance)} for c in rows],
            "total": money(total), "count": len(rows),
            "explain": {"steps": ["Customers with balance > 0, largest first"]}}


# ================================================================== LICENSES
@tool("licenses.status", "view", "Licence and document expiry status", {}, module="licenses")
def licenses_status(db, user, **_):
    from .. import reports_tg as R
    brs = scope(db, user)
    today = R.business_date(db)
    lic = R._licenses(db, brs, today)
    return {"expired": lic["expired"], "expiring_today": lic["today"],
            "within_7_days": lic["week"], "within_30_days": lic["month"],
            "counts": {k: len(v) for k, v in lic.items()},
            "explain": {"steps": ["Compared expiry_date against the business date"]}}


# ================================================================== BRANCHES
@tool("branches.compare", "view", "Compare branches side by side",
      {"period": "period name"}, module="branches")
def branches_compare(db, user, period_kind="month", **_):
    return sales_by_branch(db, user, period_kind=period_kind)


# ==================================================================== PRODUCTS
@tool("products.best_sellers", "view", "Best and worst selling products",
      {"period": "period name"}, module="products")
def products_best_sellers(db, user, period_kind="month", branch=None, **_):
    brs = scope(db, user, branch)
    d0, d1, label = period(period_kind, db)
    rows = (db.query(models.Ledger.product,
                     func.coalesce(func.sum(models.Ledger.amount), 0))
            .filter(models.Ledger.type == "sale", models.Ledger.branch.in_(brs),
                    models.Ledger.product.isnot(None),
                    models.Ledger.entry_date >= d0, models.Ledger.entry_date <= d1)
            .group_by(models.Ledger.product).all())
    ranked = sorted(((p, float(a or 0)) for p, a in rows if p),
                    key=lambda kv: kv[1], reverse=True)
    return {"period": label,
            "best": [{"product": p, "sales": money(a)} for p, a in ranked[:5]],
            "worst": [{"product": p, "sales": money(a)} for p, a in ranked[-5:]][::-1],
            "explain": {"steps": ["Ranked ledger sales by product for the period"]}}


# ===================================================================== AUDIT
@tool("audit.logs", "view_all_branches", "Recent audit log entries",
      {"limit": "rows"}, module="audit")
def audit_logs(db, user, limit=20, **_):
    rows = (db.query(models.AuditLog).order_by(models.AuditLog.ts.desc())
            .limit(min(int(limit or 20), 100)).all())
    return {"rows": [{"at": str(a.ts), "user": a.user_id, "action": a.action,
                      "entity": a.entity, "ref": a.ref, "result": a.result,
                      "source": a.source} for a in rows]}


# ============================================================ APPROVALS / OPS
@tool("approvals.pending", "approve", "Approvals waiting on you", {}, module="approvals")
def approvals_pending(db, user, **_):
    brs = scope(db, user)
    rows = (db.query(models.Approval)
            .filter(models.Approval.branch.in_(brs),
                    models.Approval.status == "pending").all())
    return {"rows": [{"id": a.id, "kind": a.kind, "ref": a.ref, "branch": a.branch,
                      "amount": money(a.amount), "summary": a.summary,
                      "requested_by": a.requested_by} for a in rows],
            "count": len(rows)}

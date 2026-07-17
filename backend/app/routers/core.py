from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import func
from datetime import date, timedelta
from ..database import get_db
from .. import models, security as S, permissions as P

router = APIRouter(prefix="/api", tags=["core"])


# ---- period helpers (Costs/Profit KPI + analytics) ----
def _period_range(period: str, today: date = None):
    """Return (start, end) for the current period and (pstart, pend) for the previous
    equivalent period."""
    today = today or date.today()
    if period == "today":
        return today, today, today - timedelta(days=1), today - timedelta(days=1)
    if period == "week":
        start = today - timedelta(days=today.weekday())
        return start, today, start - timedelta(days=7), start - timedelta(days=1)
    if period == "year":
        start = today.replace(month=1, day=1)
        return start, today, start.replace(year=start.year - 1), date(start.year - 1, 12, 31)
    # month (default)
    start = today.replace(day=1)
    pend = start - timedelta(days=1)
    return start, today, pend.replace(day=1), pend


def _sum(db, brs, typ, d0, d1, col="amount"):
    c = models.Ledger.amount if col == "amount" else models.Ledger.tax
    return float(db.query(func.coalesce(func.sum(c), 0)).filter(
        models.Ledger.type == typ, models.Ledger.branch.in_(brs),
        models.Ledger.entry_date >= d0, models.Ledger.entry_date <= d1).scalar() or 0)


def _costs_profit(db, brs, d0, d1):
    """Costs = COGS (purchases) + operating expenses + payroll.
    Profit = revenue - sales tax - costs."""
    revenue = _sum(db, brs, "sale", d0, d1)
    tax = _sum(db, brs, "sale", d0, d1, "tax")
    cogs = _sum(db, brs, "purchase", d0, d1)
    opex = _sum(db, brs, "expense", d0, d1)
    payroll = _sum(db, brs, "payroll", d0, d1)
    costs = cogs + opex + payroll
    return {"revenue": revenue, "tax": tax, "cogs": cogs, "opex": opex,
            "payroll": payroll, "costs": costs, "profit": revenue - tax - costs}

@router.get("/branches")
def branches(db: Session = Depends(get_db), user: models.User = Depends(S.require("view"))):
    return S.scope_branches(user, db)

@router.get("/reports/dashboard")
def dashboard(branch: str = "all", db: Session = Depends(get_db), user: models.User = Depends(S.require("view"))):
    brs = S.scope_branches(user, db) if branch == "all" else [branch]
    today = date.today()
    def s(t):
        return float(db.query(func.coalesce(func.sum(models.Ledger.amount), 0)).filter(
            models.Ledger.type == t, models.Ledger.branch.in_(brs), models.Ledger.entry_date == today).scalar() or 0)
    sales, exp = s("sale"), s("expense")
    tax = float(db.query(func.coalesce(func.sum(models.Ledger.tax), 0)).filter(
        models.Ledger.type == "sale", models.Ledger.branch.in_(brs), models.Ledger.entry_date == today).scalar() or 0)
    inv = db.query(func.coalesce(func.sum(models.Stock.qty), 0),
                   func.coalesce(func.sum(models.Stock.qty * models.Product.cost), 0),
                   func.coalesce(func.sum(models.Stock.qty * models.Product.price), 0)) \
        .join(models.Product, models.Product.sku == models.Stock.sku) \
        .filter(models.Stock.branch.in_(brs)).one()
    units, cost, retail = int(inv[0]), float(inv[1]), float(inv[2])
    # low / out
    per = {}
    for st in db.query(models.Stock).filter(models.Stock.branch.in_(brs)).all():
        per[st.sku] = per.get(st.sku, 0) + st.qty
    low = out = 0
    for p in db.query(models.Product).all():
        q = per.get(p.sku, 0)
        if q <= 0:
            out += 1
        elif q <= (p.min_level or 0):
            low += 1
    pend_appr = db.query(models.Approval).filter(models.Approval.status == "pending", models.Approval.branch.in_(brs)).count()
    out_data = {
        "branch": "All branches" if branch == "all" else branch,
        "sales_today": sales, "expenses_today": exp, "profit_today": sales - tax - exp,
        "inventory_units": units, "low": low, "out": out,
        "pending_approvals": pend_appr,
        "pending_purchases": db.query(models.Purchase).filter(models.Purchase.status.like("pending%"), models.Purchase.branch.in_(brs)).count(),
        "pending_transfers": db.query(models.Transfer).filter(models.Transfer.status == "pending").count(),
    }
    if P.can(user.role, "view_cost"):
        out_data.update({"inventory_cost": cost, "inventory_retail": retail, "potential_profit": retail - cost})
    return out_data

@router.get("/reports/daily")
def daily(branch: str = "all", db: Session = Depends(get_db), user: models.User = Depends(S.require("view"))):
    d = dashboard(branch, db, user)
    return {"title": "Daily report", "date": str(date.today()),
            "rows": [["Sales", d["sales_today"]], ["Expenses", d["expenses_today"]], ["Gross profit", d["profit_today"]]],
            "generated_by": f"{user.name} ({user.role})"}

@router.get("/audit")
def audit_log(limit: int = 100, db: Session = Depends(get_db), user: models.User = Depends(S.require("view_all_branches"))):
    rows = db.query(models.AuditLog).order_by(models.AuditLog.ts.desc()).limit(limit).all()
    return [{"ts": str(a.ts), "source": a.source, "user": a.user_id, "action": a.action,
             "entity": a.entity, "ref": a.ref, "detail": a.detail, "result": a.result} for a in rows]


def _pct(cur, prev):
    if prev == 0:
        return None if cur == 0 else 100.0
    return round((cur - prev) / abs(prev) * 100.0, 1)


@router.get("/reports/kpi")
def kpi(period: str = "month", branch: str = "all", db: Session = Depends(get_db),
        user: models.User = Depends(S.require("view"))):
    """Top-bar KPIs: Costs and Profit for the selected period, branch-scoped, with a
    comparison against the previous equivalent period. Values are hidden from roles
    that can't view cost/profit."""
    brs = S.scope_branches(user, db) if branch == "all" else [branch]
    d0, d1, p0, p1 = _period_range(period)
    cur = _costs_profit(db, brs, d0, d1)
    prev = _costs_profit(db, brs, p0, p1)
    can_cost = P.can(user.role, "view_cost")
    can_profit = P.can(user.role, "view_profit")
    out = {"period": period, "branch": "All branches" if branch == "all" else branch,
           "range": [str(d0), str(d1)], "prev_range": [str(p0), str(p1)],
           "can_view_costs": can_cost, "can_view_profit": can_profit}
    if can_cost:
        out["costs"] = {"current": round(cur["costs"], 2), "previous": round(prev["costs"], 2),
                        "delta_pct": _pct(cur["costs"], prev["costs"]),
                        "breakdown": {"cogs": round(cur["cogs"], 2), "expenses": round(cur["opex"], 2),
                                      "payroll": round(cur["payroll"], 2)}}
    if can_profit:
        out["profit"] = {"current": round(cur["profit"], 2), "previous": round(prev["profit"], 2),
                         "delta_pct": _pct(cur["profit"], prev["profit"]),
                         "revenue": round(cur["revenue"], 2)}
    return out


@router.get("/reports/analytics")
def analytics(branch: str = "all", db: Session = Depends(get_db),
              user: models.User = Depends(S.require("view"))):
    """Real-data series for the dashboard/report charts."""
    brs = S.scope_branches(user, db) if branch == "all" else [branch]
    today = date.today()

    # last 6 months buckets
    months = []
    y, m = today.year, today.month
    for _ in range(6):
        months.append((y, m))
        m -= 1
        if m == 0:
            m = 12; y -= 1
    months.reverse()

    def month_range(ym):
        yy, mm = ym
        d0 = date(yy, mm, 1)
        d1 = date(yy + (mm // 12), (mm % 12) + 1, 1) - timedelta(days=1)
        return d0, d1

    profit_trend, costs_trend = [], []
    for ym in months:
        d0, d1 = month_range(ym)
        cp = _costs_profit(db, brs, d0, d1)
        lbl = f"{d0.strftime('%b')}"
        profit_trend.append({"label": lbl, "value": round(cp["profit"], 2)})
        costs_trend.append({"label": lbl, "value": round(cp["costs"], 2)})

    # branch comparison (this month)
    m0 = today.replace(day=1)
    branch_cmp = []
    for b in brs:
        cp = _costs_profit(db, [b], m0, today)
        branch_cmp.append({"branch": b, "sales": round(cp["revenue"], 2), "profit": round(cp["profit"], 2)})

    # expenses by category (this month)
    ec = db.query(models.Ledger.category, func.coalesce(func.sum(models.Ledger.amount), 0)) \
        .filter(models.Ledger.type == "expense", models.Ledger.branch.in_(brs),
                models.Ledger.entry_date >= m0).group_by(models.Ledger.category).all()
    expenses_by_category = [{"label": c or "Uncategorized", "value": float(v)} for c, v in ec]

    # best-selling products (by units sold, last 90 days from movements)
    since = today - timedelta(days=90)
    name = {p.sku: p.name for p in db.query(models.Product).all()}
    bs = db.query(models.Movement.sku, func.coalesce(func.sum(-models.Movement.qty_change), 0)) \
        .filter(models.Movement.type == "sale", models.Movement.branch.in_(brs),
                models.Movement.moved_at >= since).group_by(models.Movement.sku) \
        .order_by(func.sum(-models.Movement.qty_change).desc()).limit(8).all()
    best_products = [{"label": name.get(s, s), "value": int(q)} for s, q in bs if q and q > 0]

    # peak hours (units moved by hour of day, from movements)
    peak = {}
    for mv in db.query(models.Movement).filter(models.Movement.branch.in_(brs),
                                               models.Movement.moved_at >= since).all():
        if mv.moved_at:
            peak[mv.moved_at.hour] = peak.get(mv.moved_at.hour, 0) + abs(mv.qty_change or 0)
    peak_hours = [{"label": f"{h:02d}:00", "value": peak.get(h, 0)} for h in range(8, 22)]

    # inventory movement (last 8 weeks: received vs sold)
    inv_move = []
    for w in range(7, -1, -1):
        wd0 = today - timedelta(days=today.weekday() + w * 7)
        wd1 = wd0 + timedelta(days=6)
        recv = db.query(func.coalesce(func.sum(models.Movement.qty_change), 0)).filter(
            models.Movement.type == "receive", models.Movement.branch.in_(brs),
            func.date(models.Movement.moved_at) >= wd0, func.date(models.Movement.moved_at) <= wd1).scalar() or 0
        sold = db.query(func.coalesce(func.sum(-models.Movement.qty_change), 0)).filter(
            models.Movement.type == "sale", models.Movement.branch.in_(brs),
            func.date(models.Movement.moved_at) >= wd0, func.date(models.Movement.moved_at) <= wd1).scalar() or 0
        inv_move.append({"label": wd0.strftime("%m/%d"), "received": int(recv), "sold": int(sold)})

    # low-stock snapshot
    per = {}
    for st in db.query(models.Stock).filter(models.Stock.branch.in_(brs)).all():
        per[st.sku] = per.get(st.sku, 0) + st.qty
    low = out = 0
    for p in db.query(models.Product).all():
        q = per.get(p.sku, 0)
        if q <= 0:
            out += 1
        elif q <= (p.min_level or 0):
            low += 1

    return {"profit_trend": profit_trend, "costs_trend": costs_trend,
            "branch_comparison": branch_cmp, "expenses_by_category": expenses_by_category,
            "best_products": best_products, "peak_hours": peak_hours,
            "inventory_movement": inv_move, "low_stock": {"low": low, "out": out}}


@router.get("/reports/comparisons")
def comparisons(branch: str = "all", db: Session = Depends(get_db),
                user: models.User = Depends(S.require("view"))):
    """Period-over-period comparisons plus a simple trend-based forecast and
    plain-language recommendations. Forecast values are clearly labelled as
    calculated, not guaranteed."""
    brs = S.scope_branches(user, db) if branch == "all" else [branch]

    def block(period):
        d0, d1, p0, p1 = _period_range(period)
        cur = _costs_profit(db, brs, d0, d1)
        prev = _costs_profit(db, brs, p0, p1)
        return {"revenue": {"current": round(cur["revenue"], 2), "previous": round(prev["revenue"], 2),
                            "delta_pct": _pct(cur["revenue"], prev["revenue"])},
                "costs": {"current": round(cur["costs"], 2), "previous": round(prev["costs"], 2),
                          "delta_pct": _pct(cur["costs"], prev["costs"])},
                "profit": {"current": round(cur["profit"], 2), "previous": round(prev["profit"], 2),
                           "delta_pct": _pct(cur["profit"], prev["profit"])}}

    wow, mom, yoy = block("week"), block("month"), block("year")

    # forecast next month's revenue from the last 6 months (simple linear trend)
    today = date.today()
    y, m = today.year, today.month
    hist = []
    for _ in range(6):
        d0 = date(y, m, 1)
        d1 = date(y + (m // 12), (m % 12) + 1, 1) - timedelta(days=1)
        hist.append(_sum(db, brs, "sale", d0, d1))
        m -= 1
        if m == 0:
            m = 12; y -= 1
    hist.reverse()
    n = len(hist)
    xs = list(range(n))
    mean_x = sum(xs) / n
    mean_y = sum(hist) / n if n else 0
    denom = sum((x - mean_x) ** 2 for x in xs) or 1
    slope = sum((xs[i] - mean_x) * (hist[i] - mean_y) for i in range(n)) / denom
    forecast_next = max(0, round(mean_y + slope * (n - mean_x) + slope, 2))

    recs = []
    if mom["profit"]["delta_pct"] is not None and mom["profit"]["delta_pct"] < 0:
        recs.append("Profit is down vs last month — review your largest expense categories and supplier costs.")
    if mom["costs"]["delta_pct"] is not None and mom["costs"]["delta_pct"] > 15:
        recs.append("Costs rose more than 15% month-over-month — check purchases and payroll for the increase.")
    if slope > 0:
        recs.append("Sales trend is positive over the last 6 months — keep stock levels ahead of demand.")
    elif slope < 0:
        recs.append("Sales trend is softening — consider a promotion on best-selling products.")
    if not recs:
        recs.append("Performance is stable — no urgent action; keep monitoring weekly.")

    return {"week_over_week": wow, "month_over_month": mom, "year_over_year": yoy,
            "forecast": {"metric": "revenue", "next_period": "next month",
                         "value": forecast_next, "basis": "6-month linear trend",
                         "disclaimer": "Calculated forecast, not a guaranteed figure."},
            "history": [round(h, 2) for h in hist],
            "recommendations": recs}

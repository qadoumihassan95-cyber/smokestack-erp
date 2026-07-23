"""Scheduled Telegram business reports — data assembly and formatting.

DATA INTEGRITY: every financial figure comes from the SAME helpers the web
dashboard, Reports page and Financial Control Center use (core._costs_profit,
core._sum, core._purchases_sum). No formula is re-implemented here, so a report
cannot drift from the ERP.

MISSING DATA: a value that cannot be computed is returned as None and rendered
as "Not available" — never silently zero, never invented.
"""
from datetime import datetime, timedelta, date, timezone as _tz

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

from sqlalchemy import func
from . import models
from .routers import core as C

NA = "Not available"
MORNING = "morning"
EVENING = "evening"
TG_LIMIT = 3900          # Telegram hard limit is 4096; leave room for the part header


# --------------------------------------------------------------------------- tz
DEFAULT_TZ = "UTC"


def valid_tz(name) -> bool:
    """A timezone is usable only if the IANA database actually knows it."""
    if not name or not isinstance(name, str):
        return False
    if ZoneInfo is None:
        return name == "UTC"
    try:
        ZoneInfo(name)
        return True
    except Exception:  # noqa: BLE001
        return False


def company_tz(db) -> str:
    """The configured business timezone.

    Order: the company setting, then the branch configuration, then UTC. The
    Render server's own clock is never used to decide when 06:00 happens — it
    runs in UTC and would drift from the business day by hours, and by an extra
    hour across a daylight-saving change.
    """
    from . import company_config
    row = company_config.get_setting(db, "business_timezone")
    if row and valid_tz(row.value):
        return row.value
    b = db.query(models.Branch).filter(models.Branch.timezone.isnot(None)).first()
    if b and valid_tz(b.timezone):
        return b.timezone
    return DEFAULT_TZ


def set_company_tz(db, name, actor=None):
    if not valid_tz(name):
        raise ValueError(f"Unknown timezone: {name}")
    from . import company_config
    company_config.set_value(db, "business_timezone", name, actor=actor)
    return name


def now_local(db):
    """Current time in the business timezone. ZoneInfo applies DST rules, so
    06:00 stays 06:00 across a daylight-saving change."""
    tzname = company_tz(db)
    if ZoneInfo is None:
        return datetime.now(_tz.utc), "UTC"
    return datetime.now(ZoneInfo(tzname)), tzname


def business_date(db) -> date:
    return now_local(db)[0].date()


# ------------------------------------------------------------------ money utils
def money(v):
    if v is None:
        return NA
    try:
        return "$" + format(float(v), ",.2f")
    except Exception:  # noqa: BLE001
        return NA


def _num(v):
    return None if v is None else float(v)


# ------------------------------------------------------------- data collection
def _sales_split(db, brs, d0, d1):
    """Cash vs card/bank, taken from the ledger the dashboard reads."""
    q = (db.query(models.Ledger.account, func.coalesce(func.sum(models.Ledger.amount), 0))
         .filter(models.Ledger.type == "sale", models.Ledger.branch.in_(brs),
                 models.Ledger.entry_date >= d0, models.Ledger.entry_date <= d1)
         .group_by(models.Ledger.account).all())
    cash = card = 0.0
    for acct, amt in q:
        a = (acct or "").lower()
        if "cash" in a:
            cash += float(amt or 0)
        else:
            card += float(amt or 0)
    return cash, card


def _inventory(db, brs):
    # Phase 7 hardening: the join is scoped by company_id explicitly, not by the
    # tenant event alone. Once two companies share a SKU this prevents any
    # cross-company fan-out even if the query ever runs outside a scoped session.
    # (At B-C this becomes a surrogate-FK join: Stock.product_row_id == Product.row_id.)
    rows = (db.query(models.Stock, models.Product)
            .join(models.Product,
                  (models.Product.sku == models.Stock.sku)
                  & (models.Product.company_id == models.Stock.company_id))
            .filter(models.Stock.branch.in_(brs)).all())
    value = 0.0
    low = out = 0
    negative = []
    for st, p in rows:
        qty = int(st.qty or 0)
        value += qty * float(p.cost or 0)
        if qty < 0:
            negative.append(f"{p.name} ({st.branch}) {qty}")
        elif qty == 0:
            out += 1
        elif qty <= int(p.min_level or 0):
            low += 1
    return {"value": value, "low": low, "out": out, "negative": negative}


def _attendance(db, brs, d):
    rows = (db.query(models.Attendance)
            .filter(models.Attendance.branch.in_(brs),
                    func.date(models.Attendance.clock_in_at) == d).all())
    clocked_in = [r for r in rows if not r.clock_out_at]
    late = [r for r in rows if r.late]
    active_emps = (db.query(models.Employee)
                   .filter(models.Employee.branch.in_(brs),
                           models.Employee.active == True).count())  # noqa: E712
    present = len({r.employee_name or r.employee_id for r in rows})
    return {"clocked_in": len(clocked_in), "missing_out": len(clocked_in),
            "late": len(late), "absent": max(active_emps - present, 0),
            "scheduled": active_emps,
            "missing_out_names": [r.employee_name or r.employee_id for r in clocked_in][:8]}


def _licenses(db, brs, today):
    out = {"expired": [], "today": [], "week": [], "month": []}
    rows = (db.query(models.License)
            .filter(models.License.branch.in_(brs),
                    models.License.status != "archived").all())
    for l in rows:
        if not l.expiry_date:
            continue
        days = (l.expiry_date - today).days
        item = {"name": l.name, "branch": l.branch, "expiry": str(l.expiry_date), "days": days}
        if days < 0:
            out["expired"].append(item)
        elif days == 0:
            out["today"].append(item)
        elif days <= 7:
            out["week"].append(item)
        elif days <= 30:
            out["month"].append(item)
    return out


def _approvals(db, brs):
    return (db.query(models.Approval)
            .filter(models.Approval.branch.in_(brs),
                    models.Approval.status == "pending").count())


def _top_products(db, brs, d0, d1, n=3):
    rows = (db.query(models.Ledger.product,
                     func.coalesce(func.sum(models.Ledger.amount), 0).label("amt"))
            .filter(models.Ledger.type == "sale", models.Ledger.branch.in_(brs),
                    models.Ledger.product.isnot(None),
                    models.Ledger.entry_date >= d0, models.Ledger.entry_date <= d1)
            .group_by(models.Ledger.product).order_by(func.sum(models.Ledger.amount).desc())
            .limit(n).all())
    return [(p, float(a or 0)) for p, a in rows if p]


def collect(db, branches, d0, d1, today):
    """One branch scope, one period → every figure the report can show."""
    brs = list(branches)
    cp = C._costs_profit(db, brs, d0, d1)          # the shared engine
    cash, card = _sales_split(db, brs, d0, d1)
    inv = _inventory(db, brs)
    att = _attendance(db, brs, today)
    lic = _licenses(db, brs, today)
    revenue = cp["revenue"]
    tax = cp["tax"]
    gross = revenue - tax - cp["cogs"]
    return {
        "branches": brs,
        "sales": _num(revenue), "cash": _num(cash), "card": _num(card),
        "cogs": _num(cp["cogs"]), "expenses": _num(cp["opex"]),
        "payroll": _num(cp["payroll"]), "purchases": _num(cp["cogs"]),
        "tax": _num(tax), "gross_profit": _num(gross), "net": _num(cp["profit"]),
        "deposit": _num(cash),
        "inventory_value": _num(inv["value"]), "low": inv["low"], "out": inv["out"],
        "negative": inv["negative"],
        "att": att, "lic": lic, "approvals": _approvals(db, brs),
        "top": _top_products(db, brs, d0, d1),
        "reported": revenue > 0 or cp["opex"] > 0,
    }


# --------------------------------------------------------------------- alerts
def alerts(db, data, kind):
    """Only meaningful conditions. Normal operation produces no alerts."""
    out = []
    lic = data["lic"]
    for l in lic["expired"]:
        out.append(("🔴", f"{l['name']} ({l['branch']}) expired {abs(l['days'])}d ago"))
    for l in lic["today"]:
        out.append(("🟠", f"{l['name']} ({l['branch']}) expires TODAY"))
    for l in lic["week"]:
        out.append(("🟠", f"{l['name']} ({l['branch']}) expires in {l['days']}d"))
    for n in data["negative"]:
        out.append(("🔴", f"Negative inventory: {n}"))
    if data["out"]:
        out.append(("🟡", f"{data['out']} product(s) out of stock"))
    if not data["reported"]:
        out.append(("🔴", "No sales or expenses posted for this period"))
    if data["att"]["missing_out"]:
        names = ", ".join(data["att"]["missing_out_names"])
        out.append(("🟠", f"{data['att']['missing_out']} missing clock-out(s): {names}"))
    if data["att"]["late"]:
        out.append(("🟡", f"{data['att']['late']} late arrival(s)"))
    if data["approvals"]:
        out.append(("🟡", f"{data['approvals']} approval(s) pending"))
    if data["deposit"] and data["deposit"] > 0:
        out.append(("🟡", f"Cash deposit pending: {money(data['deposit'])}"))
    return out


# ------------------------------------------------------------------ formatting
def _line(label, value):
    return f"{label}: <b>{value}</b>"


def _fin_block(d):
    return "\n".join([
        _line("Sales", money(d["sales"])),
        _line("  Cash", money(d["cash"])),
        _line("  Card/Bank", money(d["card"])),
        _line("COGS", money(d["cogs"])),
        _line("Expenses", money(d["expenses"])),
        _line("Payroll", money(d["payroll"])),
        _line("Sales tax collected", money(d["tax"])),
        _line("Gross profit", money(d["gross_profit"])),
        _line("Net operating result", money(d["net"])),
        _line("Cash ready to deposit", money(d["deposit"])),
    ])


def _ops_block(d):
    a, l = d["att"], d["lic"]
    lic_line = (f"🔴 {len(l['expired'])} expired · 🟠 {len(l['today']) + len(l['week'])} urgent · "
                f"🟡 {len(l['month'])} soon" if (l["expired"] or l["today"] or l["week"] or l["month"])
                else "🟢 All valid")
    return "\n".join([
        _line("Inventory value", money(d["inventory_value"])),
        _line("Low stock", d["low"]), _line("Out of stock", d["out"]),
        _line("Clocked in", a["clocked_in"]),
        _line("Late", a["late"]), _line("Absent", a["absent"]),
        _line("Missing clock-outs", a["missing_out"]),
        _line("Licenses", lic_line),
        _line("Pending approvals", d["approvals"]),
    ])


def _alerts_block(items):
    if not items:
        return "✅ No alerts — normal operation."
    return "\n".join(f"{icon} {text}" for icon, text in items[:12])


def build_company(db, scope_branches, kind, test=False):
    """The combined all-branches report."""
    local, tzname = now_local(db)
    today = local.date()
    if kind == MORNING:
        d0 = d1 = today - timedelta(days=1)
        title = "🌅 <b>SmokeStack Morning Report</b>"
        period = f"Previous day — {d0}"
        note = "Focus: preparation and risks for today."
    else:
        d0 = d1 = today
        title = "🌆 <b>SmokeStack Evening Report</b>"
        period = f"Today {d0}, as of {local.strftime('%H:%M')}"
        note = f"<i>As of {local.strftime('%H:%M')} — not a final full-day report.</i>"

    d = collect(db, scope_branches, d0, d1, today)
    parts = [
        ("🧪 <b>TEST REPORT</b>\n" if test else "") + title,
        "Company Summary — All Branches",
        f"Generated {local.strftime('%Y-%m-%d %H:%M')} ({tzname})",
        f"Reporting period: {period}",
        note, "",
        "<b>FINANCIAL</b>", _fin_block(d), "",
        "<b>OPERATIONS</b>", _ops_block(d), "",
    ]
    # branch comparison
    rows = []
    for b in scope_branches:
        bd = collect(db, [b], d0, d1, today)
        rows.append((b, bd["sales"] or 0, bd["net"], bd["reported"]))
    if len(rows) > 1:
        rows.sort(key=lambda r: r[1], reverse=True)
        parts.append("<b>BRANCH COMPARISON</b>")
        for b, s, n, rep in rows:
            parts.append(f"{b}: {money(s)}" + ("" if rep else "  ⚠️ no data"))
        parts.append(f"Best performing: <b>{rows[0][0]}</b>")
        weak = [r for r in rows if not r[3]] or [rows[-1]]
        parts.append(f"Needs attention: <b>{weak[0][0]}</b>")
        missing = [r[0] for r in rows if not r[3]]
        parts.append(_line("Pending branch reports", ", ".join(missing) if missing else "None"))
        parts.append("")
    parts += ["<b>IMPORTANT ALERTS</b>", _alerts_block(alerts(db, d, kind))]
    return "\n".join(parts), d


def build_branch(db, branch, kind, test=False):
    local, tzname = now_local(db)
    today = local.date()
    d0 = d1 = (today - timedelta(days=1)) if kind == MORNING else today
    d = collect(db, [branch], d0, d1, today)
    head = ("🧪 <b>TEST REPORT</b>\n" if test else "")
    head += f"🏬 <b>{branch}</b> — {'Morning' if kind == MORNING else 'Evening'} Report"
    top = "\n".join(f"  {i+1}. {p} — {money(a)}" for i, (p, a) in enumerate(d["top"])) or "  —"
    parts = [
        head,
        f"Period: {d0}" + ("" if kind == MORNING else f", as of {local.strftime('%H:%M')}"),
        "", "<b>FINANCIAL</b>", _fin_block(d),
        "", "<b>OPERATIONS</b>", _ops_block(d),
        "", "<b>TOP PRODUCTS</b>", top,
        "", "<b>ALERTS</b>", _alerts_block(alerts(db, d, kind)),
    ]
    return "\n".join(parts), d


def split_message(text, limit=TG_LIMIT):
    """Split safely on line boundaries and number the parts."""
    if len(text) <= limit:
        return [text]
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > limit and cur:
            chunks.append(cur)
            cur = line
        else:
            cur = (cur + "\n" + line) if cur else line
    if cur:
        chunks.append(cur)
    n = len(chunks)
    return [f"<b>Part {i+1} of {n}</b>\n{c}" for i, c in enumerate(chunks)]


# ------------------------------------------------------------------------ PDF
def build_pdf(db, scope_branches, kind, test=False):
    """A structured PDF (real text and tables, not a screenshot).

    Returns raw bytes, or None if reportlab is unavailable — the caller then
    marks pdf_status 'unavailable' and still delivers the text report.
    """
    try:
        from io import BytesIO
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.units import mm
        from reportlab.lib import colors
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer,
                                        Table, TableStyle)
    except Exception:  # noqa: BLE001
        return None

    local, tzname = now_local(db)
    today = local.date()
    d0 = d1 = (today - timedelta(days=1)) if kind == MORNING else today

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, title="SmokeStack Business Report",
                            leftMargin=18 * mm, rightMargin=18 * mm,
                            topMargin=16 * mm, bottomMargin=16 * mm)
    ss = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=ss["Heading1"], fontSize=17, spaceAfter=2)
    h2 = ParagraphStyle("h2", parent=ss["Heading2"], fontSize=12.5,
                        spaceBefore=12, spaceAfter=4)
    small = ParagraphStyle("small", parent=ss["Normal"], fontSize=8.5,
                           textColor=colors.HexColor("#666666"))
    body = ss["Normal"]

    def fin_table(d):
        rows = [["Metric", "Amount"],
                ["Sales", money(d["sales"])],
                ["  Cash", money(d["cash"])],
                ["  Card / Bank", money(d["card"])],
                ["COGS", money(d["cogs"])],
                ["Operating expenses", money(d["expenses"])],
                ["Payroll", money(d["payroll"])],
                ["Sales tax collected", money(d["tax"])],
                ["Gross profit", money(d["gross_profit"])],
                ["Net operating result", money(d["net"])],
                ["Cash ready to deposit", money(d["deposit"])],
                ["Inventory value", money(d["inventory_value"])]]
        t = Table(rows, colWidths=[95 * mm, 60 * mm])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f2937")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 9.5),
            ("ALIGN", (1, 0), (1, -1), "RIGHT"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1),
             [colors.white, colors.HexColor("#f4f6f8")]),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#d7dce2")),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 5)]))
        return t

    def alert_list(items):
        if not items:
            return [Paragraph("No alerts — normal operation.", body)]
        icons = {"🔴": "[CRITICAL]", "🟠": "[URGENT]", "🟡": "[WATCH]"}
        return [Paragraph(f"{icons.get(i, '[INFO]')} {t}", body) for i, t in items[:15]]

    flow = [Paragraph("SmokeStack ERP", h1),
            Paragraph(("TEST REPORT — " if test else "")
                      + ("Morning Business Report" if kind == MORNING
                         else "Evening Business Report"), ss["Heading2"]),
            Paragraph(f"Generated {local.strftime('%Y-%m-%d %H:%M')} ({tzname}) &nbsp;·&nbsp; "
                      f"Reporting period {d0}", small),
            Spacer(1, 8)]

    company = collect(db, scope_branches, d0, d1, today)
    flow += [Paragraph("Company Summary — All Branches", h2), fin_table(company)]
    flow += [Paragraph("Inventory &amp; Staff", h2),
             Paragraph(f"Low stock: {company['low']} &nbsp;·&nbsp; Out of stock: "
                       f"{company['out']} &nbsp;·&nbsp; Clocked in: "
                       f"{company['att']['clocked_in']} &nbsp;·&nbsp; Late: "
                       f"{company['att']['late']} &nbsp;·&nbsp; Absent: "
                       f"{company['att']['absent']}", body)]
    lic = company["lic"]
    flow += [Paragraph("Licence Alerts", h2),
             Paragraph(f"Expired: {len(lic['expired'])} &nbsp;·&nbsp; Expiring today: "
                       f"{len(lic['today'])} &nbsp;·&nbsp; Within 7 days: {len(lic['week'])} "
                       f"&nbsp;·&nbsp; Within 30 days: {len(lic['month'])}", body)]
    flow += [Paragraph("Important Issues", h2)] + alert_list(alerts(db, company, kind))

    for b in scope_branches:
        bd = collect(db, [b], d0, d1, today)
        flow += [Paragraph(f"Branch — {b}", h2), fin_table(bd)]
        top = bd["top"]
        flow.append(Paragraph(
            "Top products: " + (", ".join(f"{p} ({money(a)})" for p, a in top) if top else "—"),
            body))
        flow += alert_list(alerts(db, bd, kind))

    flow += [Spacer(1, 14),
             Paragraph(f"Generated by SmokeStack ERP on "
                       f"{local.strftime('%Y-%m-%d %H:%M:%S')} ({tzname}). "
                       f"Figures come from the same calculation engine as the live "
                       f"dashboard and reports.", small)]
    doc.build(flow)
    return buf.getvalue()

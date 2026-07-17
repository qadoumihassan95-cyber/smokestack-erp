"""SmokeStack ERP — interactive Telegram dashboard (Render Background Worker).

Button-driven UI built on InlineKeyboardMarkup + CallbackQueryHandler. Slash
commands (/start /menu /help /link /me) remain as hidden fallbacks. The bot never
duplicates business logic: it exchanges a linked Telegram id for the user's JWT
(POST /api/telegram/auth-token) and then calls the SAME RBAC-protected FastAPI
endpoints the web app uses, so branch scoping and cost/profit permissions are
enforced by the backend.
"""
import os
import re
import csv
import io
import time
import html
import logging
from urllib.parse import quote
from datetime import date, datetime, timedelta

import httpx
from telegram import (Update, InlineKeyboardButton, InlineKeyboardMarkup,
                      BotCommand, MenuButtonCommands)
from telegram.constants import ParseMode
from telegram.ext import (Application, CommandHandler, CallbackQueryHandler,
                          MessageHandler, ContextTypes, filters)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
# Never log the bot token that python-telegram-bot's HTTP client would otherwise print.
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("smokestack-telegram")

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
API_BASE = os.environ.get("SMOKESTACK_API_BASE", "https://smokestack-api.onrender.com").rstrip("/")

PAGE = 6                     # list page size
STATE = {}                   # tg_id -> per-user nav state (in-memory, server-side)
MONEY = "${:,.0f}"


def money(v):
    try:
        return MONEY.format(float(v or 0))
    except Exception:  # noqa: BLE001
        return "$0"


# ----------------------------------------------------------------------------- API
async def _req(method, path, token=None, body=None, headers=None):
    h = dict(headers or {})
    if token:
        h["Authorization"] = "Bearer " + token
    async with httpx.AsyncClient(timeout=25) as c:
        r = await c.request(method, API_BASE + path, json=body, headers=h)
        txt = r.text
        try:
            data = r.json() if txt else None
        except Exception:  # noqa: BLE001
            data = txt
        return r.status_code, data


def st(tg_id):
    return STATE.setdefault(str(tg_id), {"stack": [], "lists": {}, "last_cb": None})


async def get_ctx(tg_id):
    """Return (token, user, prefs) for a linked user, or (None, None, None)."""
    s = st(tg_id)
    now = time.time()
    if s.get("token") and s.get("token_exp", 0) > now:
        return s["token"], s["user"], s.get("prefs", {})
    status, data = await _req("POST", "/api/telegram/auth-token",
                              body={"tg_id": str(tg_id)}, headers={"X-Bot-Token": TOKEN})
    if status == 200 and data and data.get("access_token"):
        s["token"] = data["access_token"]
        s["user"] = data["user"]
        s["prefs"] = data.get("prefs", {})
        s["token_exp"] = now + 600
        return s["token"], s["user"], s["prefs"]
    return None, None, None


async def can_see_cost(tg_id, token):
    """Backend hides inventory_cost from unauthorized roles — reuse that as the
    single source of truth for the cost/profit permission."""
    status, d = await _req("GET", "/api/reports/dashboard?branch=all", token=token)
    return bool(d and ("inventory_cost" in d)), (d or {})


# ------------------------------------------------------------------- keyboards / ui
def kb(rows):
    return InlineKeyboardMarkup([[InlineKeyboardButton(t, callback_data=c) for (t, c) in row] for row in rows])


def home_kb():
    return kb([
        [("📊 Sales", "nav:sales"), ("💰 Profit", "nav:profit")],
        [("💸 Expenses", "nav:exp"), ("📦 Inventory", "nav:inv")],
        [("⚠️ Low Stock", "inv:low:0"), ("🚫 Out of Stock", "inv:out:0")],
        [("🏪 Branches", "nav:branches"), ("📑 Reports", "nav:reports")],
        [("🔔 Notifications", "nav:ntf"), ("⚙️ Settings", "nav:set")],
        [("🔄 Refresh", "home")],
    ])


def unlinked_kb():
    return kb([[("🔗 Link Account", "link")], [("ℹ️ Help", "help")]])


def footer(extra=None, refresh=None):
    rows = []
    if refresh:
        rows.append([("🔄 Refresh", refresh)])
    rows.append([("⬅️ Back", "back"), ("🏠 Home", "home")])
    return (extra or []) + rows


def period_rows(prefix, extra_top=None):
    rows = extra_top or []
    rows += [
        [("Today", f"{prefix}:today"), ("Yesterday", f"{prefix}:yest")],
        [("Last 7 Days", f"{prefix}:7d"), ("This Month", f"{prefix}:month")],
    ]
    return rows


HOME_TEXT = "🏪 <b>SmokeStack ERP Dashboard</b>\nChoose an option:"


# ---------------------------------------------------------------- date/aggregation
def period_range(key):
    t = date.today()
    if key == "today":
        return t, t, "Today"
    if key == "yest":
        return t - timedelta(days=1), t - timedelta(days=1), "Yesterday"
    if key == "7d":
        return t - timedelta(days=6), t, "Last 7 Days"
    if key == "month":
        return t.replace(day=1), t, "This Month"
    if key == "week":
        return t - timedelta(days=t.weekday()), t, "This Week"
    return t, t, "Today"


def prev_range(start, end):
    span = (end - start).days + 1
    return start - timedelta(days=span), start - timedelta(days=1)


def in_range(rows, start, end):
    out = []
    for r in rows:
        d = (r.get("date") or "")[:10]
        try:
            dd = date.fromisoformat(d)
        except Exception:  # noqa: BLE001
            continue
        if start <= dd <= end:
            out.append(r)
    return out


def agg_sales(rows):
    total = sum(float(r.get("amount") or 0) for r in rows)
    tax = sum(float(r.get("tax") or 0) for r in rows)
    by_branch = {}
    for r in rows:
        by_branch[r["branch"]] = by_branch.get(r["branch"], 0) + float(r.get("amount") or 0)
    return {"total": total, "tax": tax, "tx": len(rows),
            "avg": (total / len(rows)) if rows else 0, "by_branch": by_branch}


def delta_line(cur, prev):
    if not prev:
        return "vs previous: —"
    d = (cur - prev) / prev * 100
    arrow = "▲" if d >= 0 else "▼"
    return f"vs previous: {arrow} {abs(d):.0f}%  ({money(prev)})"


# ------------------------------------------------------------------- flow renderers
async def render_home(tg_id):
    token, user, _ = await get_ctx(tg_id)
    if not token:
        return ("👋 <b>Welcome to SmokeStack ERP</b>\n\nLink your account to run the shop from chat.",
                unlinked_kb(), None)
    return (HOME_TEXT, home_kb(), None)


async def render_sales_menu():
    rows = period_rows("sales")
    rows += [[("By Branch", "sales:branch"), ("Top Products", "sales:top")]]
    return ("📊 <b>Sales</b>\nPick a period:", kb(footer(rows)), None)


async def render_sales(tg_id, key):
    token, user, _ = await get_ctx(tg_id)
    if not token:
        return await render_home(tg_id)
    start, end, label = period_range(key if key in ("today", "yest", "7d", "month") else "today")
    status, rows = await _req("GET", "/api/sales?branch=all", token=token)
    rows = rows or []
    cur = agg_sales(in_range(rows, start, end))
    ps, pe = prev_range(start, end)
    prev = agg_sales(in_range(rows, ps, pe))
    lines = [f"📊 <b>Sales · {label}</b>", ""]
    lines.append(f"Total sales: <b>{money(cur['total'])}</b>")
    lines.append(f"Transactions: <b>{cur['tx']}</b>")
    lines.append(f"Average ticket: <b>{money(cur['avg'])}</b>")
    lines.append(f"Sales tax: {money(cur['tax'])}")
    lines.append(delta_line(cur["total"], prev["total"]))
    if cur["by_branch"]:
        lines.append("\n<b>By branch</b>")
        for b, v in sorted(cur["by_branch"].items(), key=lambda x: -x[1]):
            lines.append(f"• {html.escape(b)}: {money(v)}")
    extra = [[("📄 PDF", f"rep:sales:{key}:pdf"), ("📊 Excel", f"rep:sales:{key}:csv")]]
    return ("\n".join(lines), kb(footer(extra, refresh=f"sales:{key}")), None)


async def render_sales_branch(tg_id):
    token, user, _ = await get_ctx(tg_id)
    if not token:
        return await render_home(tg_id)
    start, end, _ = period_range("month")
    status, rows = await _req("GET", "/api/sales?branch=all", token=token)
    cur = agg_sales(in_range(rows or [], start, end))
    lines = ["📊 <b>Sales by branch · This Month</b>", ""]
    for b, v in sorted(cur["by_branch"].items(), key=lambda x: -x[1]):
        lines.append(f"• {html.escape(b)}: <b>{money(v)}</b>")
    if not cur["by_branch"]:
        lines.append("No sales in range.")
    return ("\n".join(lines), kb(footer(refresh="sales:branch")), None)


async def render_sales_top(tg_id):
    """Sales aren't itemised per product in the ledger, so 'Top Products' ranks by
    current inventory retail value (clearly labelled) rather than fabricating data."""
    token, user, _ = await get_ctx(tg_id)
    if not token:
        return await render_home(tg_id)
    status, prods = await _req("GET", "/api/inventory/products?branch=all", token=token)
    prods = prods or []
    ranked = sorted(prods, key=lambda p: -(float(p.get("price") or 0) * (p.get("total") or 0)))[:8]
    lines = ["📦 <b>Top products</b> <i>(by inventory retail value)</i>", ""]
    for p in ranked:
        val = float(p.get("price") or 0) * (p.get("total") or 0)
        lines.append(f"• {html.escape(p['name'])} — {money(val)}  ({p.get('total', 0)} u)")
    lines.append("\n<i>Per-transaction product sales aren't tracked in this dataset.</i>")
    return ("\n".join(lines), kb(footer(refresh="sales:top")), None)


async def render_profit_menu(tg_id):
    token, _, _ = await get_ctx(tg_id)
    if not token:
        return await render_home(tg_id)
    ok, _ = await can_see_cost(tg_id, token)
    if not ok:
        return ("🔒 <b>Profit</b>\n\nYou don't have permission to view cost and profit figures. "
                "Ask an owner or accountant for access.", kb(footer()), None)
    rows = period_rows("profit")
    return ("💰 <b>Profit</b>\nPick a period:", kb(footer(rows)), None)


async def render_profit(tg_id, key):
    token, user, _ = await get_ctx(tg_id)
    if not token:
        return await render_home(tg_id)
    ok, _dash = await can_see_cost(tg_id, token)
    if not ok:
        return ("🔒 Not permitted to view profit.", kb(footer()), None)
    start, end, label = period_range(key if key in ("today", "yest", "7d", "month") else "today")
    _, sales = await _req("GET", "/api/sales?branch=all", token=token)
    _, exps = await _req("GET", "/api/expenses?branch=all", token=token)
    s = agg_sales(in_range(sales or [], start, end))
    e = sum(float(x.get("amount") or 0) for x in in_range(exps or [], start, end))
    revenue = s["total"]
    net = revenue - s["tax"] - e
    net_margin = (net / revenue * 100) if revenue else 0
    lines = [f"💰 <b>Profit · {label}</b>", ""]
    lines.append(f"Revenue: <b>{money(revenue)}</b>")
    lines.append(f"Sales tax: {money(s['tax'])}")
    lines.append(f"Expenses: {money(e)}")
    lines.append(f"Net profit: <b>{money(net)}</b>")
    lines.append(f"Net margin: <b>{net_margin:.1f}%</b>")
    lines.append("\n<i>Itemised COGS / gross margin require product-level sales, not tracked in this dataset.</i>")
    return ("\n".join(lines), kb(footer(refresh=f"profit:{key}")), None)


async def render_exp_menu():
    rows = [
        [("Today", "exp:today"), ("This Week", "exp:week")],
        [("This Month", "exp:month"), ("By Category", "exp:cat")],
        [("By Branch", "exp:branch"), ("Latest", "exp:latest")],
        [("Largest", "exp:largest")],
    ]
    return ("💸 <b>Expenses</b>\nPick a view:", kb(footer(rows)), None)


async def render_exp(tg_id, key):
    token, user, _ = await get_ctx(tg_id)
    if not token:
        return await render_home(tg_id)
    _, rows = await _req("GET", "/api/expenses?branch=all", token=token)
    rows = rows or []
    if key in ("today", "week", "month"):
        start, end, label = period_range(key)
        sel = in_range(rows, start, end)
        total = sum(float(x.get("amount") or 0) for x in sel)
        lines = [f"💸 <b>Expenses · {label}</b>", "", f"Total: <b>{money(total)}</b>", f"Entries: {len(sel)}"]
        by_cat = {}
        for x in sel:
            by_cat[x.get("category") or "Other"] = by_cat.get(x.get("category") or "Other", 0) + float(x.get("amount") or 0)
        if by_cat:
            lines.append("\n<b>By category</b>")
            for c, v in sorted(by_cat.items(), key=lambda x: -x[1]):
                lines.append(f"• {html.escape(c)}: {money(v)}")
        extra = [[("📄 PDF", f"rep:exp:{key}:pdf"), ("📊 Excel", f"rep:exp:{key}:csv")]]
        return ("\n".join(lines), kb(footer(extra, refresh=f"exp:{key}")), None)
    if key in ("cat", "branch"):
        field = "category" if key == "cat" else "branch"
        agg = {}
        for x in rows:
            k = x.get(field) or "Other"
            agg[k] = agg.get(k, 0) + float(x.get("amount") or 0)
        title = "By category" if key == "cat" else "By branch"
        lines = [f"💸 <b>Expenses · {title}</b>", ""]
        for k, v in sorted(agg.items(), key=lambda x: -x[1]):
            lines.append(f"• {html.escape(str(k))}: <b>{money(v)}</b>")
        if not agg:
            lines.append("No expenses recorded.")
        return ("\n".join(lines), kb(footer(refresh=f"exp:{key}")), None)
    # latest / largest
    srt = sorted(rows, key=(lambda x: x.get("date", "")) if key == "latest" else (lambda x: -float(x.get("amount") or 0)))
    if key == "latest":
        srt = list(reversed(srt))
    lines = [f"💸 <b>{'Latest' if key == 'latest' else 'Largest'} expenses</b>", ""]
    for x in srt[:8]:
        lines.append(f"• {money(x.get('amount'))} — {html.escape(x.get('category') or 'Other')} · {html.escape(x.get('branch') or '')} · {x.get('date', '')[:10]}")
    if not srt:
        lines.append("No expenses recorded.")
    return ("\n".join(lines), kb(footer(refresh=f"exp:{key}")), None)


async def render_inv_menu():
    rows = [
        [("Summary", "inv:summary"), ("Search Product", "inv:search")],
        [("Stock by Branch", "inv:branch"), ("Recently Received", "inv:recv")],
        [("Recent Adjustments", "inv:adj"), ("Recent Transfers", "inv:xfer")],
        [("⚠️ Low Stock", "inv:low:0"), ("🚫 Out of Stock", "inv:out:0")],
    ]
    return ("📦 <b>Inventory</b>\nPick a view:", kb(footer(rows)), None)


async def render_inv_summary(tg_id):
    token, user, _ = await get_ctx(tg_id)
    if not token:
        return await render_home(tg_id)
    ok, dash = await can_see_cost(tg_id, token)
    _, prods = await _req("GET", "/api/inventory/products?branch=all", token=token)
    prods = prods or []
    units = sum(p.get("total") or 0 for p in prods)
    low = sum(1 for p in prods if 0 < (p.get("total") or 0) <= (p.get("min") or 0))
    out = sum(1 for p in prods if (p.get("total") or 0) <= 0)
    lines = ["📦 <b>Inventory summary</b>", "",
             f"Products: <b>{len(prods)}</b>",
             f"Units in stock: <b>{units}</b>",
             f"Low stock: ⚠️ {low}", f"Out of stock: 🚫 {out}"]
    if ok:
        lines.append(f"Inventory cost: {money(dash.get('inventory_cost'))}")
        lines.append(f"Retail value: {money(dash.get('inventory_retail'))}")
    return ("\n".join(lines), kb(footer(refresh="inv:summary")), None)


async def render_inv_branch(tg_id):
    token, user, _ = await get_ctx(tg_id)
    if not token:
        return await render_home(tg_id)
    _, branches = await _req("GET", "/api/branches", token=token)
    _, prods = await _req("GET", "/api/inventory/products?branch=all", token=token)
    lines = ["📦 <b>Stock by branch</b>", ""]
    for b in (branches or []):
        u = sum((p.get("stock") or {}).get(b, 0) for p in (prods or []))
        lines.append(f"• {html.escape(b)}: <b>{u}</b> units")
    return ("\n".join(lines), kb(footer(refresh="inv:branch")), None)


async def render_moves(tg_id, mtype):
    token, user, _ = await get_ctx(tg_id)
    if not token:
        return await render_home(tg_id)
    _, mv = await _req("GET", "/api/inventory/movements?branch=all", token=token)
    mv = [m for m in (mv or []) if m.get("type") == mtype] if mtype in ("receive", "adjust") else \
         [m for m in (mv or []) if "transfer" in (m.get("type") or "")]
    titles = {"receive": "Recently received", "adjust": "Recent adjustments", "transfer": "Recent transfers"}
    lines = [f"📦 <b>{titles.get(mtype, mtype)}</b>", ""]
    for m in mv[:8]:
        chg = m.get("change", 0)
        sign = "+" if chg >= 0 else ""
        lines.append(f"• {html.escape(m.get('sku', ''))} {sign}{chg} @ {html.escape(m.get('branch', ''))} · {str(m.get('date', ''))[:10]}")
    if len(lines) == 2:
        lines.append("Nothing recent.")
    return ("\n".join(lines), kb(footer(refresh=f"inv:{ {'receive':'recv','adjust':'adj','transfer':'xfer'}[mtype] }")), None)


async def render_low_out(tg_id, which, page):
    token, user, _ = await get_ctx(tg_id)
    if not token:
        return await render_home(tg_id)
    _, branches = await _req("GET", "/api/branches", token=token)
    _, prods = await _req("GET", "/api/inventory/products?branch=all", token=token)
    prods = prods or []
    items = []
    for p in prods:
        q = p.get("total") or 0
        if which == "out" and q <= 0:
            items.append(p)
        elif which == "low" and 0 < q <= (p.get("min") or 0):
            items.append(p)
    total = len(items)
    pages = max(1, (total + PAGE - 1) // PAGE)
    page = max(0, min(page, pages - 1))
    st(tg_id)["lists"][which] = [p["sku"] for p in items]
    head = "⚠️ <b>Low stock</b>" if which == "low" else "🚫 <b>Out of stock</b>"
    lines = [f"{head} — {total} item(s)", ""]
    rows = []
    for p in items[page * PAGE:(page + 1) * PAGE]:
        lines.append(f"• <b>{html.escape(p['name'])}</b> ({html.escape(p['sku'])}) — {p.get('total', 0)}/{p.get('min', 0)}")
        rows.append([(f"🔎 {p['name'][:22]}", f"prod:{p['sku']}")])
    if total == 0:
        lines.append("Nothing here. 🎉")
    nav = []
    if page > 0:
        nav.append(("◀️ Prev", f"inv:{which}:{page-1}"))
    if page < pages - 1:
        nav.append(("Next ▶️", f"inv:{which}:{page+1}"))
    extra = rows + ([nav] if nav else [])
    return (f"{chr(10).join(lines)}\n\nPage {page+1}/{pages}", kb(footer(extra, refresh=f"inv:{which}:{page}")), None)


async def render_search_prompt(tg_id):
    st(tg_id)["await_search"] = True
    return ("🔎 <b>Search product</b>\n\nSend a product name, SKU, or barcode.",
            kb(footer()), None)


async def render_search_results(tg_id, query):
    token, user, _ = await get_ctx(tg_id)
    if not token:
        return await render_home(tg_id)
    _, prods = await _req("GET", f"/api/inventory/products?q={quote(query, safe='')}&branch=all", token=token)
    prods = prods or []
    if not prods:
        return (f"No products match “{html.escape(query)}”.", kb(footer()), None)
    rows = [[(f"{p['name'][:26]} ({p['sku']})", f"prod:{p['sku']}")] for p in prods[:8]]
    return (f"🔎 <b>Results for “{html.escape(query)}”</b>\nTap a product:", kb(footer(rows)), None)


async def render_product(tg_id, sku):
    token, user, _ = await get_ctx(tg_id)
    if not token:
        return await render_home(tg_id)
    ok, _ = await can_see_cost(tg_id, token)
    _, prods = await _req("GET", f"/api/inventory/products?q={sku}&branch=all", token=token)
    p = next((x for x in (prods or []) if x.get("sku") == sku), None)
    if not p:
        return ("Product not found.", kb(footer()), None)
    _, mv = await _req("GET", "/api/inventory/movements?branch=all", token=token)
    mine = [m for m in (mv or []) if m.get("sku") == sku]
    last_recv = next((m for m in mine if m.get("type") == "receive"), None)
    lines = [f"📦 <b>{html.escape(p['name'])}</b>", ""]
    lines.append(f"SKU: <code>{html.escape(p['sku'])}</code>")
    lines.append(f"Barcode: {html.escape(p.get('barcode') or '—')}")
    lines.append(f"Supplier: {html.escape(p.get('supplier') or '—')}")
    lines.append(f"Min level: {p.get('min', 0)}")
    if ok:
        lines.append(f"Cost / Price: {money(p.get('cost'))} / {money(p.get('price'))}")
    else:
        lines.append(f"Price: {money(p.get('price'))}")
    lines.append("\n<b>Stock by branch</b>")
    for b, q in (p.get("stock") or {}).items():
        lines.append(f"• {html.escape(b)}: <b>{q}</b>")
    lines.append(f"\nLast received: {str(last_recv['date'])[:10] if last_recv else '—'}")
    return ("\n".join(lines), kb(footer(refresh=f"prod:{sku}")), None)


async def render_branches(tg_id):
    token, user, _ = await get_ctx(tg_id)
    if not token:
        return await render_home(tg_id)
    _, branches = await _req("GET", "/api/branches", token=token)
    rows = [[(f"🏪 {b}", f"br:{b}")] for b in (branches or [])]
    return ("🏪 <b>Branches</b>\nPick a branch:", kb(footer(rows)), None)


async def render_branch(tg_id, branch):
    token, user, _ = await get_ctx(tg_id)
    if not token:
        return await render_home(tg_id)
    ok, _ = await can_see_cost(tg_id, token)
    _, dash = await _req("GET", f"/api/reports/dashboard?branch={quote(branch, safe='')}", token=token)
    dash = dash or {}
    lines = [f"🏪 <b>{html.escape(branch)} · Today</b>", ""]
    lines.append(f"Sales: <b>{money(dash.get('sales_today'))}</b>")
    lines.append(f"Expenses: {money(dash.get('expenses_today'))}")
    if ok:
        lines.append(f"Net profit: <b>{money(dash.get('profit_today'))}</b>")
    lines.append(f"Units in stock: {dash.get('inventory_units', 0)}")
    lines.append(f"Low stock: ⚠️ {dash.get('low', 0)}   Out: 🚫 {dash.get('out', 0)}")
    extra = [[("Change Branch", "nav:branches")]]
    return ("\n".join(lines), kb(footer(extra, refresh=f"br:{branch}")), None)


async def render_reports():
    rows = [
        [("Daily Sales", "rep:daily:today:view"), ("Weekly Sales", "rep:daily:7d:view")],
        [("Monthly Sales", "rep:daily:month:view"), ("Expenses", "rep:exp:month:view")],
        [("Inventory", "rep:inv:_:view"), ("Low Stock", "rep:low:_:view")],
        [("Branch Perf.", "rep:branch:_:view"), ("Payroll", "rep:payroll:_:view")],
    ]
    return ("📑 <b>Reports</b>\nPick a report (then choose a format):", kb(footer(rows)), None)


async def build_report(tg_id, name, period):
    """Returns (title, list-of-rows[[col,col],...]) using real data."""
    token, user, _ = await get_ctx(tg_id)
    if name in ("daily",):
        start, end, label = period_range(period if period in ("today", "7d", "month") else "today")
        _, sales = await _req("GET", "/api/sales?branch=all", token=token)
        a = agg_sales(in_range(sales or [], start, end))
        rows = [["Sales", money(a["total"])], ["Transactions", a["tx"]],
                ["Avg ticket", money(a["avg"])], ["Sales tax", money(a["tax"])]]
        for b, v in sorted(a["by_branch"].items()):
            rows.append([f"Branch {b}", money(v)])
        return f"Sales report · {label}", rows
    if name == "exp":
        start, end, label = period_range(period if period in ("today", "week", "month") else "month")
        _, exps = await _req("GET", "/api/expenses?branch=all", token=token)
        sel = in_range(exps or [], start, end)
        by = {}
        for x in sel:
            by[x.get("category") or "Other"] = by.get(x.get("category") or "Other", 0) + float(x.get("amount") or 0)
        rows = [[c, money(v)] for c, v in sorted(by.items(), key=lambda x: -x[1])]
        rows.append(["TOTAL", money(sum(float(x.get('amount') or 0) for x in sel))])
        return f"Expenses report · {label}", rows
    if name == "inv":
        _, prods = await _req("GET", "/api/inventory/products?branch=all", token=token)
        rows = [[p["sku"], p["name"], p.get("total", 0), p.get("min", 0)] for p in (prods or [])]
        return "Inventory report", [["SKU", "Name", "Qty", "Min"]] + rows
    if name == "low":
        _, prods = await _req("GET", "/api/inventory/products?branch=all", token=token)
        rows = [[p["sku"], p["name"], p.get("total", 0), p.get("min", 0)]
                for p in (prods or []) if 0 < (p.get("total") or 0) <= (p.get("min") or 0)]
        return "Low-stock report", [["SKU", "Name", "Qty", "Min"]] + rows
    if name == "branch":
        _, branches = await _req("GET", "/api/branches", token=token)
        rows = []
        for b in (branches or []):
            _, d = await _req("GET", f"/api/reports/dashboard?branch={b}", token=token)
            d = d or {}
            rows.append([b, money(d.get("sales_today")), money(d.get("expenses_today")), d.get("inventory_units", 0)])
        return "Branch performance · Today", [["Branch", "Sales", "Expenses", "Units"]] + rows
    if name == "payroll":
        t = date.today()
        s = t.replace(day=1).isoformat()
        e = t.isoformat()
        status, pr = await _req("GET", f"/api/payroll?start={s}&end={e}&branch=all", token=token)
        if status != 200 or not isinstance(pr, dict):
            return "Payroll summary", [["Not permitted", ""]]
        rows = [[r["name"], r["branch"], money(r["gross"]), money(r["net"])] for r in pr.get("rows", [])]
        rows.append(["TOTAL gross", "", money(pr.get("gross", 0)), ""])
        return "Payroll summary · This Month", [["Name", "Branch", "Gross", "Net"]] + rows
    if name == "sales":
        return await build_report(tg_id, "daily", period)
    return "Report", [["No data", ""]]


def report_text(title, rows):
    lines = [f"📑 <b>{html.escape(title)}</b>", ""]
    for r in rows[:40]:
        lines.append("  ".join(html.escape(str(c)) for c in r))
    return "\n".join(lines)


def report_csv(title, rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([title])
    for r in rows:
        w.writerow(r)
    return buf.getvalue().encode("utf-8")


def report_pdf(title, rows):
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas
        buf = io.BytesIO()
        c = canvas.Canvas(buf, pagesize=A4)
        w, h = A4
        y = h - 60
        c.setFont("Helvetica-Bold", 15)
        c.drawString(50, y, title)
        y -= 28
        c.setFont("Helvetica", 10)
        for r in rows:
            line = "   ".join(str(x) for x in r)
            c.drawString(50, y, line[:100])
            y -= 16
            if y < 50:
                c.showPage()
                y = h - 60
                c.setFont("Helvetica", 10)
        c.showPage()
        c.save()
        return buf.getvalue()
    except Exception as e:  # noqa: BLE001
        log.warning("PDF gen fallback (%s)", e)
        return None


# ------------------------------------------------------------------- notifications
NTF_KEYS = [("daily_summary", "Daily Summary"), ("weekly_summary", "Weekly Summary"),
            ("low_stock", "Low Stock"), ("out_of_stock", "Out of Stock"),
            ("large_sales", "Large Sales"), ("large_expenses", "Large Expenses")]


async def render_ntf(tg_id):
    token, user, _ = await get_ctx(tg_id)
    if not token:
        return await render_home(tg_id)
    _, data = await _req("GET", "/api/telegram/prefs", token=token)
    prefs = (data or {}).get("prefs", {})
    st(tg_id)["prefs"] = prefs
    rows = []
    for k, label in NTF_KEYS:
        on = bool(prefs.get(k))
        rows.append([(f"{'✅' if on else '❌'} {label}", f"ntf:{k}")])
    rows.append([("Quiet Hours", "ntf:quiet"), ("Branch Scope", "ntf:scope")])
    rows.append([("Language", "ntf:lang")])
    return ("🔔 <b>Notifications</b>\nTap to toggle:", kb(footer(rows)), None)


async def toggle_ntf(tg_id, key):
    token, user, _ = await get_ctx(tg_id)
    if not token:
        return await render_home(tg_id)
    if key in ("quiet", "scope", "lang"):
        return await render_ntf(tg_id)  # informational stubs for now
    _, data = await _req("GET", "/api/telegram/prefs", token=token)
    prefs = (data or {}).get("prefs", {})
    prefs[key] = not bool(prefs.get(key))
    await _req("PUT", "/api/telegram/prefs", token=token, body={key: prefs[key]})
    return await render_ntf(tg_id)


async def render_settings(tg_id):
    token, user, _ = await get_ctx(tg_id)
    if not token:
        return await render_home(tg_id)
    rows = [
        [("My Account", "set:account"), ("Linked Telegram", "set:linked")],
        [("Default Branch", "set:branch"), ("Language", "set:lang")],
        [("Timezone", "set:tz"), ("Notification Settings", "nav:ntf")],
        [("🔌 Unlink Account", "set:unlink")],
    ]
    return ("⚙️ <b>Settings</b>", kb(footer(rows)), None)


async def render_setting(tg_id, key):
    token, user, _ = await get_ctx(tg_id)
    if not token:
        return await render_home(tg_id)
    if key == "account":
        br = ", ".join(user.get("branches") or []) or "All branches"
        txt = (f"👤 <b>My Account</b>\n\nName: {html.escape(user['name'])}\nRole: {user['role']}\nBranches: {br}")
        return (txt, kb(footer()), None)
    if key == "linked":
        _, s = await _req("GET", f"/api/telegram/session/{tg_id}")
        if s and s.get("linked"):
            txt = (f"🔗 <b>Linked Telegram</b>\n\nUsername: @{s.get('username') or '—'}\n"
                   f"Telegram ID: {s.get('tg_id')}\nLinked: {str(s.get('linked_at'))[:10]}")
        else:
            txt = "Not linked."
        return (txt, kb(footer()), None)
    if key == "unlink":
        rows = [[("✅ Yes, unlink", "set:unlink2"), ("Cancel", "nav:set")]]
        return ("⚠️ <b>Unlink this Telegram account?</b>\nThe bot will stop working until you link again.",
                kb(rows), None)
    if key == "unlink2":
        await _req("POST", "/api/telegram/unlink", token=token)
        st(tg_id).clear()
        return ("🔌 Unlinked. Use 🔗 Link Account to reconnect.", unlinked_kb(), None)
    if key in ("branch", "lang", "tz"):
        return ("⚙️ This preference is coming soon. Notification settings are fully editable under 🔔.",
                kb(footer()), None)
    return await render_settings(tg_id)


# -------------------------------------------------------------------- callback router
async def dispatch(tg_id, data):
    """Return (text, keyboard, document-tuple|None). document-tuple = (bytes, filename, caption)."""
    if data == "home":
        st(tg_id)["stack"] = []
        return await render_home(tg_id)
    if data == "link":
        return ("🔗 <b>Link your account</b>\n\n1. Open the web app → Settings → Telegram\n"
                "2. Tap <b>Generate Link Code</b>\n3. Send me:  <code>/link CODE</code>", unlinked_kb(), None)
    if data == "help":
        return ("ℹ️ <b>SmokeStack ERP bot</b>\n\nUse the buttons to view sales, profit, expenses, "
                "inventory, low/out-of-stock, branches, reports and notifications. "
                "Tap ☰ Menu or send /start any time.", unlinked_kb(), None)
    if data == "nav:sales":
        return await render_sales_menu()
    if data == "sales:branch":
        return await render_sales_branch(tg_id)
    if data == "sales:top":
        return await render_sales_top(tg_id)
    if data.startswith("sales:"):
        return await render_sales(tg_id, data.split(":", 1)[1])
    if data == "nav:profit":
        return await render_profit_menu(tg_id)
    if data.startswith("profit:"):
        return await render_profit(tg_id, data.split(":", 1)[1])
    if data == "nav:exp":
        return await render_exp_menu()
    if data.startswith("exp:"):
        return await render_exp(tg_id, data.split(":", 1)[1])
    if data == "nav:inv":
        return await render_inv_menu()
    if data == "inv:summary":
        return await render_inv_summary(tg_id)
    if data == "inv:branch":
        return await render_inv_branch(tg_id)
    if data == "inv:recv":
        return await render_moves(tg_id, "receive")
    if data == "inv:adj":
        return await render_moves(tg_id, "adjust")
    if data == "inv:xfer":
        return await render_moves(tg_id, "transfer")
    if data == "inv:search":
        return await render_search_prompt(tg_id)
    if data.startswith("inv:low:") or data.startswith("inv:out:"):
        _, which, page = data.split(":")
        return await render_low_out(tg_id, which, int(page))
    if data.startswith("prod:"):
        return await render_product(tg_id, data.split(":", 1)[1])
    if data == "nav:branches":
        return await render_branches(tg_id)
    if data.startswith("br:"):
        return await render_branch(tg_id, data.split(":", 1)[1])
    if data == "nav:reports":
        return await render_reports()
    if data.startswith("rep:"):
        parts = data.split(":")
        name, period, fmt = parts[1], parts[2], parts[3]
        title, rows = await build_report(tg_id, name, period)
        if fmt == "view":
            return (report_text(title, rows), kb(footer([[("📄 PDF", f"rep:{name}:{period}:pdf"),
                                                          ("📊 Excel", f"rep:{name}:{period}:csv")]])), None)
        if fmt == "csv":
            return (f"📊 {title} — Excel/CSV attached.", kb(footer()),
                    (report_csv(title, rows), re.sub(r'[^A-Za-z0-9]+', '_', title) + ".csv", title))
        if fmt == "pdf":
            pdf = report_pdf(title, rows)
            if pdf:
                return (f"📄 {title} — PDF attached.", kb(footer()),
                        (pdf, re.sub(r'[^A-Za-z0-9]+', '_', title) + ".pdf", title))
            return (f"📄 {title} (PDF unavailable, sending CSV).", kb(footer()),
                    (report_csv(title, rows), re.sub(r'[^A-Za-z0-9]+', '_', title) + ".csv", title))
    if data == "nav:ntf":
        return await render_ntf(tg_id)
    if data.startswith("ntf:"):
        return await toggle_ntf(tg_id, data.split(":", 1)[1])
    if data == "nav:set":
        return await render_settings(tg_id)
    if data.startswith("set:"):
        return await render_setting(tg_id, data.split(":", 1)[1])
    return await render_home(tg_id)


# -------------------------------------------------------------------- telegram glue
async def show(update, ctx, text, markup, document=None, force_new=False):
    q = getattr(update, "callback_query", None)
    if document:
        buf, fname, caption = document
        await ctx.bot.send_document(chat_id=update.effective_chat.id,
                                    document=io.BytesIO(buf), filename=fname, caption=caption[:1000])
        await ctx.bot.send_message(chat_id=update.effective_chat.id, text=text,
                                   reply_markup=markup, parse_mode=ParseMode.HTML)
        return
    if q and not force_new:
        try:
            await q.edit_message_text(text[:4000], reply_markup=markup, parse_mode=ParseMode.HTML)
            return
        except Exception:  # noqa: BLE001  (e.g. message not modified)
            pass
    await ctx.bot.send_message(chat_id=update.effective_chat.id, text=text[:4000],
                               reply_markup=markup, parse_mode=ParseMode.HTML)


async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    tg_id = str(q.from_user.id)
    s = st(tg_id)
    # 4) answer immediately to stop the spinner
    try:
        await q.answer()
    except Exception:  # noqa: BLE001
        pass
    # 7) prevent duplicate processing of the same callback id
    if s.get("last_cb") == q.id:
        return
    s["last_cb"] = q.id
    data = q.data or "home"
    # Back navigation via server-side stack
    if data == "back":
        if s.get("stack"):
            s["stack"].pop()
        data = s["stack"][-1] if s.get("stack") else "home"
    elif data not in ("home", "refresh"):
        if not s.get("stack") or s["stack"][-1] != data:
            s.setdefault("stack", []).append(data)
    try:
        text, markup, document = await dispatch(tg_id, data)
    except Exception as e:  # noqa: BLE001
        log.exception("callback error: %s", e)
        text, markup, document = ("⚠️ Something went wrong. Returning home.", home_kb(), None)
    await show(update, ctx, text, markup, document)


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    s = st(tg_id)
    if s.get("await_search"):
        s["await_search"] = False
        text, markup, _ = await render_search_results(tg_id, (update.message.text or "").strip())
        await ctx.bot.send_message(chat_id=update.effective_chat.id, text=text[:4000],
                                   reply_markup=markup, parse_mode=ParseMode.HTML)


# --------------------------------------------------------------------- commands
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    st(tg_id)["stack"] = []
    text, markup, _ = await render_home(tg_id)
    await update.message.reply_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ℹ️ Use the buttons. Tap ☰ Menu or /start to open the dashboard.\n"
        "/link CODE — connect your account · /me — your account",
        parse_mode=ParseMode.HTML)


async def cmd_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: /link CODE  (get the code in the web app → Settings → Telegram).")
        return
    code = ctx.args[0].strip()
    u = update.effective_user
    status, data = await _req("POST", "/api/telegram/link/verify",
                              body={"tg_id": str(u.id), "code": code, "device": "Telegram",
                                    "username": u.username or u.full_name})
    st(str(u.id)).pop("token", None)  # force fresh token next call
    if status == 200 and data and data.get("ok"):
        text, markup, _ = await render_home(str(u.id))
        await update.message.reply_text("✅ Linked!", parse_mode=ParseMode.HTML)
        await update.message.reply_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text("❌ That code is invalid or expired. Generate a new one in the web app.")


async def cmd_me(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    _, s = await _req("GET", f"/api/telegram/session/{tg_id}")
    if s and s.get("linked"):
        u = s["user"]
        br = ", ".join(u.get("branches") or []) or "All branches"
        txt = (f"✅ <b>Connected</b>\nName: {u['name']}\nRole: {u['role']}\nBranches: {br}\n"
               f"Telegram: @{s.get('username') or '—'}\nID: {s.get('tg_id')}")
        await update.message.reply_text(txt, parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text("You're not linked. Use /link CODE from the web app → Settings → Telegram.")


async def post_init(app):
    try:
        await app.bot.set_my_commands([
            BotCommand("start", "Open dashboard"), BotCommand("menu", "Open dashboard"),
            BotCommand("help", "Help"), BotCommand("me", "My account"),
        ])
        await app.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    except Exception as e:  # noqa: BLE001
        log.warning("menu button setup: %s", e)


def main():
    if not TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set — configure it on the Render worker.")
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler(["start", "menu"], cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("link", cmd_link))
    app.add_handler(CommandHandler("me", cmd_me))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    log.info("SmokeStack Telegram dashboard starting (long polling); API_BASE=%s", API_BASE)
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()

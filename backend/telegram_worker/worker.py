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
    # Operations-first main menu (Phase 2). Reporting lives under "Reports & Insights".
    return kb([
        [("💸 Add Expense", "op:exp"), ("📦 Inventory", "nav:inv")],
        [("📥 Receive Stock", "op:recv"), ("🔄 Transfer Stock", "op:xfer")],
        [("🧮 Adjust Stock", "op:adj"), ("🛒 Purchases", "op:pur")],
        [("🔍 Search Product", "inv:search"), ("📸 Scan Barcode", "op:scan")],
        [("🕒 Attendance", "att:menu"), ("📊 Reports & Insights", "reports_home")],
        [("⚙️ Settings", "nav:set"), ("🔄 Refresh", "home")],
    ])


def reports_home_kb():
    return kb([
        [("📊 Sales", "nav:sales"), ("💰 Profit", "nav:profit")],
        [("💸 Expenses", "nav:exp"), ("📦 Inventory", "nav:inv")],
        [("⚠️ Low Stock", "inv:low:0"), ("🚫 Out of Stock", "inv:out:0")],
        [("🏪 Branches", "nav:branches"), ("📑 Reports", "nav:reports")],
        [("📄 Documents", "nav:lic"), ("🔔 Notifications", "nav:ntf")],
        [("⬅️ Back", "home"), ("🏠 Home", "home")],
    ])


async def render_licenses(tg_id):
    token, user, _ = await get_ctx(tg_id)
    if not token:
        return await render_home(tg_id)
    try:
        _, al = await _req("GET", "/api/licenses/alerts", token=token)
    except Exception:  # noqa: BLE001
        al = None
    al = al or {"count": 0, "items": []}
    lines = ["📄 <b>Licenses &amp; Documents</b>", ""]
    if not al.get("count"):
        lines.append("✅ All documents are valid — nothing expiring soon.")
    else:
        lines.append(f"⚠️ <b>{al['count']}</b> document(s) need attention:")
        for i in al.get("items", [])[:12]:
            d = i.get("days_to_expiry")
            when = "expired" if (d is not None and d < 0) else (f"{d} days left" if d is not None else "no date")
            lines.append(f"• <b>{html.escape(i.get('name') or '')}</b> ({html.escape(i.get('branch') or 'All')}) — {when}")
    return ("\n".join(lines), kb(footer(refresh="nav:lic")), None)


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
        [("🔍 Search Product", "inv:search"), ("📦 Stock by Branch", "inv:branch")],
        [("⚠️ Low Stock", "inv:low:0"), ("🚫 Out of Stock", "inv:out:0")],
        [("📥 Receive Stock", "op:recv"), ("🧮 Adjust Stock", "op:adj")],
        [("🔄 Transfer Stock", "op:xfer"), ("🧾 Recent Movements", "inv:recv")],
        [("Recent Adjustments", "inv:adj"), ("Recent Transfers", "inv:xfer")],
    ]
    return ("📦 <b>Inventory</b>\nPick a view or action:", kb(footer(rows)), None)


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
    actions = [
        [("📥 Receive", f"opf:recv:{sku}"), ("🧮 Adjust", f"opf:adj:{sku}")],
        [("🔄 Transfer", f"opf:xfer:{sku}"), ("🧾 History", f"hist:{sku}")],
    ]
    return ("\n".join(lines), kb(footer(actions, refresh=f"prod:{sku}")), None)


async def render_history(tg_id, sku):
    token, user, _ = await get_ctx(tg_id)
    if not token:
        return await render_home(tg_id)
    _, mv = await _req("GET", "/api/inventory/movements?branch=all", token=token)
    mine = [m for m in (mv or []) if m.get("sku") == sku][:10]
    lines = [f"🧾 <b>History · {html.escape(sku)}</b>", ""]
    for m in mine:
        chg = m.get("change", 0)
        sign = "+" if chg >= 0 else ""
        lines.append(f"• {m.get('type')}: {sign}{chg} → {m.get('after')} @ {html.escape(m.get('branch',''))} · {str(m.get('date',''))[:10]}")
    if len(lines) == 2:
        lines.append("No movements recorded.")
    return ("\n".join(lines), kb(footer(refresh=f"hist:{sku}")), None)


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


# =========================================================== PHASE 2 — operations
# Role → the ops permissions the buttons gate on (mirrors backend PERMS; the backend
# is the hard enforcement — these just hide/deny buttons early for good UX).
ROLE_OPS = {
    "owner": {"create", "continuous_receiving", "adjust_stock", "transfer_stock", "view_cost"},
    "admin": {"create", "continuous_receiving", "adjust_stock", "transfer_stock", "view_cost"},
    "branch_manager": {"create", "continuous_receiving", "adjust_stock", "transfer_stock", "view_cost"},
    "manager": {"create", "continuous_receiving", "adjust_stock", "transfer_stock", "view_cost"},
    "inventory_manager": {"create", "continuous_receiving", "adjust_stock", "transfer_stock", "view_cost"},
    "accountant": {"create", "view_cost"},
    "cashier": {"create"},
    "employee": set(),
}
CATS = ["Rent", "Utilities", "Payroll", "Transport", "Maintenance", "Supplies", "Marketing", "Taxes", "Other"]
PAYS = ["Cash", "Card", "Bank Transfer", "Check", "Other"]
ADJ_REASONS = ["Count correction", "Damaged", "Expired", "Lost", "Found", "Returned", "Manual correction", "Other"]
FLOW_TITLES = {"exp": "💸 <b>Add Expense</b>", "recv": "📥 <b>Receive Stock</b>",
               "adj": "🧮 <b>Adjust Stock</b>", "xfer": "🔄 <b>Transfer Stock</b>", "pur": "🛒 <b>Add Purchase</b>"}
FLOW_PERM = {"exp": "create", "recv": "continuous_receiving", "adj": "adjust_stock", "xfer": "transfer_stock", "pur": "create"}
MAX_AMOUNT = 10_000_000
MAX_QTY = 1_000_000


def has(role, perm):
    return perm in ROLE_OPS.get(role, set())


def op_footer():
    return [[("❌ Cancel", "f:cancel"), ("⬅️ Step Back", "f:sb")], [("🏠 Home", "home")]]


def flow_start(tg_id, name):
    s = st(tg_id)
    s["flow"] = {"name": name, "step": 0, "data": {}, "started": time.time(), "submitting": False, "_awaiting": None}
    return s["flow"]


def flow(tg_id):
    s = st(tg_id)
    f = s.get("flow")
    if f and time.time() - f.get("started", 0) > 900:   # 15-min timeout
        s.pop("flow", None)
        return None
    return f


def flow_end(tg_id):
    st(tg_id).pop("flow", None)


class ApiErr(Exception):
    def __init__(self, status, data):
        self.status = status
        self.data = data

    def friendly(self):
        if self.status == 403:
            return "You don't have permission for this action."
        if self.status == 404:
            return "Not found (product or branch)."
        if self.status == 422:
            d = self.data.get("detail") if isinstance(self.data, dict) else None
            return "Invalid data: " + (str(d) if d else "please check your input.")
        return "The server rejected this request. Please try again."


class FlowErr(Exception):
    pass


def steps_for(name):
    if name == "exp":
        return [{"k": "branch", "kind": "branch", "prompt": "Select branch:"},
                {"k": "category", "kind": "choice", "prompt": "Select category:", "choices": [(c, c) for c in CATS]},
                {"k": "amount", "kind": "amount", "prompt": "Enter the amount (numbers only):"},
                {"k": "account", "kind": "choice", "prompt": "Payment method:", "choices": [(c, c) for c in PAYS]},
                {"k": "memo", "kind": "text", "prompt": "Add notes / description (required if category is Other):", "optional": True},
                {"k": "receipt", "kind": "file", "prompt": "Upload a receipt (photo/PDF), or Skip:", "optional": True},
                {"k": "confirm", "kind": "confirm"}]
    if name == "recv":
        return [{"k": "sku", "kind": "product", "prompt": "Find the product to receive:"},
                {"k": "branch", "kind": "branch", "prompt": "Destination branch:"},
                {"k": "qty", "kind": "qty", "prompt": "Quantity received:"},
                {"k": "supplier", "kind": "text", "prompt": "Supplier (or Skip):", "optional": True},
                {"k": "unit_cost", "kind": "cost", "prompt": "Unit cost (or Skip):", "optional": True},
                {"k": "invoice", "kind": "text", "prompt": "Invoice # (or Skip):", "optional": True},
                {"k": "receipt", "kind": "file", "prompt": "Upload invoice (photo/PDF) or Skip:", "optional": True},
                {"k": "confirm", "kind": "confirm"}]
    if name == "adj":
        return [{"k": "sku", "kind": "product", "prompt": "Find the product to adjust:"},
                {"k": "branch", "kind": "branch", "prompt": "Branch:"},
                {"k": "adjtype", "kind": "choice", "prompt": "Adjustment type:",
                 "choices": [("➕ Increase", "inc"), ("➖ Decrease", "dec"), ("🎯 Set exact", "set")]},
                {"k": "qty", "kind": "qty", "prompt": "Quantity:"},
                {"k": "reason", "kind": "choice", "prompt": "Reason:", "choices": [(r, r) for r in ADJ_REASONS]},
                {"k": "memo", "kind": "text", "prompt": "Notes (or Skip):", "optional": True},
                {"k": "confirm", "kind": "confirm"}]
    if name == "xfer":
        return [{"k": "sku", "kind": "product", "prompt": "Find the product to transfer:"},
                {"k": "from", "kind": "branch", "prompt": "Source branch:"},
                {"k": "to", "kind": "branch", "prompt": "Destination branch:", "exclude": "from"},
                {"k": "qty", "kind": "qty", "prompt": "Transfer quantity:"},
                {"k": "memo", "kind": "text", "prompt": "Notes (or Skip):", "optional": True},
                {"k": "confirm", "kind": "confirm"}]
    if name == "pur":
        return [{"k": "branch", "kind": "branch", "prompt": "Select branch:"},
                {"k": "vendor", "kind": "text", "prompt": "Supplier / vendor name:"},
                {"k": "amount", "kind": "amount", "prompt": "Total purchase amount:"},
                {"k": "invoice", "kind": "text", "prompt": "Invoice # (or Skip):", "optional": True},
                {"k": "confirm", "kind": "confirm"}]
    return []


def _validate(kind, text):
    t = (text or "").strip()
    if kind in ("amount", "cost"):
        try:
            v = float(t.replace(",", "").replace("$", ""))
        except Exception:  # noqa: BLE001
            return None, "Please enter a number."
        if v < 0:
            return None, "Amount cannot be negative."
        if kind == "amount" and v <= 0:
            return None, "Amount must be greater than zero."
        if v > MAX_AMOUNT:
            return None, "That amount looks too large."
        return v, None
    if kind == "qty":
        try:
            v = int(float(t))
        except Exception:  # noqa: BLE001
            return None, "Please enter a whole number."
        if v <= 0:
            return None, "Quantity must be a positive whole number."
        if v > MAX_QTY:
            return None, "That quantity looks too large."
        return v, None
    if not t:
        return None, "Please enter a value."
    return t, None


async def bot_audit(tg_id, user, action, entity, ref, detail, result="ok"):
    try:
        await _req("POST", "/api/telegram/audit", headers={"X-Bot-Token": TOKEN},
                   body={"tg_id": str(tg_id), "user_id": (user or {}).get("id"), "action": action,
                         "entity": entity, "ref": str(ref or ""), "detail": detail, "result": result})
    except Exception as e:  # noqa: BLE001
        log.warning("bot_audit failed: %s", e)


async def _product_qty(token, sku, branch):
    _, prods = await _req("GET", f"/api/inventory/products?q={quote(sku)}&branch=all", token=token)
    p = next((x for x in (prods or []) if x.get("sku") == sku), None)
    return int((p.get("stock") or {}).get(branch, 0)) if p else 0


async def start_flow(tg_id, name, sku=None):
    token, user, _ = await get_ctx(tg_id)
    if not token:
        return await render_home(tg_id)
    if not has(user["role"], FLOW_PERM[name]):
        return (f"🔒 You don't have permission for this action ({name}).",
                kb([[("🏠 Main Menu", "home")]]), None)
    f = flow_start(tg_id, name)
    if sku:
        _, prods = await _req("GET", f"/api/inventory/products?q={quote(sku)}&branch=all", token=token)
        p = next((x for x in (prods or []) if x.get("sku") == sku), None)
        f["data"]["sku"] = sku
        f["data"]["pname"] = p["name"] if p else sku
        f["step"] = 1  # skip the product-search step
    return await flow_render(tg_id)


async def flow_render(tg_id):
    f = flow(tg_id)
    if not f:
        return await render_home(tg_id)
    token, user, _ = await get_ctx(tg_id)
    if not token:
        flow_end(tg_id)
        return await render_home(tg_id)
    steps = steps_for(f["name"])
    i = f["step"]
    if i >= len(steps):
        return await flow_submit(tg_id)
    stp = steps[i]
    kind = stp["kind"]
    head = f"{FLOW_TITLES[f['name']]}\n<i>Step {i + 1}/{len(steps)}</i>\n\n{stp.get('prompt', '')}"
    if kind == "branch":
        _, branches = await _req("GET", "/api/branches", token=token)
        branches = branches or []
        if "exclude" in stp:
            branches = [b for b in branches if b != f["data"].get(stp["exclude"])]
        f["data"]["_branches"] = branches
        rows = [[(f"🏪 {b}", f"f:pick:{idx}")] for idx, b in enumerate(branches)]
        f["_awaiting"] = None
        return (head, kb(rows + op_footer()), None)
    if kind == "choice":
        f["data"]["_choices"] = [val for (_lbl, val) in stp["choices"]]
        rows = [[(lbl, f"f:pick:{idx}")] for idx, (lbl, _val) in enumerate(stp["choices"])]
        f["_awaiting"] = None
        return (head, kb(rows + op_footer()), None)
    if kind == "product":
        f["_awaiting"] = "search"
        return (head + "\n\nType a name, SKU, or barcode.", kb(op_footer()), None)
    if kind in ("amount", "qty", "cost", "text"):
        f["_awaiting"] = kind
        extra = [[("⏭️ Skip", "f:skip")]] if stp.get("optional") else []
        return (head, kb(extra + op_footer()), None)
    if kind == "file":
        f["_awaiting"] = "file"
        return (head, kb([[("⏭️ Skip", "f:skip")]] + op_footer()), None)
    if kind == "confirm":
        return await flow_confirm_screen(tg_id)
    return await render_home(tg_id)


async def flow_confirm_screen(tg_id):
    f = flow(tg_id)
    d = f["data"]
    name = f["name"]
    L = [FLOW_TITLES[name], "<b>Please confirm:</b>", ""]

    def add(k, v):
        L.append(f"{k}: <b>{html.escape(str(v))}</b>")

    if name == "exp":
        add("Branch", d["branch"]); add("Category", d["category"]); add("Amount", money(d["amount"]))
        if d["category"] == "Other":
            add("Description", d.get("memo") or "—")
        add("Payment", d.get("account") or "—"); add("Notes", d.get("memo") or "—")
        add("Receipt", "attached" if d.get("receipt") else "none")
    elif name == "recv":
        add("Product", d["pname"]); add("Branch", d["branch"]); add("Quantity", d["qty"])
        add("Unit cost", money(d["unit_cost"]) if d.get("unit_cost") is not None else "—")
        add("Supplier", d.get("supplier") or "—"); add("Invoice", d.get("invoice") or "—")
        add("File", "attached" if d.get("receipt") else "none")
    elif name == "adj":
        tt = {"inc": "Increase", "dec": "Decrease", "set": "Set exact"}[d["adjtype"]]
        add("Product", d["pname"]); add("Branch", d["branch"]); add("Type", tt)
        add("Quantity", d["qty"]); add("Reason", d["reason"]); add("Notes", d.get("memo") or "—")
    elif name == "xfer":
        add("Product", d["pname"]); add("From", d["from"]); add("To", d["to"])
        add("Quantity", d["qty"]); add("Notes", d.get("memo") or "—")
    elif name == "pur":
        add("Branch", d["branch"]); add("Vendor", d["vendor"]); add("Amount", money(d["amount"]))
        add("Invoice", d.get("invoice") or "—")
    kbb = kb([[("✅ Confirm", "f:go"), ("✏️ Edit", "f:edit")], [("❌ Cancel", "f:cancel")]])
    return ("\n".join(L), kbb, None)


async def flow_submit(tg_id):
    f = flow(tg_id)
    if not f:
        return await render_home(tg_id)
    if f.get("submitting"):
        return ("⏳ Processing… please wait.", kb([[("🏠 Main Menu", "home")]]), None)
    f["submitting"] = True
    token, user, _ = await get_ctx(tg_id)
    name, d = f["name"], f["data"]
    try:
        if name == "exp":
            # "Other" requires a specific description — use the notes as the description.
            custom = (d.get("memo") or "").strip() if d["category"] == "Other" else None
            if d["category"] == "Other" and not custom:
                f["submitting"] = False
                f["step"] = 4   # jump back to the notes/description step
                return ("✏️ Since the category is <b>Other</b>, please type the exact expense "
                        "(e.g. Cleaning materials, Employee meal, Delivery fee):",
                        kb([[("❌ Cancel", "f:cancel")]]), None)
            memo = d.get("memo") or ""
            if d.get("receipt"):
                memo = (memo + f" [receipt {d['receipt']['kind']} {d['receipt']['id'][:16]} {d['receipt']['size']}b]").strip()
            stx, res = await _req("POST", "/api/expenses", token=token,
                                  body={"branch": d["branch"], "category": d["category"],
                                        "amount": d["amount"], "account": d.get("account"), "memo": memo,
                                        "custom_description": custom})
            if stx >= 400:
                raise ApiErr(stx, res)
            rid = res.get("id")
            label = custom or d["category"]
            await bot_audit(tg_id, user, "create", "expense", rid,
                            f"{label} {money(d['amount'])} @ {d['branch']} pay:{d.get('account')}")
            txt = (f"✅ <b>Expense saved</b>\nID: <code>{rid}</code>\nBranch: {d['branch']}\n"
                   f"Category: {d['category']}" + (f"\nDescription: {html.escape(custom)}" if custom else "") + "\n"
                   f"Amount: {money(d['amount'])}\n"
                   f"By: {user['name']}\n{datetime.utcnow().strftime('%b %d, %Y %H:%M UTC')}")
        elif name == "recv":
            reason = " · ".join([x for x in [
                (f"Supplier {d['supplier']}" if d.get("supplier") else None),
                (f"Inv {d['invoice']}" if d.get("invoice") else None),
                (f"receipt {d['receipt']['id'][:16]}" if d.get("receipt") else None)] if x])
            body = {"sku": d["sku"], "branch": d["branch"], "qty": int(d["qty"]), "reason": reason or None}
            if d.get("unit_cost") is not None:
                body["unit_cost"] = d["unit_cost"]
            stx, res = await _req("POST", "/api/inventory/receive", token=token, body=body)
            if stx >= 400:
                raise ApiErr(stx, res)
            await bot_audit(tg_id, user, "receive", "product", d["sku"],
                            f"+{d['qty']} @ {d['branch']} new={res.get('new_stock')}")
            txt = (f"✅ <b>Stock received</b>\nProduct: {d['pname']}\nBranch: {d['branch']}\n"
                   f"Received: +{d['qty']} → new stock <b>{res.get('new_stock')}</b>")
        elif name == "adj":
            cur = await _product_qty(token, d["sku"], d["branch"])
            if d["adjtype"] == "inc":
                delta = int(d["qty"])
            elif d["adjtype"] == "dec":
                delta = -int(d["qty"])
            else:
                delta = int(d["qty"]) - cur
            reason = d["reason"] + ((" · " + d["memo"]) if d.get("memo") else "")
            stx, res = await _req("POST", "/api/inventory/adjust", token=token,
                                  body={"sku": d["sku"], "branch": d["branch"], "qty": delta, "reason": reason})
            if stx >= 400:
                raise ApiErr(stx, res)
            await bot_audit(tg_id, user, "adjust", "product", d["sku"],
                            f"{cur}->{res.get('new_stock')} @ {d['branch']} ({d['reason']})")
            txt = (f"✅ <b>Stock adjusted</b>\nProduct: {d['pname']}\nBranch: {d['branch']}\n"
                   f"Old: {cur} → New: <b>{res.get('new_stock')}</b>\nReason: {d['reason']}")
        elif name == "xfer":
            avail = await _product_qty(token, d["sku"], d["from"])
            if int(d["qty"]) > avail:
                raise FlowErr(f"Only {avail} available at {d['from']} — reduce the quantity.")
            stx, res = await _req("POST", "/api/transfers", token=token,
                                  body={"sku": d["sku"], "from_branch": d["from"], "to_branch": d["to"], "qty": int(d["qty"])})
            if stx >= 400:
                raise ApiErr(stx, res)
            await bot_audit(tg_id, user, "create", "transfer", res.get("id"),
                            f"{d['qty']}x {d['sku']} {d['from']}->{d['to']}")
            txt = (f"✅ <b>Transfer submitted</b> · status <b>{res.get('status')}</b>\n"
                   f"Product: {d['pname']}\n{d['from']} → {d['to']}\nQty: {d['qty']}\n"
                   f"Awaiting manager approval.")
        elif name == "pur":
            stx, res = await _req("POST", "/api/purchases", token=token,
                                  body={"vendor": d["vendor"], "branch": d["branch"], "amount": d["amount"]})
            if stx >= 400:
                raise ApiErr(stx, res)
            await bot_audit(tg_id, user, "create", "purchase", res.get("id"),
                            f"{d['vendor']} {money(d['amount'])} @ {d['branch']}")
            txt = (f"✅ <b>Purchase submitted</b> · status <b>{res.get('status')}</b>\n"
                   f"ID: <code>{res.get('id')}</code>\nVendor: {d['vendor']}\nBranch: {d['branch']}\n"
                   f"Amount: {money(d['amount'])}\nAwaiting approval.")
        else:
            txt = "Done."
        flow_end(tg_id)
        return (txt, kb([[("🏠 Main Menu", "home")]]), None)
    except (ApiErr, FlowErr) as e:
        f["submitting"] = False
        msg = e.friendly() if isinstance(e, ApiErr) else str(e)
        try:
            await bot_audit(tg_id, user, "denied" if isinstance(e, ApiErr) and e.status == 403 else "error",
                            name, "", msg, result="fail")
        except Exception:  # noqa: BLE001
            pass
        return (f"⚠️ {msg}", kb([[("🔁 Try Again", "f:go")], [("🏠 Main Menu", "home")]]), None)
    except Exception as e:  # noqa: BLE001
        f["submitting"] = False
        log.exception("submit error: %s", e)
        return ("⚠️ Something went wrong. Please try again.",
                kb([[("🔁 Try Again", "f:go")], [("🏠 Main Menu", "home")]]), None)


async def flow_cb(tg_id, action):
    f = flow(tg_id)
    if not f:
        return ("⏳ That action expired. Back to the menu.", home_kb(), None)
    steps = steps_for(f["name"])
    stp = steps[f["step"]] if f["step"] < len(steps) else None
    if action == "cancel":
        _, user, _ = await get_ctx(tg_id)
        await bot_audit(tg_id, user, "cancel", f["name"], "", "user cancelled", result="cancelled")
        flow_end(tg_id)
        return ("❌ Cancelled.", kb([[("🏠 Main Menu", "home")]]), None)
    if action == "sb":
        f["step"] = max(0, f["step"] - 1)
        f["submitting"] = False
        return await flow_render(tg_id)
    if action == "edit":
        f["step"] = 0
        f["submitting"] = False
        return await flow_render(tg_id)
    if action == "go":
        return await flow_submit(tg_id)
    if action == "skip":
        if stp and stp.get("optional"):
            f["data"][stp["k"]] = None
            f["step"] += 1
            f["_awaiting"] = None
        return await flow_render(tg_id)
    if action.startswith("pick:") and stp:
        idx = int(action.split(":")[1])
        if stp["kind"] == "branch":
            f["data"][stp["k"]] = f["data"]["_branches"][idx]
        elif stp["kind"] == "choice":
            f["data"][stp["k"]] = f["data"]["_choices"][idx]
        f["step"] += 1
        f["_awaiting"] = None
        return await flow_render(tg_id)
    return await flow_render(tg_id)


async def flow_pick_product(tg_id, sku):
    f = flow(tg_id)
    if not f:
        return await render_home(tg_id)
    token, _, _ = await get_ctx(tg_id)
    _, prods = await _req("GET", f"/api/inventory/products?q={quote(sku)}&branch=all", token=token)
    p = next((x for x in (prods or []) if x.get("sku") == sku), None)
    f["data"]["sku"] = sku
    f["data"]["pname"] = p["name"] if p else sku
    f["step"] += 1
    f["_awaiting"] = None
    return await flow_render(tg_id)


async def flow_text(tg_id, text):
    f = flow(tg_id)
    if not f or not f.get("_awaiting"):
        return None
    aw = f["_awaiting"]
    steps = steps_for(f["name"])
    stp = steps[f["step"]]
    if aw == "search":
        token, _, _ = await get_ctx(tg_id)
        _, prods = await _req("GET", f"/api/inventory/products?q={quote(text.strip())}&branch=all", token=token)
        prods = prods or []
        if not prods:
            return (f"No products match “{html.escape(text)}”. Type another name/SKU/barcode:",
                    kb(op_footer()), None)
        rows = [[(f"{p['name'][:26]} ({p['sku']})", f"fp:{p['sku']}")] for p in prods[:8]]
        return ("Tap the product:", kb(rows + op_footer()), None)
    val, err = _validate(aw, text)
    if err:
        extra = [[("⏭️ Skip", "f:skip")]] if stp.get("optional") else []
        return (f"⚠️ {err}\n\n{stp.get('prompt', '')}", kb(extra + op_footer()), None)
    f["data"][stp["k"]] = val
    f["step"] += 1
    f["_awaiting"] = None
    return await flow_render(tg_id)


async def flow_file(tg_id, meta):
    f = flow(tg_id)
    if not f or f.get("_awaiting") != "file":
        return None
    steps = steps_for(f["name"])
    stp = steps[f["step"]]
    f["data"][stp["k"]] = meta          # {kind, id, size} — metadata only, binary stays in Telegram
    f["step"] += 1
    f["_awaiting"] = None
    _, user, _ = await get_ctx(tg_id)
    await bot_audit(tg_id, user, "attach", f["name"], meta["id"][:20], f"{meta['kind']} {meta['size']}b")
    return await flow_render(tg_id)


async def render_purchases(tg_id):
    token, user, _ = await get_ctx(tg_id)
    if not token:
        return await render_home(tg_id)
    rows = [[("➕ Add Purchase", "op:puradd"), ("📋 Recent Purchases", "op:purlist")]]
    return ("🛒 <b>Purchases</b>", kb(footer(rows)), None)


async def render_purchase_list(tg_id):
    token, user, _ = await get_ctx(tg_id)
    _, ps = await _req("GET", "/api/purchases?branch=all", token=token)
    lines = ["🛒 <b>Recent purchases</b>", ""]
    for p in (ps or [])[:8]:
        lines.append(f"• <code>{p.get('id')}</code> — {html.escape(p.get('vendor') or '')} · "
                     f"{money(p.get('amount'))} · {p.get('status')}")
    if len(lines) == 2:
        lines.append("No purchases yet.")
    return ("\n".join(lines), kb(footer(refresh="op:purlist")), None)


async def render_scan(tg_id):
    s = st(tg_id)
    s.pop("flow", None)
    s["await_barcode"] = True
    return ("📸 <b>Scan / enter barcode</b>\n\nSend the barcode <b>number</b> as text.\n"
            "<i>Photo barcode reading isn't reliable yet — if you send a photo I'll ask you to type the number.</i>",
            kb(op_footer()), None)


async def barcode_lookup(tg_id, code):
    token, _, _ = await get_ctx(tg_id)
    st(tg_id)["await_barcode"] = False
    status, p = await _req("GET", f"/api/inventory/barcode/{quote(code.strip())}", token=token)
    if status == 200 and p and p.get("sku"):
        return await render_product(tg_id, p["sku"])
    # fall back to a normal search
    _, prods = await _req("GET", f"/api/inventory/products?q={quote(code.strip())}&branch=all", token=token)
    prods = prods or []
    if not prods:
        return (f"No product with barcode/keyword “{html.escape(code)}”.", home_kb(), None)
    rows = [[(f"{p['name'][:26]} ({p['sku']})", f"prod:{p['sku']}")] for p in prods[:8]]
    return ("🔎 Matches:", kb(footer(rows)), None)


# ========================================================= PHASE 1 — attendance (GPS)
_APP = None  # set in post_init; used to push manager/employee notifications
ATT_APPROVE = {"owner", "admin", "branch_manager", "manager", "accountant"}
ATT_CONSENT_TEXT = (
    "📍 <b>Location &amp; privacy</b>\n\n"
    "SmokeStack uses your current location <b>only</b> when you tap Clock In or Clock Out, "
    "to verify you're near your assigned branch. It never tracks you continuously and stores "
    "nothing beyond each punch.\n\nTap Continue to enable attendance."
)


def can_approve(role):
    return role in ATT_APPROVE


def _hm(iso):
    if not iso:
        return "—"
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%H:%M UTC")
    except Exception:  # noqa: BLE001
        return "—"


async def render_att_menu(tg_id):
    token, user, _ = await get_ctx(tg_id)
    if not token:
        return await render_home(tg_id)
    rows = [[("🟢 Clock In", "att:in"), ("🔴 Clock Out", "att:out")],
            [("🕒 My Attendance", "att:me:today"), ("📍 My Branch", "att:branch")],
            [("📅 Today’s Status", "att:today")]]
    if can_approve(user["role"]):
        rows.append([("⚠️ Approvals", "att:pending")])
    return ("🕒 <b>Attendance</b>\nClock in / out with your current location.", kb(footer(rows)), None)


async def render_att_today(tg_id):
    token, user, _ = await get_ctx(tg_id)
    if not token:
        return await render_home(tg_id)
    _, d = await _req("GET", "/api/attendance/today", token=token)
    d = d or {}
    labels = {"none": "Not clocked in", "active": "Currently clocked in", "completed": "Clocked out",
              "pending": "Pending manager approval", "rejected": "Rejected"}
    stt = d.get("state", "none")
    lines = ["📅 <b>Today’s status</b>", "", f"State: <b>{labels.get(stt, stt)}</b>"]
    if stt != "none":
        lines.append(f"Branch: {html.escape(str(d.get('branch') or '—'))}")
        if d.get("clock_in"):
            lines.append(f"Clock-in: {_hm(d['clock_in'])}")
        if d.get("clock_out"):
            lines.append(f"Clock-out: {_hm(d['clock_out'])}")
        if d.get("worked_minutes") is not None:
            wm = d["worked_minutes"]
            lines.append(f"Worked: {wm // 60}h {wm % 60}m")
        if d.get("ci_dist") is not None:
            lines.append(f"Distance: {d['ci_dist']} m")
    return ("\n".join(lines), kb(footer(refresh="att:today")), None)


async def render_att_me(tg_id, period):
    token, user, _ = await get_ctx(tg_id)
    if not token:
        return await render_home(tg_id)
    _, rows = await _req("GET", f"/api/attendance/me?period={period}", token=token)
    lines = [f"🕒 <b>My attendance · {period}</b>", ""]
    for a in (rows or [])[:10]:
        wm = a.get("worked_minutes")
        dur = f"{wm // 60}h{wm % 60}m" if wm is not None else "—"
        lines.append(f"• {str(a.get('clock_in'))[:10]} {html.escape(a['branch'])} · {a['status']} ({dur})")
    if len(lines) == 2:
        lines.append("No records.")
    tabs = [[("Today", "att:me:today"), ("This Week", "att:me:week"), ("This Month", "att:me:month")]]
    return ("\n".join(lines), kb(footer(tabs, refresh=f"att:me:{period}")), None)


async def render_att_branch(tg_id):
    token, user, _ = await get_ctx(tg_id)
    if not token:
        return await render_home(tg_id)
    _, branches = await _req("GET", "/api/branches", token=token)
    lines = ["📍 <b>My branch(es)</b>", ""]
    for b in (branches or []):
        _, s = await _req("GET", f"/api/attendance/branch/{quote(b)}", token=token)
        if s:
            coords = "set" if s.get("lat") is not None else "not set"
            lines.append(f"• {html.escape(b)} — radius {s['radius_m']} m · coords {coords} · "
                         f"verify {'on' if s.get('loc_verify') else 'off'}")
    return ("\n".join(lines), kb(footer(refresh="att:branch")), None)


async def render_att_pending(tg_id):
    token, user, _ = await get_ctx(tg_id)
    if not token or not can_approve(user["role"]):
        return ("🔒 Not permitted.", kb(footer()), None)
    _, rows = await _req("GET", "/api/attendance/pending", token=token)
    rows = rows or []
    if not rows:
        return ("✅ No pending attendance approvals.", kb(footer(refresh="att:pending")), None)
    lines = ["⚠️ <b>Attendance approvals</b>", ""]
    btns = []
    for a in rows[:6]:
        lines.append(f"• {html.escape(a['employee'] or '')} @ {html.escape(a['branch'])} — "
                     f"{a.get('ci_dist')} m · {html.escape(a.get('reason') or 'no reason')}")
        btns.append([(f"✅ #{a['id']}", f"att:appr:{a['id']}"), (f"❌ #{a['id']}", f"att:rej:{a['id']}")])
    return ("\n".join(lines), kb(footer(btns, refresh="att:pending")), None)


async def attendance_submit(tg_id, mode, lat, lng, live, branch=None):
    token, user, _ = await get_ctx(tg_id)
    if not token:
        return await render_home(tg_id)
    path = "/api/attendance/clock-" + ("in" if mode == "in" else "out")
    body = {"lat": lat, "lng": lng, "live": bool(live)}
    if branch:
        body["branch"] = branch
    try:
        status, d = await _req("POST", path, token=token, body=body)
    except Exception:  # noqa: BLE001
        return ("⚠️ Couldn't reach the server. Please try again.",
                kb([[("🔁 Try Again", f"att:{mode}")], [("🏠 Main Menu", "att:menu")]]), None)
    if status >= 400:
        det = (d.get("detail") if isinstance(d, dict) else None) or "Request failed."
        return (f"⚠️ {html.escape(str(det))}",
                kb([[("🔁 Try Again", f"att:{mode}")], [("🏠 Main Menu", "att:menu")]]), None)
    if mode == "out":
        wm = d.get("worked_minutes", 0) or 0
        return (f"✅ <b>Clock-out successful</b>\nBranch: {html.escape(d['branch'])}\n"
                f"Clock-in: {_hm(d.get('clock_in'))}\nClock-out: {_hm(d.get('clock_out'))}\n"
                f"Worked: <b>{wm // 60}h {wm % 60}m</b>\nStatus: Completed",
                kb([[("📅 View Today", "att:today")], [("🏠 Main Menu", "att:menu")]]), None)
    r = d.get("result")
    if r == "choose":
        rows = [[(f"{c['branch']} — {c['distance']} m", f"att:choose:{c['branch']}")] for c in d["candidates"]]
        return ("Choose your branch:", kb(rows + [[("❌ Cancel", "att:menu")]]), None)
    if r == "in":
        return (f"✅ <b>Clock-in successful</b>\nEmployee: {html.escape(user['name'])}\n"
                f"Branch: {html.escape(d['branch'])}\nTime: {_hm(d.get('time'))}\n"
                f"Distance from branch: {d['distance']} meters\n"
                f"Status: {'Late' if d.get('late') else 'On time'}",
                kb([[("🕒 View Today", "att:today")], [("🏠 Main Menu", "att:menu")]]), None)
    if r == "pending":
        await notify_approvers(tg_id, user, d)
        return (f"🕒 <b>Clock-in submitted for approval</b>\nBranch: {html.escape(d['branch'])}\n"
                f"Your distance: {d['distance']} m (allowed {d['radius']} m)\n"
                f"A manager has been notified — you'll be updated once reviewed.",
                kb([[("🏠 Main Menu", "att:menu")]]), None)
    # outside (override disabled)
    return (f"❌ You are outside the allowed attendance area.\nNearest branch: {html.escape(d['branch'])}\n"
            f"Your distance: {d['distance']} m\nAllowed radius: {d['radius']} m",
            kb([[("🔄 Share Location Again", "att:in")], [("🏠 Main Menu", "att:menu")]]), None)


async def notify_approvers(tg_id, user, d):
    if not _APP:
        return
    try:
        _, ap = await _req("GET", f"/api/attendance/approvers?branch={quote(d['branch'])}",
                           headers={"X-Bot-Token": TOKEN})
        for a in (ap or {}).get("approvers", []):
            if str(a["tg_id"]) == str(tg_id):
                continue
            txt = (f"⚠️ <b>Attendance approval request</b>\nEmployee: {html.escape(user['name'])}\n"
                   f"Branch: {html.escape(d['branch'])}\nDistance: {d['distance']} m "
                   f"(allowed {d['radius']} m)\nTap to decide:")
            mk = kb([[("✅ Approve", f"att:appr:{d['id']}"), ("❌ Reject", f"att:rej:{d['id']}")]])
            await _APP.bot.send_message(chat_id=int(a["tg_id"]), text=txt, reply_markup=mk,
                                        parse_mode=ParseMode.HTML)
    except Exception as e:  # noqa: BLE001
        log.warning("notify_approvers: %s", e)


async def att_decide(tg_id, data):
    token, user, _ = await get_ctx(tg_id)
    if not token or not can_approve(user["role"]):
        return ("🔒 Not permitted.", kb(footer()), None)
    aid = data.split(":")[2]
    act = "approve" if data.startswith("att:appr:") else "reject"
    status, d = await _req("POST", f"/api/attendance/{aid}/{act}", token=token)
    if status >= 400:
        return (f"⚠️ Couldn't {act} #{aid}.", kb(footer(refresh="att:pending")), None)
    # notify the employee
    if _APP and d.get("tg_id"):
        try:
            emsg = ("✅ Your attendance was <b>approved</b> — you're clocked in."
                    if act == "approve" else "❌ Your attendance request was <b>rejected</b>.")
            await _APP.bot.send_message(chat_id=int(d["tg_id"]), text=emsg, parse_mode=ParseMode.HTML)
        except Exception:  # noqa: BLE001
            pass
    verb = "✅ Approved" if act == "approve" else "❌ Rejected"
    return (f"{verb} attendance #{aid} ({html.escape(str(d.get('employee') or ''))}).",
            kb(footer(refresh="att:pending")), None)


async def handle_att_location_cb(update, ctx, tg_id, data):
    """Handles the callbacks that need a reply-keyboard location share or the stored pin."""
    token, user, prefs = await get_ctx(tg_id)
    if not token:
        r = await render_home(tg_id)
        await show(update, ctx, r[0], r[1])
        return
    if data.startswith("att:choose:"):
        branch = data.split(":", 2)[2]
        loc = st(tg_id).get("att_last_loc")
        if not loc:
            r = await render_att_menu(tg_id)
            await show(update, ctx, "Location expired — tap Clock In again.", r[1])
            return
        text, markup, _ = await attendance_submit(tg_id, "in", loc[0], loc[1], True, branch=branch)
        await show(update, ctx, text, markup)
        return
    mode = "out" if data in ("att:out",) or data == "att:consent:out" else "in"
    if data.startswith("att:consent:"):
        try:
            await _req("PUT", "/api/telegram/prefs", token=token, body={"att_consent": True})
        except Exception:  # noqa: BLE001
            pass
        s = st(tg_id)
        s["prefs"] = {**(s.get("prefs") or {}), "att_consent": True}
    elif not prefs.get("att_consent"):
        await show(update, ctx, ATT_CONSENT_TEXT,
                   kb([[("✅ Continue", f"att:consent:{mode}")], [("❌ Cancel", "att:menu")]]))
        return
    from telegram import ReplyKeyboardMarkup, KeyboardButton
    s = st(tg_id)
    s["att_await"] = mode
    s["att_ts"] = time.time()
    rk = ReplyKeyboardMarkup([[KeyboardButton("📍 Share Current Location", request_location=True)], ["❌ Cancel"]],
                             one_time_keyboard=True, resize_keyboard=True)
    verb = "clock in" if mode == "in" else "clock out"
    await ctx.bot.send_message(chat_id=update.effective_chat.id,
                               text=f"📍 Tap <b>Share Current Location</b> below to {verb}. "
                                    "Your location is used only for this punch.",
                               reply_markup=rk, parse_mode=ParseMode.HTML)


async def on_location(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    from telegram import ReplyKeyboardRemove
    tg_id = str(update.effective_user.id)
    s = st(tg_id)
    mode = s.get("att_await")
    if not mode:
        return
    if time.time() - s.get("att_ts", 0) > 300:                 # request freshness
        s.pop("att_await", None)
        await update.message.reply_text("That location request expired. Open Attendance and tap Clock In again.",
                                        reply_markup=ReplyKeyboardRemove())
        return
    loc = update.message.location
    live = not bool(update.message.forward_date)               # forwarded pin != live GPS
    s.pop("att_await", None)
    s["att_last_loc"] = (loc.latitude, loc.longitude)
    await update.message.reply_text("📍 Location received — verifying…", reply_markup=ReplyKeyboardRemove())
    text, markup, _ = await attendance_submit(tg_id, mode, loc.latitude, loc.longitude, live)
    await ctx.bot.send_message(chat_id=update.effective_chat.id, text=text, reply_markup=markup,
                               parse_mode=ParseMode.HTML)


# -------------------------------------------------------------------- callback router
async def dispatch(tg_id, data):
    """Return (text, keyboard, document-tuple|None). document-tuple = (bytes, filename, caption)."""
    if data == "home":
        st(tg_id).pop("flow", None)
        st(tg_id)["stack"] = []
        return await render_home(tg_id)
    if data == "att:menu":
        return await render_att_menu(tg_id)
    if data == "att:today":
        return await render_att_today(tg_id)
    if data.startswith("att:me:"):
        return await render_att_me(tg_id, data.split(":")[2])
    if data == "att:branch":
        return await render_att_branch(tg_id)
    if data == "att:pending":
        return await render_att_pending(tg_id)
    if data.startswith("att:appr:") or data.startswith("att:rej:"):
        return await att_decide(tg_id, data)
    # ---- Phase 2: operational flows ----
    if data == "reports_home":
        return ("📊 <b>Reports & Insights</b>\nChoose a view:", reports_home_kb(), None)
    if data in ("op:exp", "op:recv", "op:adj", "op:xfer"):
        return await start_flow(tg_id, data.split(":", 1)[1])
    if data.startswith("opf:"):
        _, name, sku = data.split(":", 2)
        return await start_flow(tg_id, name, sku=sku)
    if data == "op:pur":
        return await render_purchases(tg_id)
    if data == "op:puradd":
        return await start_flow(tg_id, "pur")
    if data == "op:purlist":
        return await render_purchase_list(tg_id)
    if data == "op:scan":
        return await render_scan(tg_id)
    if data.startswith("hist:"):
        return await render_history(tg_id, data.split(":", 1)[1])
    if data.startswith("f:"):
        return await flow_cb(tg_id, data.split(":", 1)[1])
    if data.startswith("fp:"):
        return await flow_pick_product(tg_id, data.split(":", 1)[1])
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
    if data == "nav:lic":
        return await render_licenses(tg_id)
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
    # Attendance location callbacks need a reply-keyboard / stored pin — handle before dispatch.
    if data in ("att:in", "att:out") or data.startswith("att:consent:") or data.startswith("att:choose:"):
        try:
            await handle_att_location_cb(update, ctx, tg_id, data)
        except Exception as e:  # noqa: BLE001
            log.exception("attendance cb error: %s", e)
            await ctx.bot.send_message(chat_id=update.effective_chat.id, text="⚠️ Something went wrong.",
                                       reply_markup=home_kb(), parse_mode=ParseMode.HTML)
        return
    # Back navigation via server-side stack (intra-flow callbacks manage their own steps)
    _intra = data.startswith(("f:", "fp:"))
    if data == "back":
        if s.get("stack"):
            s["stack"].pop()
        data = s["stack"][-1] if s.get("stack") else "home"
    elif data not in ("home", "refresh") and not _intra:
        if not s.get("stack") or s["stack"][-1] != data:
            s.setdefault("stack", []).append(data)
    try:
        text, markup, document = await dispatch(tg_id, data)
    except Exception as e:  # noqa: BLE001
        log.exception("callback error: %s", e)
        text, markup, document = ("⚠️ Something went wrong. Returning home.", home_kb(), None)
    await show(update, ctx, text, markup, document)


async def _send(update, ctx, res):
    if not res:
        return
    text, markup, document = res if len(res) == 3 else (res[0], res[1], None)
    await show(update, ctx, text, markup, document, force_new=True)


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    from telegram import ReplyKeyboardRemove
    tg_id = str(update.effective_user.id)
    s = st(tg_id)
    txt = (update.message.text or "").strip()
    # cancel a pending location share
    if s.get("att_await") and txt.startswith("❌"):
        s.pop("att_await", None)
        await update.message.reply_text("Cancelled.", reply_markup=ReplyKeyboardRemove())
        r = await render_att_menu(tg_id)
        await ctx.bot.send_message(chat_id=update.effective_chat.id, text=r[0], reply_markup=r[1],
                                   parse_mode=ParseMode.HTML)
        return
    # barcode entry mode
    if s.get("await_barcode"):
        await _send(update, ctx, await barcode_lookup(tg_id, txt))
        return
    # active operational flow expecting typed input
    if flow(tg_id) and flow(tg_id).get("_awaiting"):
        res = await flow_text(tg_id, txt)
        if res:
            await _send(update, ctx, res)
        return
    # legacy product search prompt
    if s.get("await_search"):
        s["await_search"] = False
        await _send(update, ctx, await render_search_results(tg_id, txt))


async def on_media(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    s = st(tg_id)
    f = flow(tg_id)
    msg = update.message
    # barcode photo: be honest — we don't decode images
    if s.get("await_barcode") and msg.photo:
        await msg.reply_text("I can't reliably read barcodes from photos yet — please type the barcode number.")
        return
    if not (f and f.get("_awaiting") == "file"):
        return
    meta = None
    if msg.photo:
        p = msg.photo[-1]
        meta = {"kind": "photo", "id": p.file_id, "size": p.file_size or 0}
    elif msg.document:
        doc = msg.document
        # validate type + size (<= 10 MB)
        ok_type = (doc.mime_type or "").lower() in ("application/pdf", "image/jpeg", "image/png")
        if not ok_type:
            await msg.reply_text("Only PDF or image receipts are supported. Try again or tap Skip.")
            return
        if (doc.file_size or 0) > 10 * 1024 * 1024:
            await msg.reply_text("That file is too large (max 10 MB). Try again or tap Skip.")
            return
        meta = {"kind": doc.mime_type or "document", "id": doc.file_id, "size": doc.file_size or 0}
    if not meta:
        await msg.reply_text("Please send a photo or PDF, or tap Skip.")
        return
    await _send(update, ctx, await flow_file(tg_id, meta))


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
    global _APP
    _APP = app
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
    app.add_handler(MessageHandler(filters.LOCATION, on_location))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, on_media))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    log.info("SmokeStack Telegram dashboard starting (long polling); API_BASE=%s", API_BASE)
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()

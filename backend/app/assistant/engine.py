"""Business Assistant — orchestrator, business rules and conversation context.

Ties the intent engine to the tool registry: classify → extract entities → bind
arguments → run the tool (permission-checked) → apply business rules → compose a
short, business-focused answer with numbers, warnings and a next action.

Entirely local and deterministic. No model, no network call, no API key.
"""
from datetime import date

from .. import models, permissions as P
from . import intent as I, tools as T


# ---------------------------------------------------------------- environment
def _known(db, user):
    """Names the extractor can match against.

    Branches deliberately include ones the user CANNOT see: if someone asks for
    a branch outside their scope we want the tool to refuse explicitly, not to
    silently answer about a different branch.
    """
    from .. import security as S
    brs = S.scope_branches(user, db)
    all_branches = S.all_branch_names(db)
    return {
        "branches": all_branches,
        "my_branches": brs,
        "products": [p[0] for p in db.query(models.Product.name).limit(500).all() if p[0]],
        "employees": [e[0] for e in db.query(models.Employee.name)
                      .filter(models.Employee.branch.in_(brs)).limit(500).all() if e[0]],
        "customers": [c[0] for c in db.query(models.Customer.name).limit(500).all() if c[0]],
        "suppliers": [s[0] for s in db.query(models.Supplier.name).limit(500).all() if s[0]],
    }


# -------------------------------------------------------------- business rules
def rules(tool_name, data, user):
    """Deterministic advice derived from the result — no reasoning required."""
    out = []
    if tool_name == "inventory.low_stock":
        c = data.get("counts", {})
        if c.get("out"):
            out.append({"level": "critical",
                        "text": f"{c['out']} product(s) are out of stock.",
                        "action": {"label": "Open Inventory", "view": "inventory"}})
        if c.get("low"):
            names = ", ".join(r["name"] for r in data["low_stock"][:3])
            out.append({"level": "warning",
                        "text": f"{c['low']} product(s) at or below minimum ({names}...).",
                        "action": {"label": "Create Purchase", "view": "purchases"}})
        if not c.get("out") and not c.get("low"):
            out.append({"level": "ok", "text": "Stock levels are healthy."})
    elif tool_name == "sales.summary":
        if data.get("sales") == 0:
            out.append({"level": "critical", "text": "No sales posted for this period.",
                        "action": {"label": "Open Daily Sales", "view": "sales"}})
        profit = data.get("profit")
        if profit is not None and profit < 0:
            out.append({"level": "critical",
                        "text": "Costs exceeded revenue — the period is at a loss.",
                        "action": {"label": "Open Control Center", "view": "control"}})
    elif tool_name == "employees.attendance":
        c = data.get("counts", {})
        if c.get("absent"):
            out.append({"level": "warning", "text": f"{c['absent']} employee(s) absent.",
                        "action": {"label": "Open Attendance", "view": "attendance"}})
        if c.get("missing_out"):
            out.append({"level": "warning",
                        "text": f"{c['missing_out']} missing clock-out(s)."})
        if c.get("late"):
            out.append({"level": "warning", "text": f"{c['late']} late arrival(s)."})
    elif tool_name == "licenses.status":
        if data.get("expired"):
            out.append({"level": "critical",
                        "text": f"{len(data['expired'])} licence(s) EXPIRED.",
                        "action": {"label": "Open Licenses", "view": "licenses"}})
        if data.get("expiring_today") or data.get("within_7_days"):
            n = len(data.get("expiring_today", [])) + len(data.get("within_7_days", []))
            out.append({"level": "warning", "text": f"{n} licence(s) expire within 7 days."})
    elif tool_name == "customers.outstanding":
        if data.get("total"):
            out.append({"level": "warning",
                        "text": f"{data['count']} customer(s) owe {data['total']:,.2f} in total.",
                        "action": {"label": "Open Customers", "view": "settings"}})
    elif tool_name == "sales.by_branch":
        rows = data.get("rows") or []
        if len(rows) > 1 and rows[0]["sales"] and rows[-1]["sales"] is not None:
            best, worst = rows[0], rows[-1]
            if worst["sales"] == 0:
                out.append({"level": "critical",
                            "text": f"{worst['branch']} posted no sales this period."})
            elif best["sales"] >= worst["sales"] * 2:
                out.append({"level": "warning",
                            "text": (f"{best['branch']} is outselling {worst['branch']} "
                                     f"by more than 2×.")})
    elif tool_name == "approvals.pending":
        if data.get("count"):
            out.append({"level": "warning",
                        "text": f"{data['count']} approval(s) waiting on you.",
                        "action": {"label": "Open Purchases", "view": "purchases"}})
    return out


# --------------------------------------------------------------------- answers
def _fmt(v):
    return "—" if v is None else (f"{v:,.2f}" if isinstance(v, float) else str(v))


def summarise(tool_name, data):
    """One short business sentence plus the headline numbers."""
    if tool_name == "sales.summary":
        parts = [f"Sales {_fmt(data.get('sales'))}"]
        if data.get("costs") is not None:
            parts.append(f"costs {_fmt(data['costs'])}")
        if data.get("profit") is not None:
            parts.append(f"profit {_fmt(data['profit'])}")
        return f"{data.get('period','').capitalize()}: " + ", ".join(parts) + "."
    if tool_name == "sales.by_branch":
        rows = data.get("rows") or []
        if not rows:
            return "No branch data for this period."
        return (f"{data['best']} leads with {_fmt(rows[0]['sales'])}; "
                f"{data['weakest']} is lowest at {_fmt(rows[-1]['sales'])}.")
    if tool_name == "inventory.low_stock":
        c = data.get("counts", {})
        return f"{c.get('out',0)} out of stock, {c.get('low',0)} below minimum."
    if tool_name == "inventory.value":
        return f"Inventory is worth {_fmt(data.get('value'))} across {data.get('units',0)} units."
    if tool_name == "inventory.search":
        n = data.get("count", 0)
        if not n:
            return f"No product matched '{data.get('query')}'."
        first = data["results"][0]
        return f"{first['name']} — {first['total_qty']} in stock." if n == 1 else \
               f"{n} products matched '{data.get('query')}'."
    if tool_name == "expenses.summary":
        return f"Expenses {data.get('period')}: {_fmt(data.get('total'))}."
    if tool_name == "expenses.by_category":
        rows = data.get("rows") or []
        top = rows[0]["category"] if rows else "—"
        return f"Total {_fmt(data.get('total'))}; largest category is {top}."
    if tool_name == "payroll.summary":
        return (f"Payroll {data.get('period')}: {_fmt(data.get('total'))} "
                f"across {data.get('employees',0)} employee(s).")
    if tool_name == "employees.attendance":
        c = data.get("counts", {})
        return (f"{c.get('present',0)} present, {c.get('late',0)} late, "
                f"{c.get('absent',0)} absent.")
    if tool_name == "licenses.status":
        c = data.get("counts", {})
        return (f"{len(data.get('expired',[]))} expired, "
                f"{len(data.get('within_30_days',[]))} expiring within 30 days.")
    if tool_name == "customers.outstanding":
        return f"{data.get('count',0)} customer(s) owe {_fmt(data.get('total'))}."
    if tool_name == "products.best_sellers":
        best = (data.get("best") or [{}])[0].get("product")
        return f"Best seller: {best}." if best else "No product sales in this period."
    if tool_name == "sales.by_date":
        return f"{data.get('total') and _fmt(data['total'])} over {data.get('days')} days (avg {_fmt(data.get('average'))})."
    if tool_name == "approvals.pending":
        return f"{data.get('count',0)} approval(s) pending."
    if tool_name in ("employees.search", "customers.search", "suppliers.search"):
        return f"{data.get('count',0)} result(s) for '{data.get('query')}'."
    return "Done."


# ------------------------------------------------------------------- the brain
def ask(db, user, text, context=None):
    """Answer one question. `context` carries the previous turn for follow-ups."""
    context = context or {}
    raw = (text or "").strip()
    if not raw:
        return {"ok": False, "answer": "Ask me about sales, stock, staff or expenses."}

    # 1. navigation wins outright — it is unambiguous and cheap
    view = I.detect_navigation(raw)
    if view:
        return {"ok": True, "kind": "navigate", "view": view,
                "answer": f"Opening {view}.",
                "actions": [{"label": f"Open {view}", "view": view}]}

    # 2. classify
    match = I.classify(raw)
    if not match:
        # follow-up: "now compare with yesterday" reuses the last tool
        if context.get("tool"):
            match = {"intent": context["tool"], "tool": context["tool"], "score": 0,
                     "matched": [], "bind": {}, "needs_query": False,
                     "confident": False, "alternatives": []}
        else:
            return {"ok": False, "kind": "unknown",
                    "answer": ("I did not understand that. Try “today's sales”, "
                               "“low stock”, “who is late”, or “open purchases”."),
                    "suggestions": ["today's sales", "profit this month",
                                    "low stock", "attendance today"]}

    # 3. entities
    known = _known(db, user)
    period_kind = I.extract_period(raw) or context.get("period") or "today"
    branch = I.extract_named(raw, known["branches"]) or context.get("branch")
    kwargs = {"period_kind": period_kind}
    if branch:
        kwargs["branch"] = branch

    tool_name = match["tool"]
    if match.get("needs_query"):
        pool = {"inventory.search": known["products"],
                "employees.search": known["employees"],
                "customers.search": known["customers"],
                "suppliers.search": known["suppliers"]}.get(tool_name, [])
        q = I.extract_named(raw, pool) or I.strip_terms(raw, match["matched"])
        if not q:
            return {"ok": False, "kind": "need_input",
                    "answer": "Which one should I look for?"}
        kwargs = {"q": q}

    # 4. run — the registry enforces permission
    try:
        data = T.run(tool_name, db, user, **kwargs)
    except T.Denied as e:
        return {"ok": False, "kind": "denied", "answer": str(e)}
    except T.ToolError as e:
        return {"ok": False, "kind": "error", "answer": str(e)}

    # 5. compose
    warnings = rules(tool_name, data, user)
    actions = [w["action"] for w in warnings if w.get("action")]
    answer = summarise(tool_name, data)
    if match["bind"].get("want") == "profit" and data.get("profit") is None:
        answer = "You do not have permission to view profit."
        return {"ok": False, "kind": "denied", "answer": answer}

    return {"ok": True, "kind": "answer", "intent": match["intent"], "tool": tool_name,
            "answer": answer, "data": data, "warnings": warnings,
            "actions": actions, "explain": data.get("explain"),
            "confidence": "high" if match.get("confident") else "low",
            "matched_terms": match["matched"],
            "context": {"tool": tool_name, "period": period_kind, "branch": branch},
            "hidden_by_permission": data.get("hidden") or []}

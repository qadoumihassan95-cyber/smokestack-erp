"""Business Assistant — Intent Engine.

Deterministic natural-language understanding. No model, no network, no cost.

How a phrase becomes a tool call
-------------------------------
1. Normalise    lowercase, strip Arabic diacritics, unify alef/ya forms, drop
                punctuation. Arabic and English are handled in one pass, so a
                mixed sentence ("مبيعات today") still works.
2. Score        each intent carries weighted keyword sets in both languages.
                Score = Σ weights of matched terms, with a bonus for matching a
                multi-word phrase and a penalty for a term the intent excludes.
                Highest score wins; ties break on specificity (more terms).
3. Extract      entities are pulled from the same normalised text: period,
                branch, product, employee, customer, supplier, number, date.
4. Bind         the winning intent maps entities onto its tool's arguments.

Adding a language means adding terms — the machinery is language-agnostic.
"""
import re
import unicodedata
from datetime import date, datetime, timedelta

# --------------------------------------------------------------- normalisation
_AR_DIACRITICS = re.compile(r"[ؐ-ًؚ-ٰٟۖ-ۭ]")
_PUNCT = re.compile(r"[^\w؀-ۿ\s:/-]", re.UNICODE)


def normalise(text):
    """Fold a phrase to a comparable form in either language."""
    t = unicodedata.normalize("NFKC", (text or "")).strip().lower()
    t = _AR_DIACRITICS.sub("", t)
    t = (t.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
           .replace("ى", "ي").replace("ة", "ه").replace("ؤ", "و").replace("ئ", "ي"))
    t = t.replace("ـ", "")                      # tatweel
    t = _PUNCT.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip()


def _terms(text):
    return set(normalise(text).split())


# --------------------------------------------------------------------- periods
PERIODS = [
    ("today",      ["today", "todays", "now", "اليوم", "هاليوم", "النهارده"]),
    ("yesterday",  ["yesterday", "yesterdays", "امبارح", "البارحه", "امس"]),
    ("last_week",  ["last week", "الاسبوع الماضي", "الاسبوع اللي فات"]),
    ("week",       ["week", "weekly", "this week", "الاسبوع", "هالاسبوع", "اسبوعي"]),
    ("last_month", ["last month", "الشهر الماضي", "الشهر اللي فات"]),
    ("month",      ["month", "monthly", "this month", "الشهر", "هالشهر", "شهري"]),
    ("year",       ["year", "annual", "yearly", "السنه", "هالسنه", "سنوي"]),
]


def extract_period(text):
    """Longest matching period phrase wins, so 'last week' beats 'week'."""
    n = normalise(text)
    best, best_len = None, 0
    for key, phrases in PERIODS:
        for p in phrases:
            pn = normalise(p)
            if pn and pn in n and len(pn) > best_len:
                best, best_len = key, len(pn)
    return best


DATE_RE = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")


def extract_date(text):
    m = DATE_RE.search(text or "")
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


NUM_RE = re.compile(r"\b(\d+(?:\.\d+)?)\b")


def extract_number(text):
    m = NUM_RE.search(normalise(text))
    return float(m.group(1)) if m else None


# -------------------------------------------------------------------- entities
def extract_named(text, candidates):
    """Match any known name (branch, product, employee...) inside the phrase.

    Longest match wins so 'Store A' is preferred over a stray 'a', and a
    candidate is only accepted on a word boundary.
    """
    n = normalise(text)
    hit, hit_len = None, 0
    for c in candidates or []:
        cn = normalise(c)
        if not cn or len(cn) < 2:
            continue
        if re.search(rf"(^|\s){re.escape(cn)}($|\s)", n) and len(cn) > hit_len:
            hit, hit_len = c, len(cn)
    return hit


# --------------------------------------------------------------------- intents
# weight 3 = decisive term, 2 = strong, 1 = supporting.
INTENTS = [
    {
        "id": "sales.summary", "tool": "sales.summary",
        "terms": {3: ["sales", "revenue", "sold", "turnover",
                      "مبيعات", "بعنا", "المبيعات", "ايراد", "ايرادات"],
                  1: ["how much", "total", "قديش", "كم", "اجمالي", "مجموع"]},
        "not": ["profit", "ربح", "expense", "مصروف", "branch comparison"],
    },
    {
        "id": "profit.summary", "tool": "sales.summary",
        "terms": {3: ["profit", "profits", "net", "ربح", "الربح", "ارباح", "صافي"],
                  1: ["make", "made", "earned", "كسبنا", "ربحنا"]},
        "bind": {"want": "profit"},
    },
    {
        "id": "sales.by_branch", "tool": "sales.by_branch",
        "terms": {3: ["compare branches", "branch comparison", "per branch",
                      "by branch", "which branch", "best branch",
                      "مقارنة الفروع", "اي فرع", "افضل فرع", "حسب الفرع"],
                  2: ["compare", "ranking", "مقارنه", "ترتيب"],
                  1: ["branch", "branches", "فرع", "الفروع"]},
    },
    {
        "id": "sales.trend", "tool": "sales.by_date",
        "terms": {3: ["trend", "daily sales", "last 7 days", "over time", "chart",
                      "اتجاه", "يوميات", "اخر ايام", "مبيعات يوميه"],
                  1: ["history", "series", "تاريخ"]},
    },
    {
        "id": "inventory.low_stock", "tool": "inventory.low_stock",
        "terms": {3: ["low stock", "running out", "run out", "about to run out",
                      "reorder", "below minimum", "out of stock", "restock",
                      "need restocking", "مخزون منخفض", "قرب يخلص", "رح يخلص",
                      "نفد", "نفذ", "تحت الحد", "اعادة طلب", "على وشك النفاد"],
                  2: ["low", "minimum", "منخفض", "الحد الادني"],
                  1: ["stock", "inventory", "مخزون", "المخزن"]},
    },
    {
        "id": "inventory.value", "tool": "inventory.value",
        "terms": {3: ["inventory value", "stock value", "inventory cost",
                      "قيمة المخزون", "كلفة المخزون"],
                  2: ["value", "worth", "قيمه"],
                  1: ["inventory", "stock", "مخزون"]},
    },
    {
        "id": "inventory.search", "tool": "inventory.search",
        "terms": {3: ["find product", "search product", "open product", "look up",
                      "ابحث عن منتج", "افتح منتج", "دور على"],
                  2: ["product", "item", "منتج", "صنف"],
                  1: ["find", "search", "open", "ابحث", "افتح"]},
        "needs_query": True,
    },
    {
        "id": "inventory.movements", "tool": "inventory.movements",
        "terms": {3: ["stock movement", "movements", "stock history",
                      "حركة المخزون", "حركات", "سجل المخزون"],
                  1: ["movement", "حركه"]},
    },
    {
        "id": "expenses.summary", "tool": "expenses.summary",
        "terms": {3: ["expenses", "expense", "spending", "costs paid",
                      "مصاريف", "مصروفات", "المصاريف", "صرفنا"],
                  1: ["spent", "صرف"]},
        "not": ["category", "تصنيف", "بند"],
    },
    {
        "id": "expenses.by_category", "tool": "expenses.by_category",
        "terms": {3: ["expenses by category", "expense breakdown", "where did money go",
                      "المصاريف حسب", "تفصيل المصاريف", "بنود المصاريف"],
                  2: ["category", "breakdown", "تصنيف", "بند", "تفصيل"],
                  1: ["expenses", "مصاريف"]},
    },
    {
        "id": "payroll.summary", "tool": "payroll.summary",
        "terms": {3: ["payroll", "salaries", "salary cost", "wages",
                      "رواتب", "الرواتب", "اجور", "كلفة الرواتب"],
                  1: ["staff cost", "تكلفة الموظفين"]},
    },
    {
        "id": "employees.attendance", "tool": "employees.attendance",
        "terms": {3: ["attendance", "late", "absent", "clocked in", "who is in",
                      "حضور", "غياب", "متاخر", "تاخر", "الدوام", "من حضر"],
                  1: ["present", "موجود"]},
    },
    {
        "id": "employees.search", "tool": "employees.search",
        "terms": {3: ["find employee", "open employee", "employee named",
                      "ابحث عن موظف", "افتح موظف"],
                  2: ["employee", "staff", "موظف", "الموظف"],
                  1: ["find", "search", "ابحث"]},
        "needs_query": True,
        "not": ["attendance", "payroll", "حضور", "رواتب"],
    },
    {
        "id": "customers.search", "tool": "customers.search",
        "terms": {3: ["find customer", "open customer", "customer named",
                      "ابحث عن زبون", "افتح زبون", "عميل"],
                  2: ["customer", "زبون", "الزبون"],
                  1: ["find", "ابحث"]},
        "needs_query": True,
        "not": ["outstanding", "unpaid", "balance", "ذمم", "مستحق"],
    },
    {
        "id": "customers.outstanding", "tool": "customers.outstanding",
        "terms": {3: ["unpaid", "outstanding", "receivable", "owes", "overdue",
                      "ذمم", "مستحقات", "غير مدفوع", "مديونيه"],
                  1: ["balance", "رصيد"]},
    },
    {
        "id": "suppliers.search", "tool": "suppliers.search",
        "terms": {3: ["supplier", "vendor", "مورد", "الموردين", "مزود"],
                  1: ["find", "ابحث"]},
        "needs_query": True,
    },
    {
        "id": "products.best_sellers", "tool": "products.best_sellers",
        "terms": {3: ["best selling", "top products", "worst selling", "slow moving",
                      "افضل المنتجات", "اكثر مبيعا", "اقل مبيعا", "بطيء الحركه"],
                  2: ["best", "top", "worst", "افضل", "اسوا", "اكثر"],
                  1: ["product", "products", "منتج", "منتجات"]},
    },
    {
        "id": "licenses.status", "tool": "licenses.status",
        "terms": {3: ["license", "licence", "permit", "document expiry", "expiring",
                      "رخصه", "رخص", "تراخيص", "تصريح", "انتهاء"],
                  1: ["expire", "expired", "منتهي"]},
    },
    {
        "id": "approvals.pending", "tool": "approvals.pending",
        "terms": {3: ["approvals", "pending approval", "waiting approval", "approve",
                      "موافقات", "بانتظار الموافقه", "اعتماد"],
                  1: ["pending", "معلق"]},
    },
    {
        "id": "audit.logs", "tool": "audit.logs",
        "terms": {3: ["audit log", "audit trail", "who changed", "activity log",
                      "سجل التدقيق", "سجل النشاط", "مين غير"],
                  1: ["audit", "تدقيق"]},
    },
]

# Navigation is a first-class outcome: "go to purchases", "افتح المشتريات"
NAV = {
    "dash":       ["dashboard", "home", "الرئيسيه", "لوحة", "لوحه"],
    "sales":      ["daily sales", "sales page", "المبيعات اليوميه"],
    "expenses":   ["expenses page", "صفحة المصاريف"],
    "purchases":  ["purchases", "purchase orders", "مشتريات", "المشتريات"],
    "inventory":  ["inventory page", "warehouse", "المخزن", "صفحة المخزون"],
    "reports":    ["reports", "تقارير", "التقارير"],
    "tax":        ["sales tax", "tax", "ضريبه", "الضريبه"],
    "payroll":    ["payroll page", "صفحة الرواتب"],
    "attendance": ["attendance page", "صفحة الحضور"],
    "workhours":  ["work hours", "ساعات العمل"],
    "licenses":   ["licenses page", "صفحة الرخص"],
    "telegram":   ["telegram", "تلجرام", "تليجرام"],
    "settings":   ["settings", "الاعدادات", "اعدادات"],
    "control":    ["control center", "financial control", "مركز التحكم"],
    "ai":         ["business assistant", "assistant", "المساعد"],
}

NAV_VERBS = ["go to", "open", "show me the", "navigate", "take me to",
             "افتح", "روح", "اذهب", "انتقل"]


def detect_navigation(text):
    """Return a view id when the phrase is clearly 'take me somewhere'."""
    n = normalise(text)
    has_verb = any(normalise(v) in n for v in NAV_VERBS)
    best, best_len = None, 0
    for view, names in NAV.items():
        for name in names:
            nn = normalise(name)
            if nn and nn in n and len(nn) > best_len:
                best, best_len = view, len(nn)
    if best and (has_verb or best_len >= 8):
        return best
    return None


# ----------------------------------------------------------------- the matcher
def score_intent(spec, text):
    n = normalise(text)
    words = set(n.split())
    score, matched = 0, []
    for weight, terms in spec["terms"].items():
        for term in terms:
            tn = normalise(term)
            if not tn:
                continue
            if " " in tn:
                if tn in n:                       # phrase match earns a bonus
                    score += int(weight) + 1
                    matched.append(term)
            elif tn in words:
                score += int(weight)
                matched.append(term)
    for bad in spec.get("not", []):
        bn = normalise(bad)
        if (bn in n) if " " in bn else (bn in words):
            score -= 4
    return score, matched


def classify(text):
    """Best intent for a phrase, with the evidence that produced it."""
    ranked = []
    for spec in INTENTS:
        s, matched = score_intent(spec, text)
        if s > 0:
            ranked.append((s, len(matched), spec, matched))
    if not ranked:
        return None
    ranked.sort(key=lambda r: (r[0], r[1]), reverse=True)
    s, _, spec, matched = ranked[0]
    runner = ranked[1][0] if len(ranked) > 1 else 0
    return {"intent": spec["id"], "tool": spec["tool"], "score": s,
            "matched": matched, "margin": s - runner,
            "confident": s >= 3 and (s - runner) >= 1,
            "bind": spec.get("bind", {}), "needs_query": spec.get("needs_query", False),
            "alternatives": [{"intent": r[2]["id"], "score": r[0]} for r in ranked[1:4]]}


def strip_terms(text, matched, extra=None):
    """Remove intent words so what remains is the search subject."""
    n = normalise(text)
    for t in sorted((matched or []) + (extra or []), key=len, reverse=True):
        n = n.replace(normalise(t), " ")
    for filler in ("find", "search", "open", "show", "me", "the", "for", "a", "an",
                   "please", "ابحث", "عن", "افتح", "اعرض", "بدي", "شو", "وين"):
        n = re.sub(rf"(^|\s){filler}($|\s)", " ", n)
    return re.sub(r"\s+", " ", n).strip()

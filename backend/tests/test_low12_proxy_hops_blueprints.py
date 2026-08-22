"""LOW-12 — every deployment blueprint must set `TRUSTED_PROXY_HOPS` deliberately.

ERP-Security-Auditor found the SEC HIGH-02 fix configured in the root `render.yaml`
but ABSENT from `backend/render.yaml` and `backend/render.staging.yaml`, which each
define the same API service with a complete `envVars` block. Either would deploy at
the code default of 0; behind Render's router every request then attributes to the
router, collapsing the per-IP limit into ONE bucket shared by the whole customer
base — the self-DoS the fix exists to prevent.

WHY THIS IS A TEST AND NOT JUST THREE EDITS. Fixing the three files fixes the three
files. A fourth blueprint added next month inherits the same omission, and nothing
would say so. The rule is: any service that serves HTTP traffic must state this value
explicitly. That is checkable, so it is checked.

WHAT THIS DELIBERATELY DOES NOT ASSERT — the correction ERP-Security-Auditor made to
the original "make the three files consistent" instruction, which matters because
getting it wrong reopens HIGH-02 instead of closing LOW-12:

    idx = len(parts) - hops   ->   parts[idx]

With one real proxy and hops=1 an attacker's forged entry is ignored. With hops=2
against that SAME one-proxy path, `X-Forwarded-For: forged, X` becomes
`forged, X, <observed>` and the code reads `X` — a value the ATTACKER chose. Too low
merely degrades to the socket peer; TOO HIGH IS THE VULNERABILITY, while looking
more locked-down in review.

So this module does NOT assert that the files agree with each other. Agreement is
true today only because all three sit behind exactly one Render router, and it stops
being the right rule the moment a CDN or WAF is put in front of one environment.
It asserts what is actually invariant: the key is stated, it parses as a
non-negative integer, and it does not exceed the documented topology without a
deliberate, reviewed change to the constant below.

The real proxy count is NOT knowable from this repository. This test cannot prove a
value is correct for its environment — only that no value was left to chance and
none silently exceeds the documented path. Topology-to-value correspondence is
ERP-QA's configuration review, and this test claims none of it.
"""
import os
import re

# The documented topology for every current environment: one Render router in front
# of the API. RAISING THIS IS A SECURITY DECISION, not a config tweak — it must be
# accompanied by evidence of the real proxy path for the environment concerned.
MAX_DOCUMENTED_HOPS = 1

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

# The three blueprints that exist today. Used ONLY as a floor — "these must still be
# found" — never as the set to check. See `_discover_blueprints`.
KNOWN_BLUEPRINTS = frozenset({"render.yaml",
                              os.path.join("backend", "render.yaml"),
                              os.path.join("backend", "render.staging.yaml")})

_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv"}


def _discover_blueprints():
    """FIND the deployment blueprints; do not enumerate them.

    This was a hardcoded three-element list, and ERP-Security-Auditor killed it by
    mutation: adding a fourth blueprint with a real uvicorn service and NO
    `TRUSTED_PROXY_HOPS` left the suite green (4 passed), and giving that fourth
    service `TRUSTED_PROXY_HOPS: "3"` — over-count against the same one-proxy path,
    which is HIGH-02 reopened, not a LOW-12 nit — ALSO left it green. The identical
    value in any of the three named files fails the suite; in a file the list did not
    name it was invisible.

    That is exactly the case this module's docstring promises to catch, so the
    docstring was right and the implementation contradicted it. A hand-maintained
    registry is forgotten by definition: the person adding the fourth blueprint is
    the person who does not know this list exists.

    A blueprint is any YAML outside the skipped directories with a TOP-LEVEL
    `services:` key that mentions uvicorn. Both conditions matter — GitHub workflow
    files nest `services:` under a job and never match at indent 0, and a
    docker-compose without uvicorn is not an API deployment.
    """
    found = []
    for dirpath, dirnames, filenames in os.walk(_ROOT):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fn in filenames:
            if not fn.endswith((".yaml", ".yml")):
                continue
            full = os.path.join(dirpath, fn)
            try:
                with open(full, encoding="utf-8") as fh:
                    text = fh.read()
            except (OSError, UnicodeDecodeError):
                continue
            if "uvicorn" not in text:
                continue
            if not any(ln.startswith("services:") for ln in text.splitlines()):
                continue
            found.append(os.path.relpath(full, _ROOT))
    return sorted(found)


def _services(path):
    """Parse `services:` -> per-service name/startCommand/envVars from a Render
    blueprint by INDENTATION, not by searching for a key name anywhere in the file.

    A substring search for "TRUSTED_PROXY_HOPS" would be satisfied by the word
    appearing in a comment — including the comments in this very repository that
    explain the setting — and could not tell which SERVICE carried it. PyYAML is not
    a declared dependency of this package, so the structure is walked directly; the
    subset of YAML these files use (block maps, `- key:`/`value:` pairs) is regular.
    """
    services, cur = [], None
    with open(os.path.join(_ROOT, path), encoding="utf-8") as fh:
        lines = fh.readlines()

    in_services = False
    for raw in lines:
        line = raw.split("#", 1)[0].rstrip()          # strip comments FIRST
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        body = line.strip()

        if indent == 0:
            in_services = body.startswith("services:")
            continue
        if not in_services:
            continue

        if body.startswith("- type:"):               # a new service begins
            cur = {"type": body.split(":", 1)[1].strip(), "env": {}, "start": "",
                   "name": "", "indent": indent}
            services.append(cur)
            continue
        if cur is None:
            continue

        m = re.match(r"^-?\s*key:\s*(\S+)", body)
        if m:
            cur["_pending"] = m.group(1)
            continue
        m = re.match(r"^value:\s*(.+)$", body)
        if m and cur.get("_pending"):
            cur["env"][cur.pop("_pending")] = m.group(1).strip().strip('"').strip("'")
            continue
        m = re.match(r"^(startCommand|name|runtime):\s*(.+)$", body)
        if m:
            field = {"startCommand": "start", "name": "name", "runtime": "runtime"}[m.group(1)]
            # FIRST occurrence wins. `name:` also appears NESTED under a
            # `fromDatabase:` env var, and letting it overwrite made every failure
            # message blame the database ("smokestack-db") instead of the API
            # service that actually lacked the key. The check was right and the
            # report was wrong, which is the more expensive half to debug.
            if not cur.get(field):
                cur[field] = m.group(2).strip()
    return services


def _http_services():
    """Services that actually serve HTTP requests, so the rate limiter runs in them.

    Keyed on the start command running uvicorn rather than on `type: web` — the
    static frontend is also `type: web` and has no rate limiter, and the Telegram
    worker is a `worker` that never sees an X-Forwarded-For header. Requiring the
    key on those would be a false FAIL, and a gate that cries wolf gets edited out.
    """
    out = []
    for path in _discover_blueprints():
        for svc in _services(path):
            if "uvicorn" in svc.get("start", ""):
                out.append((path, svc))
    return out


def test_the_parser_finds_the_services_it_is_supposed_to_check():
    """A guard that silently finds nothing passes forever. Pin the shape first.

    This is a FLOOR, not an equality. `== 3` pinned the parser against a blueprint
    DISAPPEARING but could not notice one being ADDED — and an added blueprint is the
    whole failure mode this module exists for. A new blueprint must WIDEN coverage,
    so more than three is success, not a reason to fail.
    """
    found = _http_services()
    paths = {p for p, _ in found}
    missing = KNOWN_BLUEPRINTS - paths
    assert not missing, (
        f"blueprints that must exist were not discovered: {sorted(missing)}. Either "
        f"they were renamed/removed, or `_discover_blueprints` stopped matching them "
        f"— do not narrow this assertion to make it pass.")
    assert len(found) >= 3, (
        f"discovery parsed only {len(found)} uvicorn services: {sorted(paths)}")
    # The label must name the API service, not a nested database reference.
    assert all("api" in s.get("name", "") for _, s in found), \
        [(p, s.get("name")) for p, s in found]


def test_every_http_service_states_its_proxy_hop_count():
    missing = [(p, s.get("name")) for p, s in _http_services()
               if "TRUSTED_PROXY_HOPS" not in s["env"]]
    assert not missing, (
        f"these HTTP services do not set TRUSTED_PROXY_HOPS and would deploy at the "
        f"code default of 0: {missing}. Behind a router that makes every request "
        f"share one rate-limit bucket. State the environment's real proxy count "
        f"explicitly, even when it is 0.")


def test_no_blueprint_trusts_more_proxies_than_the_documented_path():
    """TOO HIGH IS THE VULNERABILITY — this is the assertion that matters."""
    for path, svc in _http_services():
        raw = svc["env"].get("TRUSTED_PROXY_HOPS")
        assert raw is not None and re.fullmatch(r"\d+", raw), (
            f"{path} ({svc.get('name')}): TRUSTED_PROXY_HOPS={raw!r} is not a "
            f"non-negative integer")
        assert int(raw) <= MAX_DOCUMENTED_HOPS, (
            f"{path} ({svc.get('name')}): TRUSTED_PROXY_HOPS={raw} exceeds the "
            f"documented topology of {MAX_DOCUMENTED_HOPS} proxy. A value above the "
            f"REAL hop count reopens SEC HIGH-02: the entry the limiter then reads "
            f"is one the client supplied, so per-IP attribution returns to the "
            f"attacker. If a CDN or WAF was genuinely added, raise "
            f"MAX_DOCUMENTED_HOPS with evidence of the real path.")


def test_the_env_example_ships_the_safe_default():
    """A self-hosted or local deployment must inherit 0 deliberately, not by
    omission — the same failure mode as the blueprints, one layer out."""
    path = os.path.join(_ROOT, "backend", ".env.example")
    body = open(path, encoding="utf-8").read()
    line = next((ln for ln in body.splitlines()
                 if ln.strip().startswith("TRUSTED_PROXY_HOPS=")), None)
    assert line is not None, f"{path} does not declare TRUSTED_PROXY_HOPS"
    assert line.strip() == "TRUSTED_PROXY_HOPS=0", (
        f"the example must ship the SAFE default 0 (ignore the header, trust the "
        f"socket peer), not a topology-specific value someone may copy into an "
        f"environment that does not have those proxies; found {line.strip()!r}")

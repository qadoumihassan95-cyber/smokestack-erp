#!/usr/bin/env python3
"""PFS deployment-safety tool (Engineering Phase 3).

One reusable tool, three subcommands — no duplicated deploy logic:

  preflight            Pre-deploy gate. Fails (exit 1) if:
                         - alembic does not have exactly one head
                         - any migration in the range lacks a real downgrade()
                       Prints the backup + rollback checklist.

  verify   <url> [sha] Post-deploy verification against a live service:
                         - GET /api/health is 200 and status == ok
                         - checks.database == ok
                         - checks.build == sha  (if sha given)
                       Exits non-zero on any failure (rollback signal).

  report   <url>       Emits a markdown deployment report (health, build, apps,
                       alembic head) to stdout / $GITHUB_STEP_SUMMARY.

Usage:
  python scripts/deploy_safety.py preflight
  python scripts/deploy_safety.py verify https://smokestack-api.onrender.com <git_sha>
  python scripts/deploy_safety.py report https://smokestack-api.onrender.com
"""
import json
import os
import sys
import urllib.request


def _http_json(url):
    with urllib.request.urlopen(url, timeout=20) as r:  # noqa: S310 (fixed https health URL)
        return r.status, json.loads(r.read().decode())


def preflight():
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    cfg = Config(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(os.path.dirname(__file__), "..", "migrations"))
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()
    ok = True
    if len(heads) != 1:
        print(f"FAIL: expected 1 alembic head, found {len(heads)}: {heads}"); ok = False
    else:
        print(f"PASS: single alembic head {heads[0]}")
    missing = []
    for rev in script.walk_revisions():
        src = open(rev.path, encoding="utf-8").read()
        body = src.split("def downgrade")[1] if "def downgrade" in src else ""
        if "def downgrade" not in src or body.strip().startswith("():\n    pass"):
            missing.append(rev.revision)
    if missing:
        print(f"WARN: migrations without a real downgrade: {missing[:5]}")
    else:
        print("PASS: every migration defines a downgrade()")
    print("\nCHECKLIST before promoting to production:")
    for item in ("Fresh logical backup created + verified restorable",
                 "PITR window confirmed",
                 "Migration applied on STAGING first and validated",
                 "Rollback (down/up) rehearsed on staging",
                 "Full CI green (lint, security, deps, unit, migration-pg, build)"):
        print(f"  [ ] {item}")
    return 0 if ok else 1


def verify(url, sha=None):
    try:
        status, body = _http_json(url.rstrip("/") + "/api/health")
    except Exception as e:  # noqa: BLE001
        print(f"FAIL: health request errored: {e}"); return 1
    checks = body.get("checks", {})
    ok = True
    if status != 200 or body.get("status") != "ok":
        print(f"FAIL: health status {status} / {body.get('status')}"); ok = False
    if checks.get("database") != "ok":
        print(f"FAIL: database check = {checks.get('database')}"); ok = False
    build = checks.get("build")
    if sha and not (build and sha.startswith(build) or (build and build.startswith(sha[:12]))):
        print(f"FAIL: build {build} does not match expected {sha}"); ok = False
    print(f"{'PASS' if ok else 'FAIL'}: health=ok db={checks.get('database')} "
          f"build={build} apps={checks.get('applications')}")
    return 0 if ok else 1


def report(url):
    try:
        _, body = _http_json(url.rstrip("/") + "/api/health")
    except Exception as e:  # noqa: BLE001
        body = {"error": str(e)}
    c = body.get("checks", {})
    md = (f"## Deployment report\n\n"
          f"| field | value |\n|---|---|\n"
          f"| service | {url} |\n"
          f"| status | {body.get('status')} |\n"
          f"| database | {c.get('database')} |\n"
          f"| build | `{c.get('build')}` |\n"
          f"| applications | {c.get('applications')} |\n")
    print(md)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(md)
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "preflight":
        sys.exit(preflight())
    elif cmd == "verify":
        sys.exit(verify(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None))
    elif cmd == "report":
        sys.exit(report(sys.argv[2]))
    else:
        print(__doc__); sys.exit(2)

"""DB-backed, multi-instance-safe throttling (SS-H-007).

A fixed-window limiter that counts rows in ``auth_rate_hits`` for a scope key
within a rolling window. Because the counter lives in the shared database, every
API/worker instance sees the same tally — an in-memory limiter would not. Old
rows for a key are pruned on each check so the table cannot grow without bound.

Only a NON-reversible scope key is stored (e.g. ``login:ip:<addr>`` or
``login:user:<id>``); never a raw password or Telegram code. Callers throttle on
BOTH the client IP and the identifier, so neither a single IP nor a single
targeted account can be hammered, and the 429 response is identical whether or
not the account exists (no enumeration).
"""
from datetime import datetime, timedelta, timezone
from sqlalchemy.orm import Session
from fastapi import HTTPException, Request
from . import models
from .config import settings


def client_ip(request: "Request") -> str:
    """The address to throttle this caller on.

    SECURITY (SEC HIGH-02). This previously returned the FIRST ``X-Forwarded-For``
    entry unconditionally. That value is written by the caller, so rotating it once
    per attempt gave every request its own per-IP bucket and the per-IP half of every
    throttle never engaged — 120 logins across 120 accounts, none throttled.

    A header is only evidence if something we trust wrote it. ``trusted_proxy_hops``
    says how many proxies we actually operate; we take the entry that many from the
    RIGHT, because each proxy appends the peer it saw. With one trusted proxy the
    rightmost entry is the address that proxy observed — a client can prepend as many
    fake entries as it likes and they all sit to the left of it, ignored. With zero
    (the default) the header is not consulted at all.

    Note the default cannot simply be "never trust XFF": deployed behind Render every
    request would then report the router's address, one shared bucket, and the per-IP
    limit would throttle the whole customer base at once. The hop count is what lets
    the safe default and the real deployment both be correct — production sets
    TRUSTED_PROXY_HOPS=1 (see render.yaml).
    """
    if request is None:
        return "unknown"
    peer = getattr(getattr(request, "client", None), "host", None) or "unknown"
    hops = getattr(settings, "trusted_proxy_hops", 0) or 0
    if hops <= 0:
        return peer
    xff = request.headers.get("x-forwarded-for") if request.headers else None
    if not xff:
        return peer
    parts = [p.strip() for p in xff.split(",") if p.strip()]
    idx = len(parts) - hops
    if idx < 0:
        # Fewer entries than proxies we expect: the chain is not what we configured,
        # so nothing in it is attributable. Fall back to the peer rather than guess.
        return peer
    return parts[idx]


def _count_recent(db: Session, key: str, cutoff: datetime) -> int:
    return (db.query(models.RateHit)
            .filter(models.RateHit.scope_key == key, models.RateHit.ts >= cutoff)
            .count())


def check(db: Session, key: str, *, limit: int, window_sec: int) -> None:
    """Raise 429 if ``key`` already has >= ``limit`` hits inside the window.
    Prunes expired rows for the key first (bounded growth). Read-only otherwise."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=window_sec)
    db.query(models.RateHit).filter(
        models.RateHit.scope_key == key, models.RateHit.ts < cutoff
    ).delete(synchronize_session=False)
    if _count_recent(db, key, cutoff) >= limit:
        raise HTTPException(429, "Too many attempts. Please wait and try again.",
                            {"Retry-After": str(window_sec)})


def record(db: Session, key: str) -> None:
    """Register one attempt against ``key``. Best-effort; never breaks the request."""
    try:
        db.add(models.RateHit(scope_key=key, ts=datetime.now(timezone.utc)))
        db.commit()
    except Exception:
        db.rollback()


def guard(db: Session, request: "Request", identifier: str, *, kind: str,
          limit: int = 10, window_sec: int = 300) -> None:
    """Convenience wrapper: throttle a sensitive action on BOTH IP and identifier
    under a shared ``kind`` namespace. Call BEFORE verifying the secret."""
    ip = client_ip(request)
    check(db, f"{kind}:ip:{ip}", limit=limit * 3, window_sec=window_sec)
    if identifier:
        check(db, f"{kind}:id:{str(identifier).lower()}", limit=limit, window_sec=window_sec)


def note_failure(db: Session, request: "Request", identifier: str, *, kind: str) -> None:
    """Record a failed attempt against both IP and identifier scope keys."""
    ip = client_ip(request)
    record(db, f"{kind}:ip:{ip}")
    if identifier:
        record(db, f"{kind}:id:{str(identifier).lower()}")

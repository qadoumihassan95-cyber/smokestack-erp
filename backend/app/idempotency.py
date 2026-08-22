"""Shared idempotency framework (Engineering Phase 5).

A single ASGI middleware makes EVERY mutating endpoint idempotent when the client
sends an ``Idempotency-Key`` header — no per-endpoint code. A retried request with
the same key replays the first response instead of executing twice (prevents double
purchases, double transfers, double stock movements).

SECURITY (SEC CRITICAL-01). This middleware runs OUTERMOST, so anything it answers
is answered before routing, before ``get_current_user`` and before every
``require(...)`` dependency. That makes the replay lookup itself an authentication
and authorization boundary, and the original implementation was not one:

  * the scope was ``sha256(Authorization)`` with the literal ``"anon"`` for any
    request that sent no header, so every unauthenticated caller on the internet
    shared ONE key namespace — planting a key during a victim's login and reusing
    it returned the victim's bearer token to an anonymous caller, cross-tenant;
  * ``method`` and ``path`` were persisted but never compared, so one key made an
    unrelated later mutation return the earlier operation's success body while
    never executing (a financial write reported as done that did not happen).

The rules that make replay safe, and why each one is load-bearing:

  1. **A replay requires a LIVE authenticated principal.** The middleware resolves
     the caller itself — signature, realm, the user still exists and is active, and
     the token's ``tv`` still matches ``token_version``. Without one it neither
     replays nor persists; the request simply proceeds and the route's own auth
     chain produces the correct 401/403. The ``tv`` check matters on its own: a
     token revoked by logout / password reset / role change must not be able to
     replay a response cached before its revocation.
  2. **The namespace is the resolved identity, not the header bytes.** Scope is
     ``(realm, company_id, user_id, token_version)``, so no anonymous namespace
     exists to share and two tenants can never collide.
  3. **A key is bound to the exact operation it was issued for.** A hit whose
     stored ``method``/``path`` differ from the current request is refused 409
     rather than answered — reuse across operations is a client bug, and silently
     returning the wrong operation's body is the dangerous way to report it.
  4. **Entries expire.** Records older than ``IDEMPOTENCY_TTL_HOURS`` are pruned
     and never replayed, so the table is bounded and a cached response cannot be
     replayed indefinitely after the caller's access has otherwise changed.

NOT DONE HERE, and it is a real gap rather than an accepted one: the stored record
binds method and path but NOT the request BODY, so the same key replayed against the
same endpoint with a *different* payload still returns the first response. That turns
"exactly once" into "first write wins, silently". Closing it needs a
``request_sha256`` column — a schema change, on a branch whose remit is a set of
named security blockers with a frozen parent. It should land as its own change with
its own migration evidence rather than riding along here. Until then the exposure is
bounded to one endpoint per key (method+path must match) and to callers who reuse a
key across different payloads, which is a client bug.

(An earlier draft of this note justified the deferral by claiming the migration chain
had six un-merged heads and that migrations were not exercised by the test suite.
Both are false: ``alembic heads`` reports ONE head, the chain is linear, and at least
seven test modules run ``command.upgrade(cfg, "head")``. The deferral stands on the
scope argument above; the false justification is recorded here rather than deleted
because it was load-bearing in a plan document and someone may have relied on it.)

This complements the existing scheduler idempotency (`report_deliveries.idem_key`)
and generalises the pattern platform-wide.
"""
import hashlib
from datetime import datetime, timedelta, timezone

from jose import jwt, JWTError
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

from .config import settings

_METHODS = {"POST", "PUT", "PATCH"}


def _scope(realm, company_id, user_id, token_version) -> str:
    """The key namespace for one resolved identity.

    Derived from WHO the caller is, never from the header bytes. Including
    ``token_version`` means a revoked token lands in a different namespace from the
    session that replaced it, so it cannot read that session's cached responses.
    """
    raw = f"{realm}|{company_id}|{user_id}|{token_version}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def resolve_scope(request, db):
    """Return the idempotency scope for a LIVE authenticated ERP principal, else None.

    None means "this request has no principal the cache may act for" — the caller is
    unauthenticated, forged, expired, deactivated, revoked, a Control Center (PFS)
    token, still on a temporary password, or acting for a suspended / archived /
    read-only company. Every such request bypasses the cache entirely in BOTH
    directions: nothing is replayed to it and nothing it does is stored. It is then
    handled by the real auth chain, which is the only thing entitled to authenticate.

    CRITICALLY, this does not re-implement any of that. It calls
    ``security.get_current_user`` — THE auth chokepoint — with explicit arguments.
    A second, hand-rolled "is this caller ok?" here would inevitably be the weaker
    of the two: the first draft of this function checked signature, realm, active
    and token_version and thereby silently omitted the first-login lockout and the
    whole policy pipeline (suspended / archived / read-only company), so it would
    have replayed for principals the endpoint itself refuses. A guard placed in
    front of an authorization chain must not be a paraphrase of it.

    (``get_current_user`` is a FastAPI dependency, and calling one of those directly
    is exactly the defect class as SEC HIGH-07. The difference is that there every
    argument came from a ``Depends`` default that a direct call SKIPS, whereas here
    all three parameters are passed explicitly and none is a guard — the guarding
    is the function's own body. It is a dependency by usage, not by construction.)
    """
    from . import security

    auth = (request.headers.get("authorization") or "").strip()
    scheme, _, token = auth.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    try:
        user = security.get_current_user(request=request, token=token.strip(), db=db)
    except Exception:
        # Any refusal at all — 401 forged/expired/revoked, 403 first-login lockout or
        # company policy. Fail closed: no principal, so no replay and no persistence.
        return None
    if user is None:
        return None

    realm = "erp"
    cid = getattr(user, "_company_id", None) or getattr(user, "company_id", None) or 1
    tv = int(getattr(user, "token_version", 0) or 0)
    # An impersonation token acts AS this user in this company but is a different
    # principal with a different lifetime; it must not share a key namespace with the
    # user's own session, or a Super Admin's replay could answer the real user (or
    # the reverse). Same reasoning as token_version: distinguishable principal,
    # distinguishable namespace.
    imp = 1 if getattr(user, "_impersonation", None) else 0
    return _scope(realm, cid, f"{user.id}|imp{imp}", tv)


def _prune(db, models) -> None:
    """Drop every entry past its TTL. Bounded growth, and an expired record can
    never be replayed even if a lookup races this delete."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=settings.idempotency_ttl_hours)
    try:
        (db.query(models.IdempotencyKey)
           .filter(models.IdempotencyKey.created_at.isnot(None),
                   models.IdempotencyKey.created_at < cutoff)
           .delete(synchronize_session=False))
        db.commit()
    except Exception:
        db.rollback()          # pruning is maintenance; never fail a request over it


def _is_expired(prior) -> bool:
    created = getattr(prior, "created_at", None)
    if created is None:
        return False
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return created < datetime.now(timezone.utc) - timedelta(hours=settings.idempotency_ttl_hours)


class IdempotencyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        key = request.headers.get("idempotency-key")
        if not key or request.method not in _METHODS:
            return await call_next(request)

        # Import here to avoid import cycles at app-composition time.
        from .database import SessionLocal
        from . import models, tenancy

        # ---- who is calling? no live principal => the cache does not participate ----
        db = SessionLocal()
        try:
            tenancy.use_system_context(db)
            scope = resolve_scope(request, db)
            if scope is None:
                return await call_next(request)
            # resolve_scope ran the real auth chokepoint, which tags the session with
            # the caller's company. This session is only used for cache maintenance,
            # so put it back to an explicit system context rather than leaving it
            # half-tagged.
            tenancy.use_system_context(db)

            _prune(db, models)

            prior = (db.query(models.IdempotencyKey)
                     .filter(models.IdempotencyKey.scope == scope,
                             models.IdempotencyKey.key == key)
                     .first())
            if prior is not None and not _is_expired(prior):
                # A key belongs to ONE operation. Answering a different one with this
                # body would report success for work that never ran.
                if (prior.method or "") != request.method or (prior.path or "") != request.url.path:
                    return JSONResponse(
                        {"detail": "This Idempotency-Key was already used for a different "
                                   "request. Use a new key for a new operation."},
                        status_code=409)
                return Response(
                    content=(prior.response_body or "").encode("utf-8"),
                    status_code=prior.status_code or 200,
                    media_type=prior.content_type or "application/json",
                    headers={"Idempotency-Replayed": "true"},
                )
        finally:
            db.close()

        # ---- first time: run handler, buffer + persist the response ----
        response = await call_next(request)
        body = b""
        async for chunk in response.body_iterator:
            body += chunk

        # Persist SUCCESSES ONLY. This used to store anything under 500, which meant a
        # 401/403/422 was cached and then replayed for the whole TTL — a transient
        # failure becoming sticky, and under a stable scope the caller could not clear
        # it by retrying. A failed request has no effect to be idempotent about.
        if 200 <= response.status_code < 300:
            db = SessionLocal()
            try:
                tenancy.use_system_context(db)
                db.add(models.IdempotencyKey(
                    scope=scope, key=key, method=request.method,
                    path=request.url.path, status_code=response.status_code,
                    response_body=body.decode("utf-8", "replace"),
                    content_type=response.headers.get("content-type", "application/json"),
                ))
                db.commit()
            except Exception:
                db.rollback()   # concurrent duplicate raced us; the effect is still once
            finally:
                db.close()

        return Response(content=body, status_code=response.status_code,
                        media_type=response.headers.get("content-type"),
                        headers=dict(response.headers))

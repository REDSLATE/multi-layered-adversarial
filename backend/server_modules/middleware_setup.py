"""Middleware setup — CORS + Public-API rate limit / traffic logger.

Extracted from server.py on 2026-06-18. Behavior 1:1.

Starlette runs `middleware("http")` in REVERSE order — last added is
outermost. We want:
    outermost: traffic logger  → sees the final response (incl. 429s)
    inner:     rate limiter    → can short-circuit with 429
So we add the rate limiter FIRST (inner) and the traffic logger LAST
(outer). Don't reorder these without re-reading this comment.

CORS — explicit origin list from env (2026-05-26).
Reads `CORS_ORIGINS` (the env var the operator already has set on
prod). Comma-separated list. When set: exact-match origins +
allow_credentials=True so cookie-based auth works. When unset:
falls back to wildcard so preview / local dev keep working.

Only enable credentialed CORS when origins are pinned — Starlette
forbids `allow_credentials=True` alongside wildcard origins.
"""
from __future__ import annotations

import logging
import os
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.cors import CORSMiddleware

from shared.public_api.rate_limit import rate_limit_middleware
from shared.public_api.traffic import public_traffic_middleware

# Pymongo timeout family — the class hierarchy is stable across
# pymongo 4.x; if it moves in a future release the import fails at
# boot instead of silently degrading (better than a silent regression
# into the exact behavior this handler exists to prevent).
try:
    from pymongo.errors import (
        NetworkTimeout,
        ExecutionTimeout,
        ServerSelectionTimeoutError,
        WTimeoutError,
    )
    _PYMONGO_TIMEOUT_ERRORS: tuple[type[BaseException], ...] = (
        NetworkTimeout,
        ExecutionTimeout,
        ServerSelectionTimeoutError,
        WTimeoutError,
    )
except Exception:  # noqa: BLE001
    _PYMONGO_TIMEOUT_ERRORS = ()


_log = logging.getLogger("risedual.errors")


def _build_atlas_timeout_response(request: Request, exc: BaseException) -> JSONResponse:
    """Format the soft-degraded response for a pymongo timeout.

    Split out from `setup_middleware` so unit tests can call it with a
    stub request and assert the response shape without spinning up
    the whole app.
    """
    request_id = uuid.uuid4().hex[:12]
    path = str(request.url.path)
    method = (request.method or "GET").upper()
    exc_type = type(exc).__name__
    msg = str(exc)
    if len(msg) > 240:
        msg = msg[:240] + "…"

    # Log at WARNING (not ERROR) — this is a known infra condition
    # the operator can act on (increase tier, add index, cache) and
    # the handler is doing the recovery. ERROR would spam alerts.
    _log.warning(
        "[%s] atlas timeout %s %s → %s: %s (soft-degraded)",
        request_id, method, path, exc_type, msg,
    )

    body = {
        "ok": False,
        "degraded": True,
        "atlas_timeout": True,
        "atlas_error": f"{exc_type}: {msg}",
        "warning": "atlas_read_temporarily_slow",
        "request_id": request_id,
        "path": path,
        "method": method,
        # Every widget that iterates `items` renders empty state
        # rather than a red banner. `payload:{}` is a belt-and-braces
        # default for endpoints returning a nested object; harmless
        # for endpoints that ignore it.
        "items": [],
        "count": 0,
        "payload": {},
    }
    # GET/HEAD → 200 so the widget's happy path resolves. Writes get
    # a 503 so the caller knows the write did NOT land.
    status_code = 200 if method in {"GET", "HEAD"} else 503
    return JSONResponse(status_code=status_code, content=body)


def setup_middleware(app: FastAPI) -> None:
    """Attach all HTTP-level middleware to the FastAPI app."""
    # Order-sensitive: rate-limit (inner) added before traffic (outer).
    app.middleware("http")(rate_limit_middleware)
    app.middleware("http")(public_traffic_middleware)

    cors_env = os.environ.get("CORS_ORIGINS", "").strip()
    cors_origins = (
        [o.strip() for o in cors_env.split(",") if o.strip()]
        if cors_env and cors_env != "*"
        else ["*"]
    )
    cors_allow_credentials = cors_origins != ["*"]

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=cors_allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ══════════════════════════════════════════════════════════════
    #  Atlas timeout soft-degrade handler (2026-07-11 prod hotfix)
    # ══════════════════════════════════════════════════════════════
    #
    # Symptom this handler exists to eliminate: every operator dashboard
    # tile would render `NetworkTimeout: customer-apps-shard-XX.kndgvm.mongodb.net:27017`
    # or `ExecutionTimeout: PlanExecutor error during aggregation ...
    # MaxTimeMS...` as a red banner. Root cause: a growing tape (opinions
    # / ohlcv / intents) + shared-tier Atlas + unbounded reads. Even after
    # bounding the top-N hot paths with `.max_time_ms()`, ~295 other read
    # sites remain unbounded — auditing each one individually is a losing
    # game.
    #
    # Doctrine (aligned with the "one door" simplicity mandate): every
    # pymongo timeout on a READ request is a *transient infrastructure*
    # condition, not an application bug. The operator UI should degrade
    # to an empty state (with an amber `degraded=true` marker), never a
    # red banner. Writes still surface the failure — a POST that timed
    # out must reach the caller so retry / user feedback logic fires.
    #
    # Contract:
    #   * GET / HEAD  → HTTP 200 with `{ok:false, degraded:true,
    #                                    atlas_timeout:true, items:[],
    #                                    request_id, warning}`.
    #     A 200 is deliberate: it lets every existing widget's happy
    #     path resolve, and widgets that consume `items` or `payload`
    #     render empty state instead of showing an error message.
    #   * anything else (POST/PUT/PATCH/DELETE) → HTTP 503 with the
    #     same body shape but `ok=false`, so the caller can retry.
    #     A write path stays honest — silencing a failed write would
    #     be exactly the dishonesty the 3-clock work eliminated.
    #
    # This handler MUST be registered before the generic Exception
    # handler below — FastAPI resolves handlers by the most specific
    # matching class.
    if _PYMONGO_TIMEOUT_ERRORS:
        @app.exception_handler(_PYMONGO_TIMEOUT_ERRORS[0])
        async def _atlas_timeout_handler(request: Request, exc: BaseException):
            return _build_atlas_timeout_response(request, exc)

        # Register the same handler for the remaining timeout classes.
        for _err_cls in _PYMONGO_TIMEOUT_ERRORS[1:]:
            app.add_exception_handler(_err_cls, _atlas_timeout_handler)

    # 2026-06-18: global 500 handler — Starlette's default returns
    # plain-text "Internal Server Error" with no JSON body, which
    # surfaces in the UI as the unactionable "HTTP 500" red bar the
    # operator saw on the Production Intents page. This handler
    # ALWAYS returns a JSON body with the exception type, a short
    # message snippet, the request method/path, and a unique
    # request_id the operator can grep for in backend logs.
    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(request: Request, exc: Exception):
        request_id = uuid.uuid4().hex[:12]
        path = str(request.url.path)
        method = request.method
        exc_type = type(exc).__name__
        msg = str(exc) or "(no message)"
        if len(msg) > 240:
            msg = msg[:240] + "…"
        # Log the full traceback so the operator can match by
        # request_id in the backend log.
        _log.exception(
            "[%s] %s %s → %s: %s",
            request_id, method, path, exc_type, msg,
        )
        return JSONResponse(
            status_code=500,
            content={
                "detail": f"{exc_type}: {msg}",
                "request_id": request_id,
                "path": path,
                "method": method,
            },
        )

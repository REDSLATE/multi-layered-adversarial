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


_log = logging.getLogger("risedual.errors")


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

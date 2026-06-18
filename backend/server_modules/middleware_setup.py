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

import os

from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware

from shared.public_api.rate_limit import rate_limit_middleware
from shared.public_api.traffic import public_traffic_middleware


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

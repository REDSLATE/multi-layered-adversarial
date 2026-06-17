"""Operator + brain-side endpoint for the derived market-data feature
service (`shared/market_data/feature_service.py`).

Routes:
  GET /api/admin/market-data/snapshot/{symbol}
      - Returns `{relative_volume, has_news, ...}` for one symbol.
      - Dual auth: either operator JWT (admin) OR
        `X-Brain-Id` + `X-Runtime-Token` (brain sidecar). The brain
        path mirrors the market-data-key proxy contract so the same
        runtime token works on both surfaces.
      - Query params: `tf` (5m default), `source` (finnhub_equity
        default), `include_news` (true default).

  GET /api/admin/market-data/snapshot
      - Operator-only batch endpoint. POST-like with `symbols` query
        param (comma-separated, max 50). Returns one row per symbol.

Doctrine:
  - READ-ONLY. Never writes. Never serves broker keys.
  - The brain-auth path never returns the Finnhub API key or any
    raw header value — only the derived facts.
  - Failure on one symbol in the batch never tanks the rest.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query

from auth import get_current_user
from shared.market_data.feature_service import (
    DEFAULT_SOURCE,
    DEFAULT_TIMEFRAME,
    build_market_snapshot,
    reset_news_cache,
)


logger = logging.getLogger("risedual.market_data_snapshot")
router = APIRouter(prefix="/admin/market-data", tags=["market-data-snapshot"])


# Brain-auth mirrors the market-data-key proxy (`routes/market_data_keys.py`).
KNOWN_BRAINS: tuple[str, ...] = ("camino", "barracuda", "hellcat", "gto")
MAX_BATCH_SYMBOLS: int = 50


def _expected_token_for(brain: str) -> str:
    return os.environ.get(f"{brain.upper()}_INGEST_TOKEN", "")


async def _dual_auth(
    x_brain_id: Optional[str],
    x_runtime_token: Optional[str],
    operator_user: Optional[dict],
) -> str:
    """Accept either an operator JWT (already resolved into `operator_user`
    by `Depends(get_current_user)` upstream) OR a (brain, token) pair.

    Returns the auth principal for audit trail purposes:
        "operator:<email>"  or  "brain:<id>"
    """
    if operator_user and operator_user.get("email"):
        return f"operator:{operator_user['email']}"

    brain = (x_brain_id or "").lower().strip()
    if not brain:
        raise HTTPException(status_code=401, detail="auth required")
    if brain not in KNOWN_BRAINS:
        raise HTTPException(status_code=404, detail=f"unknown brain {brain!r}")
    expected = _expected_token_for(brain)
    if not expected:
        raise HTTPException(
            status_code=503,
            detail=f"snapshot endpoint not configured for {brain}",
        )
    if (x_runtime_token or "") != expected:
        raise HTTPException(status_code=401, detail="invalid token")
    return f"brain:{brain}"


async def _maybe_user(authorization: Optional[str] = Header(default=None)) -> Optional[dict]:
    """Try resolve the operator JWT but DON'T 401 if it's missing or
    invalid — the brain-token path is a valid alternative. The dual-
    auth helper decides whether to reject. Mirrors the validation
    logic in `auth.get_current_user` (jwt.decode + access-type check)
    but never raises — failures fall through to brain-token auth."""
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    try:
        import jwt
        from auth import _secret, JWT_ALGORITHM
        from db import db
        token = authorization.split(" ", 1)[1].strip()
        payload = jwt.decode(token, _secret(), algorithms=[JWT_ALGORITHM])
        if payload.get("type") != "access":
            return None
        user = await db.users.find_one(
            {"id": payload["sub"]}, {"_id": 0, "password_hash": 0},
        )
        return user
    except Exception:  # noqa: BLE001
        # Bad/expired/missing JWT → brain-token path will handle it.
        return None


@router.get("/snapshot/{symbol}")
async def get_market_snapshot(
    symbol: str = Path(..., min_length=1, max_length=32),
    tf: str = Query(DEFAULT_TIMEFRAME, description="bar timeframe"),
    source: str = Query(DEFAULT_SOURCE, description="bar source"),
    include_news: bool = Query(True),
    x_brain_id: Optional[str] = Header(default=None, alias="X-Brain-Id"),
    x_runtime_token: Optional[str] = Header(default=None, alias="X-Runtime-Token"),
    operator_user: Optional[dict] = Depends(_maybe_user),
) -> Dict[str, Any]:
    """Single-symbol derived snapshot. Dual auth (operator or brain).
    Doctrine pinned in `feature_service.py`."""
    principal = await _dual_auth(x_brain_id, x_runtime_token, operator_user)
    snap = await build_market_snapshot(
        symbol, tf=tf, source=source, include_news=include_news,
    )
    snap["served_to"] = principal
    snap["doctrine"] = "derived_evidence_only"
    return snap


@router.get("/snapshot")
async def get_market_snapshot_batch(
    symbols: str = Query(..., description="comma-separated tickers (max 50)"),
    tf: str = Query(DEFAULT_TIMEFRAME),
    source: str = Query(DEFAULT_SOURCE),
    include_news: bool = Query(True),
    x_brain_id: Optional[str] = Header(default=None, alias="X-Brain-Id"),
    x_runtime_token: Optional[str] = Header(default=None, alias="X-Runtime-Token"),
    operator_user: Optional[dict] = Depends(_maybe_user),
) -> Dict[str, Any]:
    """Batch snapshot. Per-symbol failures land in `errors[]`; successful
    rows go in `snapshots[]`. The endpoint NEVER 500s on a single bad
    symbol — partial-success is by design (operator screening a watchlist
    must not have one delisted ticker tank the whole call)."""
    principal = await _dual_auth(x_brain_id, x_runtime_token, operator_user)

    raw_syms = [s.strip().upper() for s in (symbols or "").split(",") if s.strip()]
    if not raw_syms:
        raise HTTPException(status_code=400, detail="symbols query param required")
    if len(raw_syms) > MAX_BATCH_SYMBOLS:
        raise HTTPException(
            status_code=400,
            detail=f"batch limit is {MAX_BATCH_SYMBOLS} symbols (got {len(raw_syms)})",
        )

    async def _one(sym: str) -> tuple[str, Any]:
        try:
            return sym, await build_market_snapshot(
                sym, tf=tf, source=source, include_news=include_news,
            )
        except Exception as exc:  # noqa: BLE001
            return sym, {"error": f"{type(exc).__name__}: {exc}"}

    results: List[tuple[str, Any]] = await asyncio.gather(
        *(_one(s) for s in raw_syms),
    )
    snapshots: List[Dict[str, Any]] = []
    errors: List[Dict[str, str]] = []
    for sym, payload in results:
        if isinstance(payload, dict) and "error" in payload:
            errors.append({"symbol": sym, "error": payload["error"]})
        else:
            snapshots.append(payload)
    return {
        "snapshots": snapshots,
        "errors": errors,
        "served_to": principal,
        "doctrine": "derived_evidence_only",
    }


@router.post("/snapshot/cache/reset-news")
async def reset_news_cache_route(
    _user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Operator escape hatch — clear the news cache. Use after rotating
    the Finnhub API key on prod so the next snapshot call re-fetches
    with the new token instead of serving the previous one's cached
    result."""
    n = reset_news_cache()
    return {"cleared": n, "doctrine": "operator_cache_reset"}

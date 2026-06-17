"""Daily market snapshot retrieval API — dual-auth read paths.

Routes (all prefixed `/api/admin/market-data/daily-snapshots`):
  GET /labels                                 → which labels exist today
  GET /                                       → full universe for one label
  GET /{symbol}                               → single symbol, all labels today
  GET /history/{symbol}                       → single symbol, last N market days
  POST /capture                               → operator-only manual fire (audit)

Auth:
  Same dual-auth pattern as `market_data_snapshot.py`: operator JWT
  via `Authorization: Bearer …` OR brain `X-Brain-Id` + `X-Runtime-Token`.
  Capture (POST) is operator-only.

Doctrine:
  - READ-ONLY for brain callers. Brains cannot trigger a capture.
  - `served_to` echoes the auth principal for audit.
  - All responses tagged `"doctrine": "derived_evidence_only"`.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query

from auth import get_current_user
from db import db
from namespaces import (
    DAILY_MARKET_SNAPSHOTS,
    DAILY_SNAPSHOT_CAPTURE_LOG,
)
from shared.snapshots.nyse_calendar import market_day_today
from shared.snapshots.service import (
    SNAPSHOT_LABELS,
    capture_snapshot,
)


logger = logging.getLogger("risedual.daily_snapshots_api")
router = APIRouter(
    prefix="/admin/market-data/daily-snapshots",
    tags=["daily-snapshots"],
)


KNOWN_BRAINS: tuple[str, ...] = ("camino", "barracuda", "hellcat", "gto")


def _expected_token_for(brain: str) -> str:
    return os.environ.get(f"{brain.upper()}_INGEST_TOKEN", "")


async def _dual_auth(
    x_brain_id: Optional[str],
    x_runtime_token: Optional[str],
    operator_user: Optional[dict],
) -> str:
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
            detail=f"daily-snapshots not configured for {brain}",
        )
    if (x_runtime_token or "") != expected:
        raise HTTPException(status_code=401, detail="invalid token")
    return f"brain:{brain}"


async def _maybe_user(authorization: Optional[str] = Header(default=None)) -> Optional[dict]:
    """Best-effort operator JWT resolution. Mirrors the helper in
    `market_data_snapshot.py` so the brain-token path stays valid
    when JWT is missing/invalid."""
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    try:
        import jwt
        from auth import _secret, JWT_ALGORITHM
        token = authorization.split(" ", 1)[1].strip()
        payload = jwt.decode(token, _secret(), algorithms=[JWT_ALGORITHM])
        if payload.get("type") != "access":
            return None
        user = await db.users.find_one(
            {"id": payload["sub"]}, {"_id": 0, "password_hash": 0},
        )
        return user
    except Exception:  # noqa: BLE001
        return None


# ──────────────────────── GET /labels ────────────────────────


@router.get("/labels")
async def list_today_labels(
    market_day: Optional[str] = Query(
        None,
        description="market day YYYY-MM-DD (defaults to today's NYSE day)",
    ),
    x_brain_id: Optional[str] = Header(default=None, alias="X-Brain-Id"),
    x_runtime_token: Optional[str] = Header(default=None, alias="X-Runtime-Token"),
    operator_user: Optional[dict] = Depends(_maybe_user),
) -> Dict[str, Any]:
    """Which snapshot labels have been captured on the given market
    day (default today)? Returns the capture-log audit rows."""
    principal = await _dual_auth(x_brain_id, x_runtime_token, operator_user)
    md = market_day or market_day_today().isoformat()
    rows = await db[DAILY_SNAPSHOT_CAPTURE_LOG].find(
        {"market_day": md},
        {"_id": 0},
    ).to_list(length=10)
    captured = sorted(
        {r["label"] for r in rows if r.get("label") in SNAPSHOT_LABELS}
    )
    return {
        "market_day": md,
        "captured_labels": captured,
        "pending_labels": [lbl for lbl in SNAPSHOT_LABELS if lbl not in captured],
        "captures": rows,
        "served_to": principal,
        "doctrine": "derived_evidence_only",
    }


# ──────────────────────── GET / (batch by label) ────────────────────────


@router.get("")
@router.get("/")
async def get_snapshot_by_label(
    label: str = Query(..., description=f"one of {SNAPSHOT_LABELS}"),
    market_day: Optional[str] = Query(
        None, description="market day YYYY-MM-DD (default today's NYSE day)",
    ),
    symbols: Optional[str] = Query(
        None,
        description="optional comma-separated filter (e.g., 'AAPL,NVDA,MSFT')",
    ),
    limit: int = Query(
        1000, ge=1, le=1000,
        description="row cap (defaults to entire universe)",
    ),
    x_brain_id: Optional[str] = Header(default=None, alias="X-Brain-Id"),
    x_runtime_token: Optional[str] = Header(default=None, alias="X-Runtime-Token"),
    operator_user: Optional[dict] = Depends(_maybe_user),
) -> Dict[str, Any]:
    """Full universe (or filtered subset) for one snapshot label."""
    principal = await _dual_auth(x_brain_id, x_runtime_token, operator_user)
    if label not in SNAPSHOT_LABELS:
        raise HTTPException(
            status_code=400,
            detail=f"label must be one of {SNAPSHOT_LABELS}",
        )
    md = market_day or market_day_today().isoformat()
    query: Dict[str, Any] = {"market_day": md, "label": label}
    if symbols:
        syms = [s.strip().upper() for s in symbols.split(",") if s.strip()]
        if syms:
            query["symbol"] = {"$in": syms}
    rows = await db[DAILY_MARKET_SNAPSHOTS].find(
        query, {"_id": 0},
    ).sort("symbol", 1).to_list(length=limit)
    return {
        "market_day": md,
        "label": label,
        "count": len(rows),
        "items": rows,
        "served_to": principal,
        "doctrine": "derived_evidence_only",
    }


# ──────────────────────── GET /{symbol} (today, all labels) ────────────────────────


@router.get("/symbol/{symbol}")
async def get_symbol_today(
    symbol: str = Path(..., min_length=1, max_length=16),
    market_day: Optional[str] = Query(
        None, description="market day YYYY-MM-DD (default today's NYSE day)",
    ),
    x_brain_id: Optional[str] = Header(default=None, alias="X-Brain-Id"),
    x_runtime_token: Optional[str] = Header(default=None, alias="X-Runtime-Token"),
    operator_user: Optional[dict] = Depends(_maybe_user),
) -> Dict[str, Any]:
    """All available labels for one symbol on the given market day."""
    principal = await _dual_auth(x_brain_id, x_runtime_token, operator_user)
    md = market_day or market_day_today().isoformat()
    sym = symbol.upper()
    rows = await db[DAILY_MARKET_SNAPSHOTS].find(
        {"market_day": md, "symbol": sym},
        {"_id": 0},
    ).to_list(length=10)
    # Pivot: dict keyed by label for easy brain consumption.
    by_label: Dict[str, Any] = {lbl: None for lbl in SNAPSHOT_LABELS}
    for r in rows:
        by_label[r["label"]] = r
    return {
        "market_day": md,
        "symbol": sym,
        "labels": by_label,
        "served_to": principal,
        "doctrine": "derived_evidence_only",
    }


# ──────────────────────── GET /history/{symbol} ────────────────────────


@router.get("/history/{symbol}")
async def get_symbol_history(
    symbol: str = Path(..., min_length=1, max_length=16),
    days: int = Query(
        5, ge=1, le=5,
        description="how many recent market days (capped at retention=5)",
    ),
    x_brain_id: Optional[str] = Header(default=None, alias="X-Brain-Id"),
    x_runtime_token: Optional[str] = Header(default=None, alias="X-Runtime-Token"),
    operator_user: Optional[dict] = Depends(_maybe_user),
) -> Dict[str, Any]:
    """Last N market days of snapshots for one symbol. Capped at
    retention window (5 trading days)."""
    principal = await _dual_auth(x_brain_id, x_runtime_token, operator_user)
    sym = symbol.upper()
    rows = await db[DAILY_MARKET_SNAPSHOTS].find(
        {"symbol": sym},
        {"_id": 0},
    ).sort("market_day", -1).to_list(length=days * len(SNAPSHOT_LABELS))
    # Group by market_day, label keys.
    by_day: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        md = r["market_day"]
        by_day.setdefault(md, {lbl: None for lbl in SNAPSHOT_LABELS})
        by_day[md][r["label"]] = r
    return {
        "symbol": sym,
        "days_requested": days,
        "days_available": len(by_day),
        "history": [
            {"market_day": md, "labels": labels}
            for md, labels in sorted(by_day.items(), reverse=True)
        ],
        "served_to": principal,
        "doctrine": "derived_evidence_only",
    }


# ──────────────────────── POST /capture (operator only) ────────────────────────


@router.post("/capture")
async def operator_capture(
    label: str = Query(..., description=f"one of {SNAPSHOT_LABELS}"),
    user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Operator escape hatch — manually trigger a capture for today's
    market day with the given label. Useful for backfilling a missed
    scheduled fire (e.g., pod was restarting at 09:35 ET)."""
    if label not in SNAPSHOT_LABELS:
        raise HTTPException(
            status_code=400,
            detail=f"label must be one of {SNAPSHOT_LABELS}",
        )
    summary = await capture_snapshot(label)
    summary["triggered_by"] = user.get("email")
    summary["doctrine"] = "derived_evidence_only"
    return summary

"""
PARADOX wake orders — operator-issued "process this ticker NOW" commands.
=========================================================================

Doctrine pin (2026-02-XX):
    Wake orders are pull-based directives, NOT execution commands. They
    tell a brain "look at SYMBOL on your next loop"; the brain still
    has to produce a valid intent that survives the gate chain. MC
    never bypasses Doctrine (c) — the wake-up panel cannot create a
    trade by itself.

Why pull-based (and not MC → sidecar HTTP push)?
    The existing platform survival layer is one-way: sidecars POST TO
    MC (heartbeats, check-ins). MC does not know sidecar URLs and we
    don't want to introduce that coupling (it breaks portability and
    requires per-environment URL registration). Instead, MC writes a
    signed order to its own DB and the sidecar polls on its next
    heartbeat. Same cadence already in use — zero new infrastructure.

Endpoints:
    POST /api/admin/paradox/wake/{brain}
        JWT-authed (operator). Body: {ticker: str, note?: str}.
        Creates one wake order targeted at {brain} and returns the
        signed envelope.

    POST /api/admin/paradox/wake-all
        JWT-authed (operator). Body: {ticker, note?, brains?: list}.
        Fans out to all LIVE_RUNTIMES (or the provided subset).

    GET /api/admin/paradox/wake-orders/{brain}
        Token-authed via `<BRAIN>_INGEST_TOKEN`. Returns the pending
        (not yet acked, not yet expired) wake orders for that brain.
        Sidecars poll this on their normal heartbeat tick.

    POST /api/admin/paradox/wake-orders/{brain}/{order_id}/ack
        Token-authed. Sidecar consumes the order. Idempotent — a
        second ack on the same order is a no-op.

    GET /api/admin/paradox/wake-orders
        JWT-authed (operator). Recent wake orders (last 24h, default
        limit 50) for the Roster UI to render verdicts inline.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import jwt
from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query
from pydantic import BaseModel, Field, field_validator

from auth import get_current_user
from db import db
from namespaces import LIVE_RUNTIMES, PARADOX_WAKE_ORDERS


router = APIRouter(prefix="/admin/paradox", tags=["paradox-wake"])


# ─────────────────────────────── constants ────────────────────────────

WAKE_ORDER_TTL_SECONDS = 15 * 60  # 15 minutes
WAKE_JWT_ALG = "HS256"
WAKE_JWT_KIND = "wake"


# ─────────────────────────────── helpers ──────────────────────────────


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _secret() -> str:
    s = os.environ.get("JWT_SECRET")
    if not s:
        raise HTTPException(status_code=500, detail="JWT_SECRET not configured")
    return s


def _expected_ingest_token(brain: str) -> str:
    """Per-brain ingest token from .env (same shape as sidecar-checkin)."""
    return os.environ.get(f"{brain.upper()}_INGEST_TOKEN", "") or ""


def _sign_wake_token(order_id: str, brain: str, ticker: str, issued_at: datetime, expires_at: datetime) -> str:
    return jwt.encode(
        {
            "order_id": order_id,
            "brain": brain,
            "ticker": ticker,
            "issued_at": int(issued_at.timestamp()),
            "exp": int(expires_at.timestamp()),
            "kind": WAKE_JWT_KIND,
        },
        _secret(),
        algorithm=WAKE_JWT_ALG,
    )


def _validate_brain(brain: str) -> str:
    brain = (brain or "").lower().strip()
    if brain not in LIVE_RUNTIMES:
        raise HTTPException(
            status_code=404,
            detail=f"unknown brain {brain!r}; expected one of {list(LIVE_RUNTIMES)}",
        )
    return brain


def _verify_ingest_token(brain: str, presented: Optional[str]) -> None:
    expected = _expected_ingest_token(brain)
    if not expected:
        raise HTTPException(
            status_code=500,
            detail=(
                f"no ingest token configured for {brain}; "
                f"set {brain.upper()}_INGEST_TOKEN in backend/.env"
            ),
        )
    if (presented or "") != expected:
        raise HTTPException(status_code=401, detail="invalid token")


def _normalize_ticker(t: str) -> str:
    t = (t or "").strip().upper()
    if not t:
        raise HTTPException(status_code=400, detail="ticker required")
    if len(t) > 16:
        raise HTTPException(status_code=400, detail="ticker too long")
    return t


def _strip_id(doc: Dict[str, Any]) -> Dict[str, Any]:
    doc.pop("_id", None)
    return doc


def _is_expired(doc: Dict[str, Any], now: datetime) -> bool:
    exp = doc.get("expires_at")
    if isinstance(exp, datetime):
        # Mongo strips tzinfo on read — coerce both sides to aware UTC.
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        return exp < now
    if isinstance(exp, str):
        try:
            return datetime.fromisoformat(exp.replace("Z", "+00:00")) < now
        except (ValueError, AttributeError):
            return False
    return False


def _serialize(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Stringify datetimes for JSON response."""
    doc = _strip_id(doc)
    for k in ("issued_at", "expires_at", "acked_at"):
        v = doc.get(k)
        if isinstance(v, datetime):
            if v.tzinfo is None:
                v = v.replace(tzinfo=timezone.utc)
            doc[k] = v.isoformat()
    return doc


# ─────────────────────────────── schemas ──────────────────────────────


class WakeRequest(BaseModel):
    ticker: str = Field(..., description="Symbol to process (uppercased server-side)")
    note: Optional[str] = Field(default=None, description="Operator note")

    @field_validator("note")
    @classmethod
    def _clip_note(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        return v[:500] if v else None


class WakeAllRequest(BaseModel):
    ticker: str
    note: Optional[str] = None
    brains: Optional[List[str]] = Field(
        default=None,
        description="Optional subset of LIVE_RUNTIMES. Omit to wake all.",
    )


class AckRequest(BaseModel):
    ack_note: Optional[str] = Field(default=None, description="Optional sidecar note")


# ─────────────────────────────── POST /wake/{brain} ───────────────────


async def _issue_order(brain: str, ticker: str, note: Optional[str], issued_by: str) -> Dict[str, Any]:
    order_id = str(uuid.uuid4())
    issued_at = _now()
    expires_at = issued_at + timedelta(seconds=WAKE_ORDER_TTL_SECONDS)
    signed = _sign_wake_token(order_id, brain, ticker, issued_at, expires_at)
    doc = {
        "order_id": order_id,
        "brain": brain,
        "ticker": ticker,
        "note": note,
        "signed_token": signed,
        "issued_by": issued_by,
        "issued_at": issued_at,
        "expires_at": expires_at,
        "status": "pending",
        "acked_at": None,
        "ack_note": None,
    }
    await db[PARADOX_WAKE_ORDERS].insert_one(dict(doc))  # copy so insert can't mutate
    return _serialize(doc)


@router.post("/wake/{brain}")
async def post_wake(
    brain: str,
    body: WakeRequest,
    user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Issue a wake order to ONE brain."""
    brain = _validate_brain(brain)
    ticker = _normalize_ticker(body.ticker)
    order = await _issue_order(brain, ticker, body.note, user.get("email", "unknown"))
    return {"ok": True, "order": order}


# ─────────────────────────────── POST /wake-all ───────────────────────


@router.post("/wake-all")
async def post_wake_all(
    body: WakeAllRequest,
    user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Issue a wake order to every LIVE_RUNTIMES brain (or a subset)."""
    ticker = _normalize_ticker(body.ticker)
    if body.brains is not None:
        target_brains = [_validate_brain(b) for b in body.brains]
        if not target_brains:
            raise HTTPException(status_code=400, detail="brains list cannot be empty")
    else:
        target_brains = list(LIVE_RUNTIMES)

    issued_by = user.get("email", "unknown")
    orders = []
    for b in target_brains:
        orders.append(await _issue_order(b, ticker, body.note, issued_by))
    return {"ok": True, "count": len(orders), "orders": orders}


# ─────────────────────────────── GET /wake-orders/{brain} ─────────────


@router.get("/wake-orders/{brain}")
async def list_pending_orders_for_brain(
    brain: str,
    x_runtime_token: Optional[str] = Header(default=None, alias="X-Runtime-Token"),
) -> Dict[str, Any]:
    """Token-authed (per-brain ingest token). Sidecars poll this."""
    brain = _validate_brain(brain)
    _verify_ingest_token(brain, x_runtime_token)

    now = _now()
    cursor = (
        db[PARADOX_WAKE_ORDERS]
        .find({"brain": brain, "status": "pending"})
        .sort("issued_at", -1)
        .limit(50)
    )
    pending = []
    async for d in cursor:
        if _is_expired(d, now):
            # opportunistic mark-expired so the operator UI doesn't keep
            # showing them as pending forever.
            await db[PARADOX_WAKE_ORDERS].update_one(
                {"order_id": d["order_id"]},
                {"$set": {"status": "expired"}},
            )
            continue
        pending.append(_serialize(d))
    return {
        "ok": True,
        "brain": brain,
        "checked_at": now.isoformat(),
        "count": len(pending),
        "orders": pending,
    }


# ─────────────────────────────── POST /wake-orders/{brain}/{id}/ack ───


@router.post("/wake-orders/{brain}/{order_id}/ack")
async def ack_wake_order(
    brain: str,
    order_id: str,
    body: AckRequest,
    x_runtime_token: Optional[str] = Header(default=None, alias="X-Runtime-Token"),
) -> Dict[str, Any]:
    """Token-authed. Sidecar consumes an order. Idempotent."""
    brain = _validate_brain(brain)
    _verify_ingest_token(brain, x_runtime_token)

    doc = await db[PARADOX_WAKE_ORDERS].find_one({"order_id": order_id})
    if not doc:
        raise HTTPException(status_code=404, detail=f"unknown order {order_id!r}")
    if doc.get("brain") != brain:
        raise HTTPException(
            status_code=403,
            detail=f"order {order_id!r} is not targeted at {brain!r}",
        )

    # Idempotent ack — if already acked, return current state without
    # rewriting acked_at (preserves the original consumption time).
    if doc.get("status") == "acked":
        return {"ok": True, "already_acked": True, "order": _serialize(doc)}

    now = _now()
    await db[PARADOX_WAKE_ORDERS].update_one(
        {"order_id": order_id},
        {
            "$set": {
                "status": "acked",
                "acked_at": now,
                "ack_note": (body.ack_note or None),
            },
        },
    )
    fresh = await db[PARADOX_WAKE_ORDERS].find_one({"order_id": order_id})
    return {"ok": True, "already_acked": False, "order": _serialize(fresh or doc)}


# ─────────────────────────────── GET /wake-orders (admin) ─────────────


@router.get("/wake-orders")
async def list_recent_orders(
    hours: int = Query(default=24, ge=1, le=168),
    limit: int = Query(default=50, ge=1, le=200),
    brain: Optional[str] = Query(default=None),
    _user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Admin-only view: recent wake orders for the Roster UI."""
    since = _now() - timedelta(hours=hours)
    q: Dict[str, Any] = {"issued_at": {"$gte": since}}
    if brain:
        q["brain"] = _validate_brain(brain)
    cursor = db[PARADOX_WAKE_ORDERS].find(q).sort("issued_at", -1).limit(limit)
    items = []
    async for d in cursor:
        items.append(_serialize(d))
    return {
        "ok": True,
        "since": since.isoformat(),
        "count": len(items),
        "items": items,
    }

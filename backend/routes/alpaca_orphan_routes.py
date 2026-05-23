"""Alpaca orphan-fill ingester — admin HTTP endpoint.

Doctrine pin (2026-02-18):
    On 2026-05-22 the operator surfaced 995 Alpaca paper orders from
    2026-04-04 → 2026-05-18 with no matching MC `execution_receipts`.
    Almost certainly orphan fills from before Camaro's iter-106m
    key-strip — Camaro had raw Alpaca keys and was POSTing direct.
    Keys are now rotated; MC is sole authority.

    These orders need to be made AUDITABLE without being made
    LEARNABLE. The memory kernel correctly classifies them UV
    (Unverified) because MC's gate chain never ran against them; we
    can't validate the conditions they fired under. Doctrine refuses
    to feed UV samples into expectancy.

    This endpoint:
      * Reads MC's already-loaded Alpaca credentials (no shell env).
      * Fetches FILLED orders in a date window.
      * Upserts each into `broker_orders` (idempotent).
      * Submits each to `/admin/memory-kernel/submit` (UV classification).
      * Returns a count summary.

    What it does NOT do:
      * Write `execution_receipts` rows (those mean MC issued it).
      * Write `observation_receipts` rows (no MC-graded snapshot).
      * Touch `learning_ladder` progress counters.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth import get_current_user
from db import db


logger = logging.getLogger(__name__)


router = APIRouter(prefix="/admin/alpaca", tags=["alpaca-orphans"])


ALPACA_BASE_URL = "https://paper-api.alpaca.markets"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _resolve_creds() -> tuple[str, str]:
    """Pull the live Alpaca credentials MC already has stored. No env
    var fishing — the same creds the auto-router uses."""
    from shared.broker.alpaca_routes import _decrypted_keys  # noqa: WPS433
    keys = await _decrypted_keys()
    if not keys:
        raise HTTPException(
            status_code=412,
            detail="alpaca credentials not configured on MC — "
                   "connect via /admin/alpaca first",
        )
    return keys


async def _fetch_filled(api_key: str, api_secret: str,
                        after: str, until: str,
                        limit: int = 500) -> List[Dict[str, Any]]:
    """Page through Alpaca's /v2/orders with pagination by submitted_at."""
    headers = {
        "APCA-API-KEY-ID": api_key,
        "APCA-API-SECRET-KEY": api_secret,
    }
    cursor_after = after
    all_orders: list = []
    async with httpx.AsyncClient(timeout=30) as cli:
        for _ in range(20):  # hard cap: 20 pages × 500 = 10000 orders
            params = {
                "status": "closed",
                "after": cursor_after,
                "until": until,
                "limit": str(limit),
                "direction": "asc",
            }
            r = await cli.get(
                f"{ALPACA_BASE_URL}/v2/orders",
                headers=headers,
                params=params,
            )
            r.raise_for_status()
            page = r.json()
            if not page:
                break
            all_orders.extend(page)
            if len(page) < limit:
                break
            # advance cursor past the last submitted_at of the page
            last_ts = page[-1].get("submitted_at")
            if not last_ts or last_ts == cursor_after:
                break
            cursor_after = last_ts
    return [o for o in all_orders if o.get("status") == "filled"]


async def _upsert_broker_order(order: Dict[str, Any]) -> str:
    """Idempotent insert into broker_orders."""
    broker_order_id = order["id"]
    doc = {
        "broker_order_id": broker_order_id,
        "symbol": order.get("symbol"),
        "status": "FILLED",
        "filled_qty": float(order.get("filled_qty") or 0),
        "filled_avg_price": float(order.get("filled_avg_price") or 0),
        "side": order.get("side"),
        "submitted_at": order.get("submitted_at"),
        "filled_at": order.get("filled_at"),
        "source": order.get("source", "access_key"),
        "venue": "alpaca",
        "ingested_at": _now_iso(),
        "ingest_origin": "alpaca_orphan_admin_endpoint",
    }
    await db.broker_orders.update_one(
        {"broker_order_id": broker_order_id},
        {"$set": doc},
        upsert=True,
    )
    return broker_order_id


async def _submit_to_kernel(order: Dict[str, Any], jwt: str,
                            base_url: str) -> Dict[str, Any]:
    """POST orphan to memory-kernel/submit. The kernel will write the
    UV quarantine row because no matching execution_receipts exists."""
    payload = {
        "source_stack": "alpaca_orphan",
        "memory_type": "execution",
        "payload": {
            "symbol": order.get("symbol"),
            "broker_order_id": order["id"],
            "filled_qty": float(order.get("filled_qty") or 0),
            "filled_avg_price": float(order.get("filled_avg_price") or 0),
            "side": order.get("side"),
            "submitted_at": order.get("submitted_at"),
            "filled_at": order.get("filled_at"),
            "alpaca_source": order.get("source", "access_key"),
            "ingest_note": "Orphan fill — broker fired without MC receipt.",
        },
        "requested_provenance": None,
    }
    async with httpx.AsyncClient(timeout=30) as cli:
        r = await cli.post(
            f"{base_url}/api/admin/memory-kernel/submit",
            json=payload,
            headers={"Authorization": f"Bearer {jwt}"},
        )
        try:
            r.raise_for_status()
        except httpx.HTTPStatusError:
            return {"provenance": "submit_failed", "error": r.text[:200]}
        return r.json()


# ─────────────────────────── routes ───────────────────────────


class IngestIn(BaseModel):
    after: str = Field(..., description="ISO-8601 start of window")
    until: str = Field(..., description="ISO-8601 end of window (exclusive)")
    dry_run: bool = Field(default=False,
                          description="Only count orders; do NOT write anything")


@router.post("/ingest-orphans")
async def ingest_orphans(
    body: IngestIn,
    user: dict = Depends(get_current_user),  # noqa: B008
):
    """Backfill orphan fills from Alpaca into MC's audit trail.

    Idempotent: re-running over the same window is safe — broker_orders
    upserts on broker_order_id, and the memory kernel's quarantine
    row uses the same key.

    DOCTRINE: Orphan fills are written for AUDIT ONLY. They never
    feed doctrine expectancy. They never populate observation_receipts.
    They never advance learning_ladder counters. If you want to bend
    that doctrine, ship a separate explicit endpoint.
    """
    api_key, api_secret = await _resolve_creds()

    try:
        orders = await _fetch_filled(api_key, api_secret,
                                     after=body.after, until=body.until)
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502,
                            detail=f"alpaca fetch failed: {e!r}") from e

    if body.dry_run:
        # Per-symbol/side summary so operator can preview impact.
        from collections import Counter
        bysym: dict = Counter()
        for o in orders:
            bysym[(o.get("symbol"), o.get("side"))] += 1
        return {
            "dry_run": True,
            "found_count": len(orders),
            "by_symbol_side": [
                {"symbol": k[0], "side": k[1], "count": v}
                for k, v in sorted(bysym.items(), key=lambda x: -x[1])
            ],
            "doctrine_note": (
                "Dry-run: nothing written. Set dry_run=false to actually "
                "ingest these orphan fills to MC's audit trail. "
                "Orphans are NEVER trainable by doctrine."
            ),
        }

    # We can't call the memory kernel by HTTP from inside FastAPI
    # without re-auth dance; call the internal submit function directly
    # if it exists, else fall back to a direct quarantine write.
    ingested_count = 0
    quarantined_count = 0
    error_count = 0
    actor = user.get("email") or "operator"
    for order in orders:
        try:
            await _upsert_broker_order(order)
            ingested_count += 1
            # Direct quarantine write — same as memory kernel's UV path.
            await db.memory_kernel_quarantine.update_one(
                {"broker_order_id": order["id"]},
                {"$set": {
                    "broker_order_id": order["id"],
                    "symbol": order.get("symbol"),
                    "side": order.get("side"),
                    "filled_qty": float(order.get("filled_qty") or 0),
                    "filled_avg_price": float(order.get("filled_avg_price") or 0),
                    "filled_at": order.get("filled_at"),
                    "submitted_at": order.get("submitted_at"),
                    "alpaca_source": order.get("source", "access_key"),
                    "provenance": "UV",
                    "alert_level": "CRITICAL",
                    "reason": "orphan_fill_no_mc_receipt",
                    "ingested_at": _now_iso(),
                    "ingested_by": actor,
                }},
                upsert=True,
            )
            quarantined_count += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("orphan ingest failed for order=%s err=%r",
                           order.get("id"), e)
            error_count += 1

    logger.info(
        "alpaca orphan ingest by %s: window=%s..%s found=%d ingested=%d "
        "quarantined=%d errors=%d",
        actor, body.after, body.until, len(orders),
        ingested_count, quarantined_count, error_count,
    )

    return {
        "after": body.after,
        "until": body.until,
        "found_count": len(orders),
        "ingested": ingested_count,
        "quarantined_UV": quarantined_count,
        "errors": error_count,
        "actor": actor,
        "doctrine_note": (
            "All orphan fills classified UV (Unverified). They are now "
            "auditable in `broker_orders` + `memory_kernel_quarantine`, "
            "but doctrine refuses to train on them — MC's gate chain "
            "never validated the conditions they fired under."
        ),
    }


@router.get("/orphan-summary")
async def orphan_summary(
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    """Read-only summary of ingested orphans (no broker call). Useful
    for operator UI to surface "you have N orphans on file" inline."""
    total = await db.memory_kernel_quarantine.count_documents(
        {"reason": "orphan_fill_no_mc_receipt"},
    )
    by_symbol = await db.memory_kernel_quarantine.aggregate([
        {"$match": {"reason": "orphan_fill_no_mc_receipt"}},
        {"$group": {"_id": "$symbol", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": 50},
    ]).to_list(50)
    return {
        "total_orphans": total,
        "top_symbols": [
            {"symbol": r["_id"], "count": r["count"]} for r in by_symbol
        ],
        "doctrine_note": (
            "Orphan fills exist for audit only. They do NOT feed "
            "doctrine expectancy, observation receipts, or ladder "
            "progress. They will never be trainable."
        ),
    }

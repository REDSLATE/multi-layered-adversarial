"""Executions — the ONE audit collection per order attempt.

Doctrine (2026-02-27 architectural reduction):

Every attempt to route an intent to a broker writes ONE row to the
`executions` collection. Period.

This replaces:
    * shared_gate_results        (direct_execute audit + auto_submit_*)
    * pipeline_receipts          (unified pipeline receipts)
    * shared_auto_submit_audit
    * auto_submit_tiers
    * vote_escalations
    * governor_interventions
    * roadguard_stops
    * shared_executor_rotations  (kept separate — that's seat audit)
    * shared_kraken_audit_log    (kept — broker connectivity audit)

One row per attempt. Reads on this collection answer:
    * Did this intent fire? When? Where did it go? What did the broker say?
    * Daily exposure: sum(notional_usd) where ok=True today.
    * Broker error rate: count by exception_type over time.

Schema:
    intent_id       : str
    ts              : ISO datetime
    brain           : str           (which brain emitted the intent)
    lane            : "equity" | "crypto"
    action          : "BUY" | "SELL" | "SHORT" | "COVER"
    symbol          : str
    notional_usd    : float         (what we asked the broker for)

    decision        : "fire" | "pass"
    seat_holder     : str | None
    seat_reason     : str

    risk_ok         : bool
    risk_reason     : str

    broker          : str | None    ("webull" | "kraken" | ...)
    broker_order_id : str | None
    broker_status   : str | None    ("submitted" | "filled" | "rejected" | "error")
    broker_response : dict | None   (raw payload — capped at 2KB)
    exception_type  : str | None
    exception_msg   : str | None

    ok              : bool          (true iff broker accepted)
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from db import db


logger = logging.getLogger("risedual.executions")
_COLL = "executions"
_MAX_RESPONSE_BYTES = 2048


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _truncate_response(resp: Any) -> Optional[dict]:
    """Truncate large broker responses to ~2KB so we don't bloat Mongo
    on chatty broker APIs. Always returns a dict or None."""
    if resp is None:
        return None
    if isinstance(resp, dict):
        body = resp
    else:
        body = {"raw": str(resp)}
    try:
        s = json.dumps(body, default=str)
        if len(s) > _MAX_RESPONSE_BYTES:
            return {"truncated": True, "preview": s[:_MAX_RESPONSE_BYTES]}
    except Exception:  # noqa: BLE001
        return {"truncated": True, "preview": str(body)[:_MAX_RESPONSE_BYTES]}
    return body


async def record(
    *,
    intent: dict[str, Any],
    seat_verdict: str,
    seat_holder: Optional[str],
    seat_reason: str,
    risk_ok: bool,
    risk_reason: str,
    notional_usd: float,
    broker: Optional[str] = None,
    broker_order_id: Optional[str] = None,
    broker_status: Optional[str] = None,
    broker_response: Any = None,
    exception_type: Optional[str] = None,
    exception_msg: Optional[str] = None,
    ok: bool = False,
) -> str:
    """Write one execution row. Returns the inserted _id as a string.
    Bookkeeping failure is logged but NEVER raised — broker truth
    is authoritative; audit writes cannot block trades."""
    row = {
        "intent_id": intent.get("intent_id"),
        "ts": _now_iso(),
        "brain": (intent.get("stack") or intent.get("brain") or "").lower() or None,
        "lane": (intent.get("lane") or "").lower() or None,
        "action": (intent.get("action") or "").upper() or None,
        "symbol": intent.get("symbol"),
        "notional_usd": float(notional_usd or 0.0),
        "decision": seat_verdict,
        "seat_holder": seat_holder,
        "seat_reason": seat_reason,
        "risk_ok": bool(risk_ok),
        "risk_reason": risk_reason,
        "broker": broker,
        "broker_order_id": broker_order_id,
        "broker_status": broker_status,
        "broker_response": _truncate_response(broker_response),
        "exception_type": exception_type,
        "exception_msg": (exception_msg or "")[:2000] or None,
        "ok": bool(ok),
    }
    try:
        result = await db[_COLL].insert_one(row)
        return str(result.inserted_id)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "executions.record failed intent=%s err=%s",
            row.get("intent_id"), exc,
        )
        return ""


async def recent(
    limit: int = 50,
    *,
    lane: Optional[str] = None,
    brain: Optional[str] = None,
    ok: Optional[bool] = None,
) -> list[dict]:
    """List recent execution rows, newest first. Used by the operator
    tile that answers "what did the broker actually say?". Indexed
    read — covered by `executions_ts_idx` (created in db.py)."""
    q: dict = {}
    if lane:
        q["lane"] = lane.lower()
    if brain:
        q["brain"] = brain.lower()
    if ok is not None:
        q["ok"] = bool(ok)
    cursor = (
        db[_COLL]
        .find(q, {"_id": 0})
        .sort("ts", -1)
        .max_time_ms(8000)
    )
    return await cursor.to_list(limit)


__all__ = ["record", "recent"]

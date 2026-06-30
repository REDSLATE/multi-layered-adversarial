"""Audit — writes to MC's `executions` and the trader's own `trader_receipts`.

Two collections, one shared, one trader-only:

    executions       — the canonical audit, also written to by MC's
                       old auto_router code path. The trader stamps
                       `source: "trader"` on every row so MC tiles
                       can filter to trader-truth.

    trader_receipts  — per-cycle log, owned exclusively by the
                       trader. One row per cycle PER lane (so two
                       rows per cycle when both lanes are active).
                       Captures: brain signals, seat snapshot, risk
                       verdict, broker call result. This is the
                       "what did the loop do this minute?" tape.

Bookkeeping failure is logged but NEVER raised — broker truth is
authoritative; audit writes cannot block a trade or kill the loop.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional


logger = logging.getLogger("trader.audit")
_MAX_RESPONSE_BYTES = 2048


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _truncate(resp: Any) -> Optional[dict]:
    if resp is None:
        return None
    body = resp if isinstance(resp, dict) else {"raw": str(resp)}
    try:
        s = json.dumps(body, default=str)
        if len(s) > _MAX_RESPONSE_BYTES:
            return {"truncated": True, "preview": s[:_MAX_RESPONSE_BYTES]}
    except Exception:  # noqa: BLE001
        return {"truncated": True, "preview": str(body)[:_MAX_RESPONSE_BYTES]}
    return body


async def write_execution(
    db,
    *,
    intent_id: str,
    brain: Optional[str],
    lane: Optional[str],
    action: Optional[str],
    symbol: Optional[str],
    notional_usd: float,
    seats: Optional[dict],
    angels: Optional[dict],
    risk_multiplier: float,
    risk_ok: bool,
    risk_reason: str,
    broker: Optional[str] = None,
    broker_order_id: Optional[str] = None,
    broker_status: Optional[str] = None,
    broker_response: Any = None,
    exception_type: Optional[str] = None,
    exception_msg: Optional[str] = None,
    ok: bool = False,
) -> Optional[str]:
    row = {
        "intent_id": intent_id,
        "ts": _now_iso(),
        "source": "trader",  # ALWAYS stamped — distinguishes from MC
        "brain": (brain or "").lower() or None,
        "lane": (lane or "").lower() or None,
        "action": (action or "").upper() or None,
        "symbol": symbol,
        "notional_usd": float(notional_usd or 0.0),
        "risk_multiplier": float(risk_multiplier or 1.0),
        "decision": "fire" if ok or broker else "pass",
        "seats": seats or {},
        "angels": angels or {},
        "risk_ok": bool(risk_ok),
        "risk_reason": risk_reason,
        "broker": broker,
        "broker_order_id": broker_order_id,
        "broker_status": broker_status,
        "broker_response": _truncate(broker_response),
        "exception_type": exception_type,
        "exception_msg": (exception_msg or "")[:2000] or None,
        "ok": bool(ok),
    }
    try:
        r = await db["executions"].insert_one(row)
        return str(r.inserted_id)
    except Exception as e:  # noqa: BLE001
        logger.error("write_execution failed intent=%s err=%s", intent_id, e)
        return None


async def write_receipt(
    db,
    *,
    cycle_id: str,
    lane: str,
    symbol: str,
    last_price: Optional[float],
    signals: list[dict],
    chosen: Optional[dict],
    seats: dict,
    angels: dict,
    risk_verdict: dict,
    broker_result: Optional[dict] = None,
    error: Optional[str] = None,
) -> Optional[str]:
    """One per-cycle receipt — answers `what did the trader do this
    minute on this lane?`. Operator-visible source of truth for the
    sidecar's behavior."""
    row = {
        "cycle_id": cycle_id,
        "ts": _now_iso(),
        "lane": (lane or "").lower(),
        "symbol": symbol,
        "last_price": last_price,
        "signals": signals,         # list of {brain, verdict, confidence, reason}
        "chosen": chosen,           # the signal that fired (or None)
        "seats": seats,             # snapshot of seat holders at decision time
        "angels": angels,           # angel-name labels for those seats
        "risk": risk_verdict,       # {ok, reason, notional_usd, spent_today_usd}
        "broker_result": _truncate(broker_result),
        "error": error,
        "source": "trader",
    }
    try:
        r = await db["trader_receipts"].insert_one(row)
        return str(r.inserted_id)
    except Exception as e:  # noqa: BLE001
        logger.error("write_receipt failed cycle=%s err=%s", cycle_id, e)
        return None

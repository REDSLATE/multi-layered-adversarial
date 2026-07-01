"""Audit — thin wrapper over the local store.

Doctrine pin (2026-07-01):
    "Write local JSONL receipt immediately. Same receipt to SQLite.
     Sync to Mongo third."

This module used to write directly to Mongo. It now delegates to
`trader.store`, which does JSONL → SQLite → best-effort Mongo mirror.
Signatures are unchanged so `main.py` needs no other edits: the `db`
argument is accepted and ignored (kept in the signature to preserve
history + explicitness).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from trader import store


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
        "source": "trader",
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
    store.record_execution(row)
    return intent_id


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
    row = {
        "cycle_id": cycle_id,
        "ts": _now_iso(),
        "lane": (lane or "").lower(),
        "symbol": symbol,
        "last_price": last_price,
        "signals": signals,
        "chosen": chosen,
        "seats": seats,
        "angels": angels,
        "risk": risk_verdict,
        "broker_result": _truncate(broker_result),
        "error": error,
        "source": "trader",
    }
    rowid = store.record_receipt(row)
    return str(rowid) if rowid else None

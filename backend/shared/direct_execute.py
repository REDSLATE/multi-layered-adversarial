"""Direct-Execute fast path — operator override of the dry-run + gate maze.

Doctrine pin (2026-02-26, operator directive: "The gates and the dry
runs need to be removed. The money is not that serious. I need a
functional stack and this isn't it."):

    When `DIRECT_EXECUTE_MODE=true`, every newly-ingested routable
    intent (BUY / SELL / SHORT / COVER) is shipped straight to
    `broker_router.route_order` at the configured notional. No
    dry-run, no soft gates, no auto-submit policy filter, no
    consensus check, no seat-authority classification.

What still applies (intentionally — these are money safety, not
opinion gates):

    * Broker freeze (operator emergency stop)
    * Per-lane operator broker toggle  (`/api/admin/broker/lanes/...`)
    * Webull pre-trade cap evaluator   (`shared/broker/webull_caps.py`)
    * MC receipt seal                  (`RISEDUAL_BROKER_REQUIRE_MC_RECEIPT`)
    * Adapter credential presence      (broker actually configured)
    * Idempotency — an intent fires AT MOST once

Every direct-execute attempt writes ONE row to
`shared_gate_results` with a `direct_execute_*` kind so the operator
can curl `/api/admin/direct-execute/recent` and see what the broker
actually returned (raw exception type/message included).

Toggle:
    `DIRECT_EXECUTE_MODE=true` in `backend/.env`, or via
    `POST /api/admin/direct-execute/toggle` at runtime.
"""
from __future__ import annotations

import logging
import os
import traceback
from datetime import datetime, timezone
from typing import Any, Optional

from db import db
from namespaces import SHARED_GATE_RESULTS, SHARED_INTENTS

logger = logging.getLogger("risedual.direct_execute")


_DIRECT_EXECUTE_ENV = "DIRECT_EXECUTE_MODE"
_RUNTIME_OVERRIDE_COLL = "shared_direct_execute_state"
_OVERRIDE_ID = "singleton"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _env_enabled() -> bool:
    raw = os.environ.get(_DIRECT_EXECUTE_ENV, "")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


async def is_direct_execute_enabled() -> bool:
    """Runtime-flippable flag. Mongo singleton overrides env so the
    operator can toggle without redeploying. Falls through to env on
    DB hiccup so a Mongo outage cannot silently disable direct execute
    after the operator turned it on."""
    try:
        doc = await db[_RUNTIME_OVERRIDE_COLL].find_one(
            {"_id": _OVERRIDE_ID}, {"_id": 0, "enabled": 1},
        )
        if doc is not None and "enabled" in doc:
            return bool(doc["enabled"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("direct_execute toggle lookup failed: %s; falling back to env", exc)
    return _env_enabled()


async def set_direct_execute_enabled(enabled: bool, actor: str) -> dict[str, Any]:
    """Flip the runtime override + audit-log the change."""
    now = _now_iso()
    await db[_RUNTIME_OVERRIDE_COLL].update_one(
        {"_id": _OVERRIDE_ID},
        {
            "$set": {
                "enabled": bool(enabled),
                "updated_at": now,
                "updated_by": actor,
            },
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )
    return {
        "enabled": bool(enabled),
        "actor": actor,
        "updated_at": now,
    }


def _default_notional() -> float:
    """Per-order notional for direct executes. Capped by the env's
    money-safety per-order cap so direct mode cannot exceed what the
    operator configured. Also clamped by Webull's $1-$10 evaluator
    inside `route_order` — this is just the first ceiling."""
    try:
        env_default = float(
            os.environ.get("DIRECT_EXECUTE_NOTIONAL_USD")
            or os.environ.get("AUTO_ROUTER_NOTIONAL_USD")
            or os.environ.get("RISEDUAL_CAP_PER_ORDER_USD")
            or "5"
        )
    except (TypeError, ValueError):
        env_default = 5.0
    try:
        per_order_cap = float(os.environ.get("RISEDUAL_CAP_PER_ORDER_USD", "10"))
    except (TypeError, ValueError):
        per_order_cap = 10.0
    return max(0.01, min(env_default, per_order_cap))


async def _write_audit(intent_id: str, payload: dict[str, Any]) -> None:
    """Audit row writer. Swallows DB failures — the broker call is
    authoritative; bookkeeping cannot block trades."""
    try:
        await db[SHARED_GATE_RESULTS].insert_one({
            "intent_id": intent_id,
            "ts": _now_iso(),
            "by": "direct_execute",
            **payload,
        })
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "direct_execute audit write failed intent=%s payload=%s err=%s",
            intent_id, payload.get("kind"), exc,
        )


async def direct_execute(
    intent_id: str,
    *,
    actor: str = "direct_execute",
    notional_usd: Optional[float] = None,
) -> dict[str, Any]:
    """Fast-path executor. Loads the intent, calls broker_router, records
    the outcome. Returns a verdict dict for the caller (caller is
    typically fire-and-forget so the return value is mostly for tests
    + the admin endpoint that can synchronously re-fire an intent).
    """
    intent = await db[SHARED_INTENTS].find_one(
        {"intent_id": intent_id}, {"_id": 0},
    )
    if not intent:
        await _write_audit(intent_id, {
            "kind": "direct_execute_skipped",
            "reason": "intent_not_found",
        })
        return {"verdict": "intent_not_found", "intent_id": intent_id}

    action = (intent.get("action") or "").upper()
    if action not in ("BUY", "SELL", "SHORT", "COVER"):
        await _write_audit(intent_id, {
            "kind": "direct_execute_skipped",
            "reason": f"non_routable_action:{action!r}",
            "skip_category": "non_routable_action",
        })
        return {"verdict": "non_routable_action", "action": action}

    if intent.get("executed"):
        return {
            "verdict": "already_executed",
            "executed_at": intent.get("executed_at"),
        }

    notional = float(notional_usd) if notional_usd is not None else _default_notional()

    # ─── Broker call ─────────────────────────────────────────────────
    # Lazy import: avoids a circular at module-load time (broker_router
    # imports a number of submodules that may import this module via
    # the admin route registration in router_registry).
    from shared.broker_router import BrokerRouteBlocked, route_order  # noqa: WPS433

    try:
        order = await route_order(
            intent,
            notional_usd=notional,
            client_order_id=f"de-{intent_id[:24]}",
        )
    except BrokerRouteBlocked as exc:
        # Operator-controlled NO_TRADE — freeze, lane toggle, cap.
        # These are doctrinally NOT the "soft gates" the operator
        # wanted gone; surface cleanly and stop.
        reason = str(exc)
        await _write_audit(intent_id, {
            "kind": "direct_execute_blocked",
            "reason": reason,
            "skip_category": "broker_route_blocked",
            "intent_notional_usd": notional,
        })
        return {"verdict": "blocked", "reason": reason}
    except Exception as exc:  # noqa: BLE001
        # Raw broker exception — capture type, message, traceback so
        # the operator can read it from `/api/admin/direct-execute/recent`
        # without grepping supervisor logs.
        exc_type = type(exc).__name__
        exc_msg = str(exc)[:1000]
        tb = traceback.format_exc()[-2000:]
        logger.exception(
            "direct_execute broker call raised intent=%s symbol=%s action=%s "
            "broker_exc=%s msg=%s",
            intent_id, intent.get("symbol"), action, exc_type, exc_msg,
        )
        await _write_audit(intent_id, {
            "kind": "direct_execute_failed",
            "exception_type": exc_type,
            "exception_message": exc_msg,
            "traceback": tb,
            "skip_category": "broker_exception",
            "intent_notional_usd": notional,
            "symbol": intent.get("symbol"),
            "action": action,
            "lane": intent.get("lane"),
        })
        return {
            "verdict": "failed",
            "exception_type": exc_type,
            "exception_message": exc_msg,
        }

    # ─── Success ─────────────────────────────────────────────────────
    await db[SHARED_INTENTS].update_one(
        {"intent_id": intent_id},
        {"$set": {
            "executed": True,
            "executed_at": _now_iso(),
            "executed_by": actor,
            "gate_state": "submitted_direct",
            "dry_run_state": "skipped_direct_execute",
            "direct_execute_order": {
                k: order.get(k) for k in (
                    "id", "order_id", "broker", "broker_symbol", "canonical",
                    "lane", "side", "qty", "notional", "status",
                    "filled_qty", "filled_avg_price", "submitted_at",
                ) if order.get(k) is not None
            },
        }},
    )
    await _write_audit(intent_id, {
        "kind": "direct_execute_submitted",
        "intent_notional_usd": notional,
        "broker": order.get("broker"),
        "canonical": order.get("canonical"),
        "broker_symbol": order.get("broker_symbol"),
        "status": order.get("status"),
        "order_id": order.get("id") or order.get("order_id"),
        "executed": True,
    })
    logger.info(
        "direct_execute OK intent=%s symbol=%s action=%s notional=%.2f "
        "broker=%s order_id=%s",
        intent_id, intent.get("symbol"), action, notional,
        order.get("broker"), order.get("id") or order.get("order_id"),
    )
    return {
        "verdict": "submitted",
        "intent_id": intent_id,
        "notional_usd": notional,
        "order": order,
    }

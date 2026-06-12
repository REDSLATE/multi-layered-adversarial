"""Webull bracket-intent recorder (P1, 2026-02-19).

Operator directive: Webull supports a full toolkit of bracket order
types (OTOCO, OCO, market+TP/SL). The atomic Webull-side bracket
submit is high-risk to wire today (the SDK has no combo method; would
need custom HTTP), but the TRAINING-VALUE crux is the cleaner outcome
label:

    today:  outcome_label = win|loss|scratch  (PnL-thresholded, noisy)
    after:  outcome_label = tp_hit|sl_hit|timeout  (categorical,
                            directly aligned with the brain's thesis)

This module captures the brain's stated thesis (target_price +
stop_price + entry_price) on every order submit so the MC-side
outcome resolver can later assign one of {tp_hit, sl_hit, timeout}
deterministically by polling the live price.

Why it matters for training:
  * Confidence calibration becomes learnable.
    P(tp_hit | confidence ∈ [0.7, 0.8]) is a supervised target.
  * Adversary signal sharpens — redeye learns "when alpha says long
    with conf=0.85, the SL hits 60% of the time".
  * Holding-period bias becomes observable via the `timeout` band.
  * `doctrine_sidecars.outcome_join` consumers (auto_retire,
    scorecards, calibration sweeps) inherit the cleaner labels with
    NO code changes downstream.

This file is the WRITE side. The READ side lives in
`shared/runtime/bracket_outcome_resolver.py`.
"""
from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from db import db
from namespaces import WEBULL_BRACKET_INTENTS

logger = logging.getLogger(__name__)


# Default timeout — how long we'll wait for tp/sl to fire before
# resolving the bracket as `timeout`. 60 min is the operator-tunable
# floor; brains with strong momentum theses may set their own via
# intent.evidence.bracket_timeout_min.
BRACKET_TIMEOUT_MIN_DEFAULT = float(
    os.environ.get("RISEDUAL_BRACKET_TIMEOUT_MIN", "60"),
)


def bracket_outcomes_enabled() -> bool:
    """Master kill-switch. Default OFF so the operator opts in. Hot
    re-read on every call so flipping the env doesn't require a
    restart."""
    return os.environ.get(
        "RISEDUAL_BRACKET_OUTCOMES_ENABLED", "false",
    ).lower() in ("true", "1", "yes", "on")


def _f(v: Any) -> Optional[float]:
    """Permissive float coercion; tolerates None/strings/whitespace."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


async def record_bracket_intent(
    intent: dict,
    order: dict,
    entry_price: float,
) -> Optional[str]:
    """Persist the brain's bracket thesis alongside a freshly-submitted
    order. Returns the bracket_id (a UUID) if recorded, None if the
    intent didn't carry usable target/stop fields or the feature is
    disabled.

    Called from `broker_router.route_order` immediately after the
    adapter returns a successful order envelope.

    Args:
        intent: the full brain intent envelope (carries
            `target_price`, `stop_price`, optional
            `evidence.bracket_timeout_min`, `confidence`, `stack`,
            `symbol`, `lane`).
        order: the adapter's order receipt
            (`order_id`, `client_order_id`, `qty`, `notional`,
            `side`, `submitted_at`).
        entry_price: the fill price MC observed at submit time
            (typically the snapshot price seen by the adapter just
            before placing the order; if the actual fill price comes
            back later the resolver will refine it).
    """
    if not bracket_outcomes_enabled():
        return None

    target_price = _f(intent.get("target_price"))
    stop_price = _f(intent.get("stop_price"))
    if target_price is None or stop_price is None:
        # Brain didn't publish a thesis — nothing to label categorically.
        # The legacy PnL-thresholded outcome resolver still applies to
        # this intent; we just don't get the cleaner label.
        return None
    if target_price <= 0 or stop_price <= 0:
        return None

    side = str(intent.get("action") or order.get("side") or "BUY").upper()
    # Doctrine sanity: for a BUY, target > entry > stop. For SELL, the
    # inverse. If the brain published a malformed bracket we don't
    # write it — the resolver would either fire wrong or wedge.
    if side in ("BUY", "COVER"):
        if not (stop_price < entry_price < target_price):
            logger.warning(
                "bracket_intent malformed BUY: stop=%.4f entry=%.4f tp=%.4f "
                "symbol=%s — skipping bracket capture",
                stop_price, entry_price, target_price, intent.get("symbol"),
            )
            return None
    elif side in ("SELL", "SHORT"):
        if not (target_price < entry_price < stop_price):
            logger.warning(
                "bracket_intent malformed SELL: tp=%.4f entry=%.4f stop=%.4f "
                "symbol=%s — skipping bracket capture",
                target_price, entry_price, stop_price, intent.get("symbol"),
            )
            return None
    else:
        return None

    bracket_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    timeout_min = (
        _f((intent.get("evidence") or {}).get("bracket_timeout_min"))
        or BRACKET_TIMEOUT_MIN_DEFAULT
    )

    doc = {
        "bracket_id": bracket_id,
        "intent_id": intent.get("intent_id"),
        "stack": intent.get("stack"),
        "symbol": intent.get("symbol"),
        "lane": intent.get("lane"),
        "side": side,
        "qty": _f(order.get("qty")) or 0.0,
        "notional": _f(order.get("notional")) or 0.0,
        "entry_price": entry_price,
        "target_price": target_price,
        "stop_price": stop_price,
        "confidence": _f(intent.get("confidence")) or 0.0,
        "broker": order.get("broker") or "webull",
        "broker_order_id": order.get("order_id"),
        "client_order_id": order.get("client_order_id"),
        "opened_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=timeout_min)).isoformat(),
        "timeout_min": timeout_min,
        "status": "open",
        # Filled by the resolver once the bracket resolves:
        "resolved_at": None,
        "resolved_price": None,
        "outcome_label": None,
        "pnl_usd": None,
    }
    try:
        await db[WEBULL_BRACKET_INTENTS].insert_one(doc)
    except Exception as e:  # noqa: BLE001
        # Best-effort capture — never block the trade on a write
        # failure. A missed bracket label is a soft loss; the legacy
        # outcome resolver still produces a row.
        logger.warning(
            "bracket_intent insert failed bracket_id=%s symbol=%s: %s",
            bracket_id, intent.get("symbol"), e,
        )
        return None

    logger.info(
        "bracket_intent recorded bracket_id=%s symbol=%s side=%s "
        "entry=%.4f tp=%.4f stop=%.4f conf=%.2f timeout=%.0fmin",
        bracket_id, intent.get("symbol"), side, entry_price,
        target_price, stop_price, doc["confidence"], timeout_min,
    )
    return bracket_id

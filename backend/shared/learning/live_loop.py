"""Live-experience capture — the entry hook to the learning loop.

Called from `shared/auto_router.py::_route_one` on BOTH the success
and the reject terminal paths. Every intent that reached the broker
door (fill or reject) writes a row into `learning_experiences`.

Design principles:
    * NEVER block the auto-router tick. Best-effort — a failure to
      write a learning row must NEVER kill an order-in-progress.
    * Capture the FULL intent context (snapshot + doctrine packet)
      at execution time — the resolver looks up these features later
      when bucketing, so a lossy capture destroys the training set.
    * Reject captures are as valuable as fill captures. "Kraken
      rejected me for insufficient funds at 09:42" is a lesson.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional


logger = logging.getLogger("shared.learning.live_loop")

# Namespace — kept out of `namespaces.py` for now so the module stays
# self-contained. Migrate to the central registry when the analyzer /
# lesson-queue collections land in Stage 2.
LEARNING_EXPERIENCES = "learning_experiences"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def should_learn_from(intent: dict) -> bool:
    """Predicate: is this intent worth capturing as a learning
    experience?

    Rules (2026-07-09 operator spec):
      1. Directional (BUY/SELL) — HOLD is not a market touch.
      2. Positive notional — micro-live probes ($1/$5) count;
         zero-sized intents don't.
      3. Recognized lane — anything outside {equity, crypto} is a
         dev-only synthetic intent, not real market exposure.
    """
    exec_blk = intent.get("execution") or {}
    action = (exec_blk.get("action") or intent.get("action") or "").upper()
    notional = exec_blk.get("notional_usd") or intent.get("final_notional_usd") or 0
    lane = (intent.get("lane") or "").lower()

    try:
        notional_f = float(notional)
    except (TypeError, ValueError):
        notional_f = 0.0

    return (
        action in {"BUY", "SELL"}
        and notional_f > 0
        and lane in {"equity", "crypto"}
    )


async def capture_experience(
    db,
    *,
    intent: dict,
    broker_receipt: Optional[dict] = None,
    terminal_state: str = "unknown",
    reject_reason: Optional[str] = None,
) -> Optional[str]:
    """Write a single row into `learning_experiences`. Returns the
    intent_id on success, None on skip/failure.

    Args:
        db:              Motor async DB handle.
        intent:          The intent doc as it lives in `shared_intents`
                         at capture time. MUST include `intent_id`,
                         `symbol`, `lane`, `action`. `snapshot` and
                         `doctrine_packet` are captured verbatim.
        broker_receipt:  The broker adapter's response dict, if any.
                         For rejects, pass the taxonomy classification
                         so `reject_reason` fields flow through.
        terminal_state:  One of the auto_router gate_states we treat
                         as final for learning purposes:
                             submitted | filled | broker_rejected |
                             risk_blocked | seat_blocked | ...
        reject_reason:   Human-readable reject label from the taxonomy
                         (e.g. `capital_ledger_cap`, `min_order_notional`).
    """
    if not should_learn_from(intent):
        return None

    intent_id = intent.get("intent_id")
    if not intent_id:
        # Should never happen post-emit, but the learning tape must
        # never crash the writer if it does.
        logger.warning("learning: intent has no intent_id — skipping capture")
        return None

    exec_blk = intent.get("execution") or {}
    action = (exec_blk.get("action") or intent.get("action") or "").upper()
    notional = float(
        exec_blk.get("notional_usd")
        or intent.get("final_notional_usd")
        or 0.0
    )
    receipt = broker_receipt or {}

    fill_price = (
        receipt.get("filled_avg_price")
        or receipt.get("fill_price")
        or (intent.get("snapshot") or {}).get("last_price")
    )

    # Capture the entry price for outcome resolution. If broker
    # didn't fill (rejected), we still stamp the intended entry
    # price (from the snapshot) so post-hoc "what if it had filled"
    # analysis has a reference point.
    entry_price = (
        receipt.get("filled_avg_price")
        or receipt.get("fill_price")
        or (intent.get("snapshot") or {}).get("last_price")
    )

    experience = {
        "intent_id": intent_id,
        "symbol": intent.get("symbol"),
        "lane": (intent.get("lane") or "").lower(),
        "stack": intent.get("stack") or intent.get("stack_canonical"),
        "stack_canonical": (
            intent.get("stack_canonical") or intent.get("stack")
        ),
        "action": action,
        "notional_usd": notional,
        "notional_source": intent.get("notional_source"),

        # Terminal broker outcome at capture time.
        "terminal_state": terminal_state,
        "fill_price": fill_price,
        "entry_price": entry_price,
        "broker": receipt.get("broker") or (intent.get("broker_order") or {}).get("broker"),
        "broker_order_id": (
            receipt.get("txid")
            or receipt.get("order_id")
            or receipt.get("id")
            or (intent.get("broker_order") or {}).get("order_id")
            or (intent.get("broker_order") or {}).get("id")
        ),
        "broker_status": receipt.get("status") or terminal_state,
        "reject_reason": reject_reason,

        # Full feature/doctrine snapshot — the analyzer will bucket
        # experiences by fields inside these blobs later. Store as
        # dict, not as a serialized string, so the aggregator can
        # `$group` on nested keys without a second parse pass.
        "features": intent.get("snapshot") or {},
        "doctrine": intent.get("doctrine_packet") or {},

        "created_at": _now_iso(),

        # Outcome fields — resolved asynchronously by
        # `outcome_resolver.resolve_pending_outcomes()`.
        "outcome_5m_bps": None,
        "outcome_15m_bps": None,
        "outcome_1h_bps": None,
        "outcome_resolved_at_5m": None,
        "outcome_resolved_at_15m": None,
        "outcome_resolved_at_1h": None,
        "realized_pnl_bps": None,
        "win": None,
    }

    try:
        # Upsert on intent_id so re-captures (e.g. a rejected intent
        # that gets retried and later fills) OVERWRITE — the learning
        # row should always reflect the terminal state, not the
        # midway-through one.
        await db[LEARNING_EXPERIENCES].update_one(
            {"intent_id": intent_id},
            {"$set": experience},
            upsert=True,
        )
        return intent_id
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "learning.capture_experience failed intent_id=%s: %s",
            intent_id, exc,
        )
        return None

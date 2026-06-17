"""Adapter: legacy auto-router intent → unified pipeline call.

When `UNIFIED_PIPELINE_ENABLED=true`, the auto-router delegates the
entire decision to `shared.pipeline.execution_pipeline`. This module
is the ONE place that translates between the legacy `intent` dict
shape (from `shared_intents`) and the new `BrainOpinion` dataclass,
and wraps the existing `route_order` so the pipeline can call it as
`broker.submit_market_order(...)`.

Doctrine: this adapter does NOT add any new gating, sizing, or
classification. It is a pure translation layer. The unified pipeline
is the only authority on whether the broker is called.
"""
from __future__ import annotations

import logging
import os
import uuid
from typing import Any, Dict

from shared.broker_router import route_order
from shared.pipeline import (
    BrainOpinion,
    PipelineReceipt,
    run_execution_pipeline,
)
from shared.pipeline.governor import Governor
from shared.pipeline.receipts import ReceiptStore
from shared.pipeline.roadguard import RoadGuard
from shared.pipeline.seat_policy import SeatPolicy


logger = logging.getLogger("pipeline.adapter")


def is_pipeline_enabled() -> bool:
    """Single source of truth for the feature flag."""
    return os.environ.get("UNIFIED_PIPELINE_ENABLED", "false").lower() == "true"


def _opinion_from_intent(intent: Dict[str, Any], requested_notional: float) -> BrainOpinion:
    """Pure translator. Reads legacy intent fields into the pipeline shape.

    Doctrine layer outputs already present on the intent (governor
    risk_multiplier, doctrine quality, auditor objections) are folded
    into `evidence` so the Governor and the /why endpoint can read
    them without re-running the doctrine layer.
    """
    evidence: Dict[str, Any] = {}

    # Doctrine packet → evidence (read-only; not a gate).
    pkt = intent.get("doctrine_packet") or {}
    base_labels = (pkt.get("base_labels") or {})
    evidence["doctrine_quality"] = base_labels.get("quality")
    evidence["doctrine_score"] = base_labels.get("score")
    evidence["doctrine_labels"] = list(base_labels.get("labels") or [])

    # Governor stance from the doctrine packet (if shipped).
    governor_node = (pkt.get("governor") or {})
    rm = governor_node.get("risk_multiplier")
    if rm is not None:
        evidence["risk_multiplier"] = rm

    # Brain-emitted evidence fields the doctrine layer expects.
    for k in ("spread_bps", "rvol", "earnings_within_days", "halt_risk",
              "buying_power", "market_open"):
        if k in intent:
            evidence[k] = intent[k]

    return BrainOpinion(
        intent_id=str(intent.get("intent_id") or uuid.uuid4()),
        brain_id=str(intent.get("stack") or intent.get("brain_id") or "").lower(),
        lane=str(intent.get("lane") or "").lower(),
        symbol=str(intent.get("symbol") or "").upper(),
        action=str(intent.get("action") or "HOLD").upper(),
        confidence=float(intent.get("confidence") or 0.0),
        notional_usd=float(requested_notional or 0.0),
        evidence=evidence,
    )


class _BrokerAdapter:
    """Wraps `route_order` in the `submit_market_order` shape the
    pipeline expects. Lives for one pipeline invocation."""

    def __init__(self, intent: Dict[str, Any]):
        self._intent = intent

    async def submit_market_order(
        self,
        *,
        symbol: str,
        side: str,
        notional_usd: float,
        lane: str,
    ) -> Dict[str, Any]:
        # `side` in the pipeline matches `action` in the legacy intent.
        # `route_order` reads `intent["action"]` so we make sure it's
        # the canonical BUY/SELL — the pipeline guarantees this.
        intent_for_route = dict(self._intent)
        intent_for_route["action"] = side
        intent_for_route["symbol"] = symbol
        intent_for_route["lane"] = lane
        client_order_id = f"up-{symbol.lower()}-{uuid.uuid4().hex[:8]}"
        order = await route_order(
            intent_for_route,
            notional_usd=notional_usd,
            client_order_id=client_order_id,
        )
        return {"status": "submitted", "broker_order": order}


def _verdict_from_receipt(receipt: PipelineReceipt) -> Dict[str, Any]:
    """Map PipelineReceipt → legacy auto-router response dict.

    Keeps the existing auto-router callers (status endpoint, smoke
    tests, post-mortem aggregator) working without touching them.
    """
    if receipt.final_status == "SUBMITTED":
        verdict = "executed"
    elif receipt.final_status in ("BLOCKED", "NO_ORDER"):
        verdict = "no_trade"
    elif receipt.final_status == "DECISION_LOGGED":
        verdict = "advisory_only"
    elif receipt.final_status == "BROKER_ERROR":
        verdict = "error"
    else:
        verdict = "no_trade"
    return {
        "intent_id": receipt.intent_id,
        "verdict": verdict,
        "reason": receipt.final_reason,
        "restriction_source": receipt.restriction_source,
        "final_status": receipt.final_status,
        "broker_called": receipt.broker_called,
        "final_notional": receipt.final_notional,
        "pipeline": "unified",
    }


async def run_unified_for_intent(
    intent: Dict[str, Any],
    requested_notional: float,
) -> Dict[str, Any]:
    """Top-level entry point used by the auto-router when the flag
    is on. Returns the legacy verdict dict."""
    opinion = _opinion_from_intent(intent, requested_notional)
    receipt = await run_execution_pipeline(
        opinion,
        seat_policy=SeatPolicy(),
        governor=Governor(),
        roadguard=RoadGuard(),
        broker=_BrokerAdapter(intent),
        receipt_store=ReceiptStore(),
    )
    logger.info(
        "unified_pipeline intent=%s brain=%s lane=%s symbol=%s status=%s "
        "source=%s reason=%s notional=%.2f",
        receipt.intent_id, receipt.brain_id, receipt.lane, receipt.symbol,
        receipt.final_status, receipt.restriction_source,
        receipt.final_reason, receipt.final_notional,
    )
    return _verdict_from_receipt(receipt)

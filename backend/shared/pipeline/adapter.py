"""Adapter: legacy auto-router intent → unified pipeline call.

The auto-router delegates the entire decision to
`shared.pipeline.execution_pipeline`. This module is the ONE place
that translates between the legacy `intent` dict shape (from
`shared_intents`) and the new `BrainOpinion` dataclass, and wraps
the existing `route_order` so the pipeline can call it as
`broker.submit_market_order(...)`.

Doctrine: this adapter does NOT add any new gating, sizing, or
classification beyond the Intent Firewall security pre-check. It is
a pure translation layer plus a thin security gate. The unified
pipeline is the only authority on whether the broker is called.

Pipeline doctrine (2026-06-22, operator-pinned 5-stage model)::

    Brain → Intent Firewall → Seat → Trade Governor → RoadGuard → Broker

The Intent Firewall (`shared.security.intent_firewall`) sits between
the brain emit and the seat. It blocks ONLY for security violations
(prompt injection, secret exfiltration, broker directives, etc.) —
never for trading logic. Default deploy phase is OBSERVE (stamp
receipt, do not block) — set `MYTHOS_DEPLOY_PHASE=BLOCK` once the
false-positive rate is baselined.

History (2026-06-18): the legacy `unified_pipeline_enabled` feature
flag (env + Mongo + 5-second cache) was deleted in the same pass
that removed the legacy 20-gate chain from auto_router.py. The
pipeline is now unconditional — there is no fallback path. Operator
kill switches are `/api/admin/auto-router/stop` (full loop halt) and
`/api/admin/trading/disable` (per-order RoadGuard hard stop).
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
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
from shared.security.intent_firewall import intent_firewall_check


logger = logging.getLogger("pipeline.adapter")


def _intent_dict_for_firewall(intent: Dict[str, Any]) -> Dict[str, Any]:
    """Project the legacy intent dict into the shape the Intent
    Firewall expects. The firewall reads brain_id, runtime_origin,
    action, lane, broker_directive plus any HIGH_RISK_FIELDS
    (reasoning, metadata, tool_payload, memory_write, freeform_notes,
    broker_directive) that happen to be on the intent.

    Doctrine: this is a read-only projection. The firewall MUST NOT
    mutate the intent; we only pass the fields it cares about so a
    legacy intent missing `runtime_origin` is treated as in-process
    (the auto-router runs in-process).
    """
    out: Dict[str, Any] = {
        "brain_id": str(
            intent.get("brain_id") or intent.get("stack") or ""
        ).lower(),
        "runtime_origin": intent.get("runtime_origin") or "in_process",
        "lane": str(intent.get("lane") or "").lower(),
        "action": str(intent.get("action") or "HOLD").upper(),
        "broker_directive": intent.get("broker_directive"),
    }
    # HIGH_RISK_FIELDS — pass through if present so the scanner can
    # inspect them. Missing fields are simply skipped.
    for field in ("reasoning", "metadata", "memory_write",
                  "tool_payload", "freeform_notes", "signed_source",
                  "research_ts"):
        if field in intent and intent[field] is not None:
            out[field] = intent[field]
    return out


def _firewall_blocked_receipt(
    intent: Dict[str, Any],
    requested_notional: float,
    fw_receipt: Dict[str, Any],
) -> PipelineReceipt:
    """Build a PipelineReceipt for an intent the firewall refused.
    Mirrors the shape `run_execution_pipeline` produces so downstream
    consumers (auto-router status, /why endpoint, UI) don't need a
    special case for firewall blocks."""
    return PipelineReceipt(
        intent_id=str(intent.get("intent_id") or uuid.uuid4()),
        brain_id=str(intent.get("stack") or intent.get("brain_id") or "").lower(),
        lane=str(intent.get("lane") or "").lower(),
        symbol=str(intent.get("symbol") or "").upper(),
        action=str(intent.get("action") or "HOLD").upper(),
        confidence=float(intent.get("confidence") or 0.0),
        final_status="BLOCKED",
        final_reason=str(fw_receipt.get("reason") or "MYTHOS_BLOCKED"),
        restriction_source="firewall",
        requested_notional=float(requested_notional or 0.0),
        final_notional=0.0,
        broker_called=False,
        autonomy_mode="",
        governor_multiplier=1.0,
        evidence_snapshot={
            "firewall": {
                "severity": fw_receipt.get("severity"),
                "deploy_phase": fw_receipt.get("deploy_phase"),
                "lockdown_triggered": fw_receipt.get("lockdown_triggered"),
                "would_have_severity": fw_receipt.get("would_have_severity"),
            },
        },
        ts=datetime.now(timezone.utc).isoformat(),
    )


def _opinion_from_intent(intent: Dict[str, Any], requested_notional: float) -> BrainOpinion:
    """Pure translator. Reads legacy intent fields into the pipeline shape.

    Doctrine layer outputs already present on the intent (governor
    risk_multiplier, doctrine quality, auditor objections) are folded
    into `evidence` so the Governor and the /why endpoint can read
    them without re-running the doctrine layer.
    """
    # 2026-02 (Step 5) — lift the intent doc into v3 shape so the
    # planning block is available to SeatPolicy. v2 docs come back
    # with a synthesised plan; v3 docs pass through with defaults.
    from shared.intent_envelope_v3 import normalize_intent  # noqa: WPS433
    _v3_lifted = normalize_intent(intent) or {}

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
        # ─── Paradox v3 lift (Step 5) ─────────────────────────────
        # Lift the persisted intent doc into v3 shape so SeatPolicy
        # can read `plan.intent` uniformly across v2 + v3 rows.
        # `normalize_intent` synthesises the plan from `action` for
        # v2 docs (per PRD §6.2 mapping). For v3 docs it passes the
        # operator-shipped plan through with defaults filled.
        intent_version=_v3_lifted.get("intent_version"),
        plan=_v3_lifted.get("plan"),
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
    is on. Returns the legacy verdict dict.

    Stage 0 (Intent Firewall): security pre-check. Blocks ONLY for
    security violations — never for trading logic. In OBSERVE phase
    (the default), blocks are downgraded to WARN and the intent
    proceeds; the would-have-blocked reason is preserved on the
    receipt for audit. In BLOCK phase, a firewall block short-
    circuits the pipeline and writes a BLOCKED receipt with
    `restriction_source="firewall"`.
    """
    # Stage 0 — Intent Firewall (security only).
    fw_receipt = intent_firewall_check(_intent_dict_for_firewall(intent))
    if not fw_receipt.get("allowed", True):
        receipt = _firewall_blocked_receipt(intent, requested_notional, fw_receipt)
        await ReceiptStore().write(receipt)
        logger.warning(
            "intent_firewall_blocked intent=%s brain=%s lane=%s symbol=%s "
            "reason=%s severity=%s phase=%s",
            receipt.intent_id, receipt.brain_id, receipt.lane, receipt.symbol,
            receipt.final_reason, fw_receipt.get("severity"),
            fw_receipt.get("deploy_phase"),
        )
        return _verdict_from_receipt(receipt)

    opinion = _opinion_from_intent(intent, requested_notional)
    # Stamp the firewall receipt onto the opinion's evidence so the
    # /why endpoint can surface "passed firewall in OBSERVE mode"
    # even when the firewall didn't block. Cheap, audit-grade.
    opinion.evidence["firewall"] = {
        "allowed": fw_receipt.get("allowed"),
        "reason": fw_receipt.get("reason"),
        "severity": fw_receipt.get("severity"),
        "deploy_phase": fw_receipt.get("deploy_phase"),
        "would_have_severity": fw_receipt.get("would_have_severity"),
    }

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

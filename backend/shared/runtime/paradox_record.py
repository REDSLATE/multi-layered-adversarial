"""
Paradox-record writer
=====================

Doctrine: AUDITOR is not a seat. It is the emergent function of the
(executor, opponent) interaction — this artifact.

The kernel writes ONE paradox_record per gate evaluation. The
record preserves:
  * what the executor wanted to do
  * what the opponent challenged (or shadow-observed / was offline)
  * the kernel's final verdict (APPROVED / DAMPENED / REJECTED)

`audit_status` is the operator-facing accountability marker:
  * final     → opponent was live and weighed in
  * shadow    → opponent in observation mode; trade fired anyway
  * unaudited → opponent offline; operator must be aware

Reads are at `/api/admin/paradox/records`. The collection name is
`namespaces.PARADOX_RECORDS`.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from db import db
from namespaces import (
    OPPONENT_MODE_LIVE,
    OPPONENT_MODE_OFFLINE,
    OPPONENT_MODE_SHADOW,
    PARADOX_RECORDS,
    ROLE_ANCHORS,
)


def _opponent_mode() -> str:
    declared = os.environ.get("OPPONENT_MODE", OPPONENT_MODE_SHADOW)
    if declared not in {OPPONENT_MODE_LIVE, OPPONENT_MODE_SHADOW, OPPONENT_MODE_OFFLINE}:
        return OPPONENT_MODE_OFFLINE
    return declared


def _audit_status(mode: str) -> str:
    return {
        OPPONENT_MODE_LIVE: "final",
        OPPONENT_MODE_SHADOW: "shadow",
        OPPONENT_MODE_OFFLINE: "unaudited",
    }.get(mode, "unaudited")


def _summarise_gates(gates: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Compact view of the gate chain for the audit record."""
    return {
        "all_passed": all(g.get("passed") for g in gates) if gates else False,
        "first_block": next(
            ({"name": g.get("name"), "reason": g.get("reason")}
             for g in gates if not g.get("passed")),
            None,
        ),
        "gate_names": [g.get("name") for g in gates],
    }


def _classify_verdict(gate_summary: Dict[str, Any], risk_multiplier: Optional[float]) -> str:
    """Map the gate-chain output to a verdict label."""
    if not gate_summary.get("all_passed"):
        return "REJECTED"
    if risk_multiplier is not None and risk_multiplier < 1.0:
        return "DAMPENED"
    return "APPROVED"


async def write_paradox_record(
    *,
    intent: Dict[str, Any],
    gates: List[Dict[str, Any]],
    risk_multiplier: Optional[float] = None,
    evaluation_kind: str = "dry_run",
    evaluated_by: Optional[str] = None,
) -> Dict[str, Any]:
    """Write a paradox_record after gate evaluation.

    Best-effort: failures are logged and swallowed; this is an audit
    side-effect and must never break the live gate flow.

    Returns the inserted document (without `_id`) on success, or a
    minimal stub if writing failed.
    """
    try:
        mode = _opponent_mode()
        gate_summary = _summarise_gates(gates)
        verdict = _classify_verdict(gate_summary, risk_multiplier)
        audit_status = _audit_status(mode)

        # Executor call snapshot — the directional intent as posted.
        executor_call = {
            "symbol": intent.get("symbol"),
            "direction": intent.get("direction") or intent.get("action"),
            "confidence": intent.get("confidence"),
            "lane": intent.get("lane"),
            "source_stack": intent.get("stack") or intent.get("source_stack"),
        }

        # Opponent challenge surface. Today we surface the gate-chain
        # output as a proxy for the opponent's view. When REDEYE
        # returns to live mode, a true opponent challenge payload will
        # arrive via intent.brain_packets or a dedicated endpoint and
        # will overlay this section.
        opponent_challenge = (
            None
            if mode == OPPONENT_MODE_OFFLINE
            else {
                "mode": mode,
                "gates_view": gate_summary,
                "risk_multiplier": risk_multiplier,
            }
        )

        record = {
            "intent_id": intent.get("intent_id"),
            "executor_runtime": ROLE_ANCHORS["executor"],
            "executor_call": executor_call,
            "opponent_runtime": ROLE_ANCHORS["opponent"],
            "opponent_mode": mode,
            "opponent_challenge": opponent_challenge,
            "kernel_verdict": verdict,
            "audit_status": audit_status,
            "evaluation_kind": evaluation_kind,
            "evaluated_by": evaluated_by,
            "gate_summary": gate_summary,
            "risk_multiplier": risk_multiplier,
            "created_at": datetime.now(timezone.utc),
        }

        await db[PARADOX_RECORDS].insert_one(record)
        record.pop("_id", None)
        return record
    except Exception as e:  # noqa: BLE001 — best-effort audit side-effect
        # Don't crash the live path on an audit-write failure; log a
        # minimal stub the caller can inspect during debugging.
        return {
            "ok": False,
            "error": str(e),
            "intent_id": intent.get("intent_id"),
        }

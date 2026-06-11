"""Safety-gate audit — operator decision-support for re-evaluating
the 4 gates the operator wants to review after market close:

  * `lane_execution_enabled`
  * `executor_seat_check`
  * `governor_authority`
  * `roadguard_spread_floor`

Reads `shared_gate_results` (one row per gate check on an intent) and
returns per-gate pass / block stats with a sample of recent block
reasons, sliced by lookback window. Pure read.

Operator workflow:
  1. After market close, hit `/api/admin/safety-gates/audit?hours=24`.
  2. Inspect block-rate per gate + sample block-reasons.
  3. Decide which gates to relax / keep / tighten.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query

from auth import get_current_user
from db import db
from namespaces import SHARED_GATE_RESULTS

router = APIRouter(prefix="/admin/safety-gates", tags=["safety-gates"])

TARGET_GATES = (
    "lane_execution_enabled",
    "executor_seat_check",
    "governor_authority",
    "roadguard_spread_floor",
)


@router.get("/audit")
async def audit(
    hours: float = Query(default=24.0, ge=0.0, le=24 * 30),
    gates: Optional[List[str]] = Query(default=None),
    sample_size: int = Query(default=5, ge=0, le=50),
    _user: dict = Depends(get_current_user),  # noqa: B008
) -> Dict[str, Any]:
    """Per-gate pass / block stats over the lookback window.

    `hours=0` means "no time filter — all history".
    """
    target_gates = gates if gates else list(TARGET_GATES)

    q: Dict[str, Any] = {}
    if hours > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        q["ts"] = {"$gte": cutoff.isoformat()}

    # Pull only the fields we need to keep the payload light.
    rows = await db[SHARED_GATE_RESULTS].find(
        q,
        {"_id": 0, "intent_id": 1, "ts": 1, "verdict": 1, "gates": 1, "kind": 1},
    ).sort("ts", -1).to_list(50_000)

    per_gate: Dict[str, Dict[str, Any]] = {
        g: {
            "pass_count": 0,
            "block_count": 0,
            "block_reason_samples": [],
            "pass_reason_samples": [],
            "block_reason_buckets": defaultdict(int),
        }
        for g in target_gates
    }
    total_decisions = 0
    verdict_counts: Dict[str, int] = defaultdict(int)
    kind_counts: Dict[str, int] = defaultdict(int)

    for row in rows:
        verdict_counts[row.get("verdict") or "unknown"] += 1
        kind_counts[row.get("kind") or "unknown"] += 1
        for g in row.get("gates") or []:
            name = g.get("name")
            if name not in per_gate:
                continue
            total_decisions += 1
            passed = bool(g.get("passed"))
            reason = (g.get("reason") or "").strip()
            agg = per_gate[name]
            if passed:
                agg["pass_count"] += 1
                if reason and len(agg["pass_reason_samples"]) < sample_size:
                    agg["pass_reason_samples"].append({
                        "reason": reason[:240],
                        "ts": row.get("ts"),
                        "intent_id": row.get("intent_id"),
                    })
            else:
                agg["block_count"] += 1
                # Bucket by first 60 chars of reason for distribution view
                bucket = reason[:60] if reason else "(no reason)"
                agg["block_reason_buckets"][bucket] += 1
                if reason and len(agg["block_reason_samples"]) < sample_size:
                    agg["block_reason_samples"].append({
                        "reason": reason[:240],
                        "ts": row.get("ts"),
                        "intent_id": row.get("intent_id"),
                    })

    # Finalize per-gate output
    gate_payload: List[Dict[str, Any]] = []
    for name in target_gates:
        agg = per_gate[name]
        total = agg["pass_count"] + agg["block_count"]
        block_rate = (agg["block_count"] / total) if total else None
        # Sort buckets by count descending, top 5
        top_buckets = sorted(
            agg["block_reason_buckets"].items(),
            key=lambda x: -x[1],
        )[:5]
        gate_payload.append({
            "gate": name,
            "total_checks": total,
            "pass_count": agg["pass_count"],
            "block_count": agg["block_count"],
            "block_rate": round(block_rate, 4) if block_rate is not None else None,
            "top_block_reasons": [
                {"reason_prefix": k, "count": v} for k, v in top_buckets
            ],
            "block_reason_samples": agg["block_reason_samples"],
            "pass_reason_samples": agg["pass_reason_samples"],
        })

    # Sort gates by block rate descending so the noisiest ones show first
    gate_payload.sort(
        key=lambda x: -(x["block_rate"] if x["block_rate"] is not None else -1),
    )

    return {
        "window_hours": hours,
        "rows_scanned": len(rows),
        "decisions_against_target_gates": total_decisions,
        "verdict_counts": dict(verdict_counts),
        "kind_counts": dict(kind_counts),
        "gates": gate_payload,
        "doctrine_note": (
            "Read-only audit. Block-rate per gate is the operator's primary "
            "signal for relax / keep / tighten decisions. Top block reasons "
            "show which sub-clauses are firing most. Surface inflection "
            "points before market open."
        ),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

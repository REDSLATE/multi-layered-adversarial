"""Hot-Brain Router DRY-RUN endpoint (2026-06-22).

Operator pin (verbatim):
    "That endpoint becomes your truth serum:
       Would RISEDUAL have traded more?
       Which brain is actually hot?
       Is the Kernel helping or overblocking?
       Are we still too conservative?
     Then after one trading day of dry-run data, you can decide
     whether to let the router influence toehold execution."

Doctrine: STRICTLY read-only. The router is DORMANT in this codebase
today — see `shared/brains/hot_brain_router.py` docstring. This
endpoint replays the router's decision tree against intents that
already exist in `shared_intents` over the requested window and
shows what the router WOULD have done. Nothing is written back to
any intent, no audit row is emitted, no broker is touched.

GET /api/admin/hot-brain-router/dry-run?days=1

Response shape (locked by tests/test_hot_brain_router_dry_run_2026_06_22.py):

    {
      "window_days": 1,
      "total_intents": 128,
      "would_block": 12,
      "would_reduce": 34,
      "would_pass": 71,
      "would_elevate": 11,
      "by_brain": {
        "gto": {"hot": 8, "neutral": 15, "cold": 2, "unknown": 0},
        ...
      },
      "examples": [
        {
          "intent_id": "...",
          "brain": "gto",
          "symbol": "NVDA",
          "regime": "trend_up",
          "kernel_adjusted_score": 0.74,
          "route_action": "elevate",
          "reason": "hot_brain_elevated_with_governor_consent"
        },
        ...
      ]
    }

Up to 20 representative examples are returned — spread across the
four route actions so the operator sees a sample of each bucket
rather than 20 ELEVATEs in a row.

Constraints: `days ∈ [1, 7]`. Larger windows hit too many intents
to keep the response sub-second; the operator's stated workflow is
"one trading day at a time."
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query

from auth import get_current_user
from db import db
from namespaces import SHARED_INTENTS
from shared.brains.brain_performance_store import get_recent_brain_performance
from shared.brains.hot_brain_router import (
    RouteAction,
    RouterContext,
    route_hot_brain,
)

logger = logging.getLogger("risedual.hot_brain_dry_run")

router = APIRouter(prefix="/admin/hot-brain-router", tags=["admin-hot-brain-router"])


# Operator wants neutral governor context for the dry-run baseline so
# the router decision shows what it WOULD do under "normal" conditions
# — operator can then sanity-check against the live governor state.
_DRY_RUN_CONTEXT = RouterContext(
    governor_size_mult=1.0,
    governor_vote_required=False,
    verifier_seat_tier="standard",
    roadguard_status="OPEN",
    current_portfolio_heat=0.0,
)

# Per-bucket example cap — total max 20 examples returned, spread
# evenly so operator can eyeball each route_action.
_EXAMPLES_PER_BUCKET = 5


def _empty_brain_tally() -> Dict[str, int]:
    return {"hot": 0, "neutral": 0, "cold": 0, "unknown": 0}


def _regime_hint(intent: Dict[str, Any]) -> str:
    """Best-effort regime label off the intent's persisted evidence.

    Brains stamp `evidence.regime` or `regime` on the intent at emit
    time; we expose it verbatim if present, else fall back to
    "unknown". Dry-run never RECOMPUTES regime — that would re-run a
    classifier against possibly-stale snapshot data and conflict
    with the operator's "read-only" doctrine for this endpoint.
    """
    if "regime" in intent and intent["regime"]:
        return str(intent["regime"]).lower()
    ev = intent.get("evidence") or {}
    if isinstance(ev, dict) and ev.get("regime"):
        return str(ev["regime"]).lower()
    return "unknown"


@router.get("/dry-run")
async def hot_brain_router_dry_run(
    days: int = Query(default=1, ge=1, le=7),
    _user: dict = Depends(get_current_user),  # noqa: B008
) -> Dict[str, Any]:
    """Replay the Hot-Brain Router against the last `days` of
    intents. Read-only — no writes, no broker calls."""
    cutoff_iso = (
        datetime.now(timezone.utc) - timedelta(days=days)
    ).isoformat()

    rows: List[Dict[str, Any]] = await db[SHARED_INTENTS].find(
        {
            "ingest_ts": {"$gte": cutoff_iso},
            "action": {"$in": ["BUY", "SELL", "SHORT", "COVER"]},
        },
        {
            "_id": 0,
            "intent_id": 1,
            "stack": 1,
            "lane": 1,
            "symbol": 1,
            "confidence": 1,
            "regime": 1,
            "evidence": 1,
            "ingest_ts": 1,
        },
    ).sort("ingest_ts", -1).to_list(2000)

    totals = {
        "would_block": 0,
        "would_reduce": 0,
        "would_pass": 0,
        "would_elevate": 0,
    }
    by_brain: Dict[str, Dict[str, int]] = defaultdict(_empty_brain_tally)
    examples_by_action: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

    # Cache per (brain, lane, symbol) so we don't re-aggregate the
    # same triple twice when the brain emitted multiple intents on
    # the same name in the window.
    perf_cache: Dict[tuple, Any] = {}

    for intent in rows:
        brain = (intent.get("stack") or "").lower()
        lane = (intent.get("lane") or "").lower()
        symbol = (intent.get("symbol") or "").upper()
        if not (brain and lane and symbol):
            continue
        key = (brain, lane, symbol)
        if key not in perf_cache:
            try:
                perf_cache[key] = await get_recent_brain_performance(
                    brain, lane, symbol,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "perf fetch failed brain=%s lane=%s sym=%s err=%s",
                    brain, lane, symbol, exc,
                )
                continue
        perf = perf_cache[key]
        decision = route_hot_brain(perf, _DRY_RUN_CONTEXT)

        # Bucket the decision into the four operator-facing totals.
        action_bucket = {
            RouteAction.BLOCK: "would_block",
            RouteAction.REDUCE: "would_reduce",
            RouteAction.PASS_THROUGH: "would_pass",
            RouteAction.ELEVATE: "would_elevate",
        }[decision.route_action]
        totals[action_bucket] += 1
        by_brain[brain][decision.state.lower()] += 1

        # Collect example up to the per-bucket cap. Spreading the
        # examples across buckets gives the operator a balanced view
        # rather than 20 ELEVATEs.
        if len(examples_by_action[action_bucket]) < _EXAMPLES_PER_BUCKET:
            examples_by_action[action_bucket].append({
                "intent_id": intent.get("intent_id"),
                "brain": brain,
                "symbol": symbol,
                "regime": _regime_hint(intent),
                "kernel_adjusted_score": round(decision.lane_adjusted_score, 4),
                "route_action": decision.route_action.value,
                "reason": decision.reason,
            })

    # Flatten examples in a stable order: ELEVATE first (most actionable
    # for the "would we have traded more?" question), then PASS,
    # REDUCE, BLOCK. Operator's gaze hits the most-actionable rows.
    examples: List[Dict[str, Any]] = []
    for bucket in ("would_elevate", "would_pass", "would_reduce", "would_block"):
        examples.extend(examples_by_action.get(bucket, []))

    return {
        "window_days": days,
        "total_intents": len(rows),
        **totals,
        "by_brain": dict(by_brain),
        "examples": examples,
        # Diagnostic context — surfaces the kernel's dormant state +
        # the neutral RouterContext used for the dry-run so the
        # operator can answer "is the router scoring me against the
        # current governor's permissions, or the baseline?" without
        # reading the code.
        "dry_run_context": {
            "governor_size_mult": _DRY_RUN_CONTEXT.governor_size_mult,
            "governor_vote_required": _DRY_RUN_CONTEXT.governor_vote_required,
            "verifier_seat_tier": _DRY_RUN_CONTEXT.verifier_seat_tier,
            "roadguard_status": _DRY_RUN_CONTEXT.roadguard_status,
            "current_portfolio_heat": _DRY_RUN_CONTEXT.current_portfolio_heat,
        },
        "router_status": "DORMANT — read-only dry-run only; not wired into live execution",
    }

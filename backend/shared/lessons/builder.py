"""Lesson builder — joins all the layers into one labeled record.

This is the read-side core of the Verifier Rule Sheet. Given an
intent_id, it walks the data:

    shared_intents  → brain & research & gate & market context
    execution_receipts → fill price / qty / ts / slippage
    shared_broker_fills (later, by symbol+ts+side) — refines fill
    shared_brain_outcomes / doctrine_sidecars.outcome_join → outcome
    MAE/MFE helper → realized risk shape

Doctrine: builder is READ-ONLY. It NEVER writes to `shared_intents`,
`execution_receipts`, or outcomes. The lesson is computed on demand
(cheap aggregate) and can also be persisted to `shared_lessons` by
the cache writer, but the builder itself is side-effect free.
"""
from __future__ import annotations

import logging
from typing import Optional

from db import db
from namespaces import (
    EXECUTION_RECEIPTS,
    SHARED_INTENTS,
    SHARED_OUTCOMES as SHARED_BRAIN_OUTCOMES,
)

from .mae_mfe import compute_mae_mfe_bps
from .schemas import Lesson, LessonOutcome
from .setup_classifier import classify_setup
from shared.intent_envelope_v3 import normalize_intent  # 2026-02 Paradox v3


_log = logging.getLogger("risedual.lessons.builder")


# Some collections live under names that vary across deployments —
# tolerate the absence of `doctrine_sidecars.outcome_join` rather
# than hard-failing the lesson build.
_OUTCOME_JOIN_COLL = "doctrine_sidecars"


def _strongest(signals: list[dict]) -> tuple[Optional[str], Optional[float]]:
    """Return (direction, score) for the strongest non-HOLD signal."""
    cand = [s for s in (signals or []) if s.get("direction") in ("BUY", "SELL")]
    if not cand:
        return None, None
    cand.sort(key=lambda s: float(s.get("score") or 0.0), reverse=True)
    return cand[0].get("direction"), float(cand[0].get("score") or 0.0)


async def _find_outcome(intent_id: str, opinion_id: Optional[str]) -> dict:
    """Look up an outcome label across the two known sources. Returns
    `{"outcome": LessonOutcome, "label_source": str|None, ...extras}`."""
    # First check shared_brain_outcomes by opinion_id (older path).
    if opinion_id:
        row = await db[SHARED_BRAIN_OUTCOMES].find_one(
            {"opinion_id": opinion_id}, sort=[("resolved_at", -1)],
        )
        if row:
            actual = (row.get("actual") or "").lower()
            mapped: LessonOutcome = (
                "win" if actual == "win"
                else "loss" if actual in ("loss", "lose")
                else "scratch" if actual in ("scratch", "flat")
                else "unknown"
            )
            return {
                "outcome": mapped,
                "label_source": "brain_outcomes",
                "exit_ts": row.get("resolved_at"),
            }

    # Then check the bracket resolver join (newer path).
    try:
        row = await db[_OUTCOME_JOIN_COLL].find_one(
            {"event_type": "outcome_join", "intent_id": intent_id},
            sort=[("ts", -1)],
        )
    except Exception:  # noqa: BLE001
        row = None
    if row:
        label = (row.get("label") or "").lower()
        mapped = (
            "win" if label in ("tp_hit", "win")
            else "loss" if label in ("sl_hit", "loss")
            else "scratch" if label in ("timeout", "scratch")
            else "unknown"
        )
        return {
            "outcome": mapped,
            "label_source": "bracket_resolver",
            "exit_ts": row.get("resolved_at") or row.get("ts"),
            "exit_price": row.get("exit_price"),
        }

    return {"outcome": "unknown", "label_source": None}


async def build_lesson(intent_id: str) -> Optional[Lesson]:
    """Return the labeled lesson for `intent_id`, or None if the
    intent doesn't exist. Outcome may be `"pending"` / `"unknown"`
    when the position is still open or hasn't been resolved yet —
    callers can re-build later to refresh."""
    intent = await db[SHARED_INTENTS].find_one(
        {"intent_id": intent_id}, {"_id": 0},
    )
    if not intent:
        return None

    # ─── Paradox v3 lift (Step 2) ─────────────────────────────────
    # Apply the read-side lifter so v2 lessons carry the synthesised
    # plan/execution shape alongside v3 lessons. Downstream
    # consumers (Setup Memory, Hot-Brain Router perf store, the
    # frontend lesson card) read the v3 fields uniformly without
    # branching on `intent_version`.
    intent = normalize_intent(intent)
    plan = intent.get("plan") or {}
    execution = intent.get("execution") or {}

    evidence = intent.get("evidence") or {}
    snapshot = intent.get("snapshot") or {}
    signals = evidence.get("research_signals") or []
    strongest_dir, strongest_score = _strongest(signals)

    # Execution side
    receipt = await db[EXECUTION_RECEIPTS].find_one(
        {"intent_id": intent_id}, {"_id": 0}, sort=[("executed_at", -1)],
    )
    fill_price = None
    fill_qty = None
    fill_ts = None
    slippage_bps = None
    if receipt:
        fill_price = receipt.get("filled_avg_price")
        fill_qty = receipt.get("filled_qty")
        fill_ts = receipt.get("filled_at") or receipt.get("executed_at")
        # Slippage: if intent had a reference price (from snapshot),
        # compute the deviation; otherwise leave None.
        ref_price = snapshot.get("price")
        if ref_price and fill_price:
            try:
                bps = (float(fill_price) - float(ref_price)) / float(ref_price) * 10_000
                # Sign: positive = paid up (BUY bad), negative = got better (BUY good).
                if (intent.get("action") or "").upper() == "SELL":
                    bps = -bps
                slippage_bps = round(bps, 2)
            except Exception:  # noqa: BLE001
                slippage_bps = None

    # Outcome lookup
    outcome_block = await _find_outcome(
        intent_id, opinion_id=intent.get("opinion_id"),
    )
    exit_price = outcome_block.get("exit_price")
    exit_ts = outcome_block.get("exit_ts")

    # MAE / MFE — only meaningful when we have a fill price.
    mae_bps = mfe_bps = None
    if fill_price and fill_ts:
        try:
            ex = await compute_mae_mfe_bps(
                symbol=intent["symbol"],
                lane=intent.get("lane") or "equity",
                side=intent.get("action") or "BUY",
                fill_price=float(fill_price),
                fill_ts=fill_ts,
                exit_ts=exit_ts,
            )
            mae_bps = ex["mae_bps"]
            mfe_bps = ex["mfe_bps"]
        except Exception as e:  # noqa: BLE001
            _log.warning("mae/mfe compute failed for %s: %s", intent_id, e)

    # P&L (bps) — straightforward when both prices are present.
    pnl_bps = None
    pnl_usd = None
    if fill_price and exit_price:
        try:
            fp = float(fill_price)
            ep = float(exit_price)
            raw_bps = (ep - fp) / fp * 10_000
            if (intent.get("action") or "").upper() == "SELL":
                raw_bps = -raw_bps
            pnl_bps = round(raw_bps, 2)
            if fill_qty:
                pnl_usd = round((ep - fp) * float(fill_qty) *
                                (1.0 if (intent.get("action") or "").upper() == "BUY" else -1.0), 4)
        except Exception:  # noqa: BLE001
            pnl_bps = None

    # If the intent was blocked, derive missed/avoided when we have
    # any forward-bar info on hand (cheap proxy: if research is BUY
    # and intent is BUY but action didn't execute, and price went up,
    # that's a missed trade).
    final_outcome: LessonOutcome = outcome_block["outcome"]
    if not intent.get("executed") and final_outcome == "unknown":
        # Best-effort missed/avoided based on next-bar move when
        # available. Avoid noisy heuristics on intents <10 minutes old.
        final_outcome = "pending"

    governor_block = evidence.get("governor") or {}
    return Lesson(
        intent_id=intent_id,
        stack=intent.get("stack") or "?",
        lane=intent.get("lane") or "?",
        symbol=intent.get("symbol") or "?",
        action=(intent.get("action") or "").upper(),
        confidence=float(intent.get("confidence") or 0.0),
        rationale=intent.get("rationale"),
        posted_at=intent.get("ingest_ts") or intent.get("created_at"),
        # Research
        research_signals=signals,
        research_status=evidence.get("research_status"),
        research_strongest_direction=strongest_dir,
        research_score=strongest_score,
        research_source=evidence.get("research_source"),
        research_tf=evidence.get("research_tf"),
        # Market
        regime=intent.get("regime"),
        market_quality_score=evidence.get("market_quality_score")
            or snapshot.get("market_quality_score"),
        spread_bps=(snapshot.get("spread_bps") or evidence.get("spread_bps")),
        # Gate
        seat_holder_at_post=intent.get("executor_holder_at_post"),
        governor_multiplier=(governor_block.get("multiplier")
            or intent.get("risk_multiplier")),
        gate_state=intent.get("gate_state"),
        dry_run_state=intent.get("dry_run_state"),
        blocked_by=list(intent.get("blocked_by") or []),
        executed=bool(intent.get("executed")),
        # Execution
        fill_price=(float(fill_price) if fill_price is not None else None),
        fill_qty=(float(fill_qty) if fill_qty is not None else None),
        fill_ts=fill_ts,
        slippage_bps=slippage_bps,
        # Position
        exit_price=(float(exit_price) if exit_price is not None else None),
        exit_ts=exit_ts,
        holding_period_sec=None,        # derived later from ts deltas
        mae_bps=mae_bps,
        mfe_bps=mfe_bps,
        pnl_bps=pnl_bps,
        pnl_usd=pnl_usd,
        # Verdict
        setup_id=classify_setup(intent.get("action") or "", signals),
        outcome=final_outcome,
        label_source=outcome_block.get("label_source"),
        # ─── Paradox v3 plan layer (Step 2) — lifted on read ────
        intent_version=intent.get("intent_version"),
        plan_stance=plan.get("stance"),
        plan_intent=plan.get("intent"),
        plan_setup=plan.get("setup"),
        plan_execution_style=plan.get("execution_style"),
        plan_size_posture=plan.get("size_posture"),
        plan_portfolio_posture=plan.get("portfolio_posture"),
        plan_confidence=(float(plan["confidence"]) if plan.get("confidence") is not None else None),
        plan_horizon=plan.get("horizon"),
        plan_trigger_price=(float(plan["trigger_price"]) if plan.get("trigger_price") is not None else None),
        plan_invalidation_price=(float(plan["invalidation_price"]) if plan.get("invalidation_price") is not None else None),
        plan_target_prices=list(plan.get("target_prices") or []) or None,
        plan_ttl_seconds=plan.get("ttl_seconds"),
        plan_setup_custom_tag=plan.get("setup_custom_tag"),
        plan_hedge_against_symbol=plan.get("hedge_against_symbol"),
        execution_action=execution.get("action"),
        execution_derived_from_plan=execution.get("derived_from_plan"),
    )


async def build_lessons_bulk(
    *,
    stack: Optional[str] = None,
    lane: Optional[str] = None,
    symbol: Optional[str] = None,
    setup_id: Optional[str] = None,
    outcome: Optional[str] = None,
    limit: int = 100,
) -> list[Lesson]:
    """Filter + build lessons in bulk. Implemented as N sequential
    `build_lesson` calls because (a) the dataset is small (~10k
    intents/day at peak), (b) each call has independent Mongo lookups
    and a pipeline of asyncio gather here would just amplify load on
    the bar source. Easy to swap to gather() later if a need arises.

    Filtering by `setup_id` or `outcome` requires a build pass because
    those fields are derived, not stored — we filter post-hoc rather
    than pushing into the Mongo query.
    """
    q: dict = {}
    if stack: q["stack"] = stack
    if lane:  q["lane"] = lane
    if symbol: q["symbol"] = symbol

    cursor = db[SHARED_INTENTS].find(q, {"_id": 0, "intent_id": 1}).sort("ingest_ts", -1)
    fetch_limit = max(limit * 3, 200) if (setup_id or outcome) else limit
    ids = await cursor.to_list(fetch_limit)

    out: list[Lesson] = []
    for row in ids:
        iid = row.get("intent_id")
        if not iid:
            continue
        lesson = await build_lesson(iid)
        if lesson is None:
            continue
        if setup_id and lesson.setup_id != setup_id:
            continue
        if outcome and lesson.outcome != outcome:
            continue
        out.append(lesson)
        if len(out) >= limit:
            break
    return out

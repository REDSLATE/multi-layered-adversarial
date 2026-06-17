"""Promotion Artifact Report — shadow-proposal vs. live-fill comparison.

DOCTRINE:
  A brain in a `challenger` (or other non-execute) seat gets every intent
  it emits silently downgraded to `shadow_proposal` (the gate chain refuses
  to route it). Over time, those shadow intents pile up uncorroborated.

  This module gives the operator a single piece of evidence to evaluate
  whether the shadow brain has earned promotion to a seat with
  `may_execute=True`. It compares the brain's shadow intents against the
  EXECUTING brain's (default: Alpha) realized fills on the same symbols
  during an overlapping window.

  Metrics emitted:
    * sample_size                 — number of shadow intents in window
    * directional_agreement_rate  — % of shadow intents where executor
                                    traded the same direction within
                                    DIRECTIONAL_AGREEMENT_WINDOW_MIN
    * hit_rate_mtm                — % of shadow intents whose MTM move
                                    over HIT_RATE_HORIZON_MIN matched
                                    the brain's proposed direction
    * simulated_pnl_usd           — sum of unit-size mark-to-market PnL
                                    assuming each shadow intent was
                                    executed at $1000 notional
    * realized_pnl_match_usd      — for the subset that had a directional
                                    match with the executor, sum of the
                                    executor's actual realized PnL on the
                                    same symbol within the window
    * verdict                     — `recommend_promote` / `insufficient_data`
                                    / `keep_in_challenger`

  Output is purely advisory — operators still countersign every promotion
  via the Patent J flow in `shared/promotion.py`. This is the upstream
  EVIDENCE that feeds that decision.

  This endpoint is READ-ONLY. It never mutates seats, authority, or roster.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from auth import get_current_user
from db import db
from namespaces import (
    SHARED_INTENTS,
    SHARED_OHLCV_BARS,
    EXECUTION_RECEIPTS,
    RUNTIMES,
)

router = APIRouter(prefix="/admin/promotion-artifact", tags=["promotion-artifact"])

# ─────────────────────────── Constants ────────────────────────────────

# Window (minutes) for "executor traded the same direction on the same
# symbol around when the shadow brain proposed it".
DIRECTIONAL_AGREEMENT_WINDOW_MIN: int = 60

# Horizon (minutes) for mark-to-market hit-rate scoring: did the price
# move the way the brain proposed within this window?
HIT_RATE_HORIZON_MIN: int = 60

# Unit notional used for simulated PnL (so brains with different
# `risk_multiplier` and `notional` proposals are all scored on equal
# footing).
SIMULATED_NOTIONAL_USD: float = 1000.0

# Minimum sample size before the verdict can recommend promotion.
MIN_SAMPLES_FOR_VERDICT: int = 20

# Promotion thresholds — these were tuned to 30% per operator directive
# (2026-02-18). Adjust upward as the doctrine matures.
PROMOTION_HIT_RATE_FLOOR: float = 0.30
PROMOTION_AGREEMENT_FLOOR: float = 0.30


# ─────────────────────────── Helpers ──────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(s: str | None) -> Optional[datetime]:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _action_to_direction(action: str | None) -> Optional[Literal["long", "short"]]:
    """Normalize an action string to a direction. None = neutral / unknown."""
    if not action:
        return None
    a = action.upper()
    if a in ("BUY", "ENTER_LONG", "SCALE_IN", "LONG"):
        return "long"
    if a in ("SELL", "EXIT", "SHORT", "ENTER_SHORT"):
        return "short"
    return None


async def _price_at(symbol: str, at_dt: datetime) -> Optional[float]:
    """Return the closest OHLCV close-price for `symbol` at or after `at_dt`.

    Looks up any tf — the most recent bar with `ts >= at_dt` wins. Falls
    back to the most recent bar at or before `at_dt` if no forward bar
    exists yet (the horizon hasn't elapsed).
    """
    at_iso = at_dt.isoformat()
    # Prefer first bar at or after `at_dt`
    doc = await db[SHARED_OHLCV_BARS].find_one(
        {"symbol": symbol, "ts": {"$gte": at_iso}},
        {"_id": 0, "c": 1, "ts": 1},
        sort=[("ts", 1)],
    )
    if doc and isinstance(doc.get("c"), (int, float)):
        return float(doc["c"])
    # Fallback: most recent bar before at_dt
    doc = await db[SHARED_OHLCV_BARS].find_one(
        {"symbol": symbol, "ts": {"$lte": at_iso}},
        {"_id": 0, "c": 1, "ts": 1},
        sort=[("ts", -1)],
    )
    if doc and isinstance(doc.get("c"), (int, float)):
        return float(doc["c"])
    return None


def _direction_signed_return(
    direction: Literal["long", "short"],
    entry: float,
    exit_: float,
) -> float:
    """Signed return for a unit position. long: (exit-entry)/entry.
    short: (entry-exit)/entry."""
    if entry <= 0:
        return 0.0
    raw = (exit_ - entry) / entry
    return raw if direction == "long" else -raw


def _verdict_from_metrics(
    samples: int,
    hit_rate: Optional[float],
    agreement: Optional[float],
) -> str:
    if samples < MIN_SAMPLES_FOR_VERDICT:
        return "insufficient_data"
    if hit_rate is None or agreement is None:
        return "insufficient_data"
    if hit_rate >= PROMOTION_HIT_RATE_FLOOR and agreement >= PROMOTION_AGREEMENT_FLOOR:
        return "recommend_promote"
    return "keep_in_challenger"


# ─────────────────────── Per-brain core ───────────────────────────────

async def _alpha_match_in_window(
    symbol: str,
    direction: Literal["long", "short"],
    around_dt: datetime,
    benchmark_brain: str,
) -> Optional[dict]:
    """Return the benchmark brain's execution receipt on `symbol` within
    ±DIRECTIONAL_AGREEMENT_WINDOW_MIN of `around_dt` that matches `direction`,
    or None.
    """
    lo = (around_dt - timedelta(minutes=DIRECTIONAL_AGREEMENT_WINDOW_MIN)).isoformat()
    hi = (around_dt + timedelta(minutes=DIRECTIONAL_AGREEMENT_WINDOW_MIN)).isoformat()
    cursor = db[EXECUTION_RECEIPTS].find(
        {
            "stack": benchmark_brain,
            "symbol": symbol,
            "executed_at": {"$gte": lo, "$lte": hi},
        },
        {"_id": 0, "action": 1, "side": 1, "notional_usd": 1, "filled_avg_price": 1, "executed_at": 1},
    )
    async for r in cursor:
        r_dir = _action_to_direction(r.get("action")) or _action_to_direction(r.get("side"))
        if r_dir == direction:
            return r
    return None


async def compute_brain_report(
    brain: str,
    hours: int = 24,
    benchmark_brain: str = "camino",
) -> dict:
    """Compute a PromotionArtifact report for one brain over `hours` window.

    Implements both PnL methodologies per operator request (2026-02-18):
      * Mark-to-Market simulated PnL (entry vs. price at horizon)
      * Realized-fill PnL for the subset that matched the benchmark's
        actual fill direction
    """
    if hours <= 0 or hours > 720:
        raise ValueError("hours must be between 1 and 720")

    end = _now()
    start = end - timedelta(hours=hours)

    # Pull this brain's shadow proposals in the window. A shadow proposal
    # is any intent the brain emitted while NOT holding the executor seat.
    cursor = db[SHARED_INTENTS].find(
        {
            "stack": brain,
            "ingest_ts": {"$gte": start.isoformat(), "$lte": end.isoformat()},
            "holds_executor_seat": False,
        },
        {
            "_id": 0,
            "intent_id": 1,
            "symbol": 1,
            "action": 1,
            "confidence": 1,
            "lane": 1,
            "ingest_ts": 1,
            "executor_holder_at_post": 1,
        },
    ).sort("ingest_ts", 1)

    intents = await cursor.to_list(length=10000)

    samples = len(intents)
    agreement_hits = 0
    hit_rate_hits = 0
    hit_rate_eligible = 0  # intents where we could resolve both entry + horizon prices
    simulated_pnl_usd = 0.0
    realized_pnl_match_usd = 0.0
    per_intent: list[dict] = []

    for it in intents:
        symbol = it.get("symbol")
        action = it.get("action")
        direction = _action_to_direction(action)
        ingest_dt = _parse_iso(it.get("ingest_ts"))
        row: dict = {
            "intent_id": it.get("intent_id"),
            "symbol": symbol,
            "action": action,
            "direction": direction,
            "confidence": it.get("confidence"),
            "lane": it.get("lane"),
            "ingest_ts": it.get("ingest_ts"),
            "executor_holder_at_post": it.get("executor_holder_at_post"),
            "alpha_match": None,
            "entry_price": None,
            "exit_price": None,
            "mtm_return_pct": None,
            "simulated_pnl_usd": None,
            "realized_pnl_match_usd": None,
        }

        if not symbol or not direction or ingest_dt is None:
            per_intent.append(row)
            continue

        # ── Directional agreement check
        match = await _alpha_match_in_window(symbol, direction, ingest_dt, benchmark_brain)
        if match is not None:
            agreement_hits += 1
            row["alpha_match"] = {
                "action": match.get("action"),
                "side": match.get("side"),
                "notional_usd": match.get("notional_usd"),
                "filled_avg_price": match.get("filled_avg_price"),
                "executed_at": match.get("executed_at"),
            }

        # ── Mark-to-Market simulation
        entry = await _price_at(symbol, ingest_dt)
        exit_dt = ingest_dt + timedelta(minutes=HIT_RATE_HORIZON_MIN)
        # Only score if horizon has elapsed (don't score open positions)
        if exit_dt <= end:
            exit_ = await _price_at(symbol, exit_dt)
        else:
            exit_ = None

        if entry is not None and exit_ is not None:
            hit_rate_eligible += 1
            ret = _direction_signed_return(direction, entry, exit_)
            pnl = SIMULATED_NOTIONAL_USD * ret
            simulated_pnl_usd += pnl
            if ret > 0:
                hit_rate_hits += 1
            row.update({
                "entry_price": entry,
                "exit_price": exit_,
                "mtm_return_pct": round(ret * 100.0, 4),
                "simulated_pnl_usd": round(pnl, 2),
            })

        # ── Realized-fill PnL contribution (executor's actual fill in
        # the matched direction). We don't have the executor's exit yet,
        # so we approximate using the same horizon close price.
        if match is not None and entry is not None and exit_ is not None:
            executor_entry = match.get("filled_avg_price")
            if isinstance(executor_entry, (int, float)) and executor_entry > 0:
                exec_ret = _direction_signed_return(direction, float(executor_entry), exit_)
                exec_notional = match.get("notional_usd") or SIMULATED_NOTIONAL_USD
                if isinstance(exec_notional, (int, float)):
                    exec_pnl = float(exec_notional) * exec_ret
                    realized_pnl_match_usd += exec_pnl
                    row["realized_pnl_match_usd"] = round(exec_pnl, 2)

        per_intent.append(row)

    directional_agreement_rate = (agreement_hits / samples) if samples else None
    hit_rate_mtm = (hit_rate_hits / hit_rate_eligible) if hit_rate_eligible else None

    verdict = _verdict_from_metrics(samples, hit_rate_mtm, directional_agreement_rate)

    return {
        "brain": brain,
        "benchmark_brain": benchmark_brain,
        "window": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "hours": hours,
        },
        "thresholds": {
            "min_samples": MIN_SAMPLES_FOR_VERDICT,
            "hit_rate_floor": PROMOTION_HIT_RATE_FLOOR,
            "agreement_floor": PROMOTION_AGREEMENT_FLOOR,
            "directional_agreement_window_min": DIRECTIONAL_AGREEMENT_WINDOW_MIN,
            "hit_rate_horizon_min": HIT_RATE_HORIZON_MIN,
            "simulated_notional_usd": SIMULATED_NOTIONAL_USD,
        },
        "metrics": {
            "sample_size": samples,
            "directional_agreement_rate": directional_agreement_rate,
            "directional_agreement_hits": agreement_hits,
            "hit_rate_mtm": hit_rate_mtm,
            "hit_rate_eligible": hit_rate_eligible,
            "hit_rate_hits": hit_rate_hits,
            "simulated_pnl_usd": round(simulated_pnl_usd, 2),
            "realized_pnl_match_usd": round(realized_pnl_match_usd, 2),
        },
        "verdict": verdict,
        "verdict_rationale": _rationale(verdict, samples, hit_rate_mtm, directional_agreement_rate),
        "per_intent": per_intent,
        "generated_at": end.isoformat(),
        "report_version": "promotion_artifact_v1_shadow_vs_fill",
    }


def _rationale(
    verdict: str,
    samples: int,
    hit_rate: Optional[float],
    agreement: Optional[float],
) -> str:
    if verdict == "insufficient_data":
        if samples < MIN_SAMPLES_FOR_VERDICT:
            return (
                f"only {samples} shadow proposals in window — need at least "
                f"{MIN_SAMPLES_FOR_VERDICT} for a verdict"
            )
        return "no resolvable hit-rate or agreement (missing price/fill data)"
    if verdict == "recommend_promote":
        return (
            f"hit_rate={hit_rate:.0%} ≥ {PROMOTION_HIT_RATE_FLOOR:.0%} AND "
            f"agreement={agreement:.0%} ≥ {PROMOTION_AGREEMENT_FLOOR:.0%} "
            f"across {samples} shadow proposals — evidence supports promotion"
        )
    # keep_in_challenger
    parts = []
    if hit_rate is not None and hit_rate < PROMOTION_HIT_RATE_FLOOR:
        parts.append(f"hit_rate={hit_rate:.0%} below {PROMOTION_HIT_RATE_FLOOR:.0%} floor")
    if agreement is not None and agreement < PROMOTION_AGREEMENT_FLOOR:
        parts.append(f"agreement={agreement:.0%} below {PROMOTION_AGREEMENT_FLOOR:.0%} floor")
    return "; ".join(parts) or "below promotion floors"


# ─────────────────────────── HTTP API ─────────────────────────────────

class BrainReportResponse(BaseModel):
    brain: str
    benchmark_brain: str
    window: dict
    thresholds: dict
    metrics: dict
    verdict: str
    verdict_rationale: str
    per_intent: list
    generated_at: str
    report_version: str


@router.get("/{brain}", response_model=BrainReportResponse)
async def get_promotion_artifact(
    brain: str,
    hours: int = Query(24, ge=1, le=720),
    benchmark_brain: str = Query("camino"),
    _user: dict = Depends(get_current_user),
):
    if brain not in RUNTIMES:
        raise HTTPException(status_code=404, detail=f"unknown brain: {brain}")
    if benchmark_brain not in RUNTIMES:
        raise HTTPException(status_code=400, detail=f"unknown benchmark_brain: {benchmark_brain}")
    if brain == benchmark_brain:
        raise HTTPException(status_code=400, detail="brain must differ from benchmark_brain")
    return await compute_brain_report(brain=brain, hours=hours, benchmark_brain=benchmark_brain)


@router.get("")
async def get_promotion_artifact_all(
    hours: int = Query(24, ge=1, le=720),
    benchmark_brain: str = Query("camino"),
    _user: dict = Depends(get_current_user),
):
    """All-brains scan against the benchmark brain. Used by the dashboard
    Promotion-Evidence panel.

    Doctrine (2026-02-18): brains currently holding GOVERNOR authority
    are excluded from the report. Governor is off-ladder: it's a
    terminal seat that cannot be promoted to a trading authority
    (mirrors `promote_brain`'s explicit refusal at line 176). Chevelle
    holds the equity AND crypto governor seats; evaluating it as a
    promotion candidate against Alpha's fills is a category error and
    confused the operator UI by inflating the "brain reports" count.
    """
    if benchmark_brain not in RUNTIMES:
        raise HTTPException(status_code=400, detail=f"unknown benchmark_brain: {benchmark_brain}")
    # Pull authority states once; cheap enough at 4 brains.
    from shared.promotion import _current_state  # noqa: WPS433
    excluded_governors: list[str] = []
    reports = []
    for rt in RUNTIMES:
        if rt == benchmark_brain:
            continue
        state = await _current_state(rt)
        if (state or {}).get("authority_state") == "governor":
            excluded_governors.append(rt)
            continue
        reports.append(await compute_brain_report(brain=rt, hours=hours, benchmark_brain=benchmark_brain))
    return {
        "benchmark_brain": benchmark_brain,
        "hours": hours,
        "reports": reports,
        "excluded_governors": excluded_governors,
        "generated_at": _now().isoformat(),
        "report_version": "promotion_artifact_v1_shadow_vs_fill",
    }

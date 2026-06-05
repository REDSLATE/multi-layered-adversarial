"""Confidence-floor calibration sweep — read-only diagnostic.

Doctrine:
    OBSERVATION, NOT ENFORCEMENT. This endpoint reports what WOULD
    pass at each candidate confidence floor over a historical window.
    It NEVER changes the production floor (which is read from
    `RISEDUAL_{LANE}_CONFIDENCE_FLOOR` env vars and lives in the
    council policy tables). Calibrate here, decide there.

    HOLD INVARIANT (load-bearing):
        DIRECTIONAL = {"BUY", "SELL", "SHORT", "COVER"}
        if action not in DIRECTIONAL: never counted toward any floor.

    No matter how low the floor sweep goes (including 0.00), a HOLD
    can never be reported as "passing." HOLD is not a trade.

Survivor bias:
    Rows with `gate_state == "rejected_at_ingest"` are EXCLUDED from
    pass counts (they never reached the executor regardless of floor)
    but counted in the `rejected_at_ingest` summary so the operator
    sees what fraction of intents are filtered before they ever land.
    This gives a more honest distribution than "what would have
    passed among the survivors."

Outcome join:
    `shared_brain_outcomes` is keyed by `opinion_id`, not `intent_id`,
    so there is no exact join. We approximate: an outcome matches an
    intent iff `(runtime == stack)` AND `(topic.endswith(":" + symbol))`
    AND `resolved_at` is within 24h after `ingest_ts`. Reported as
    `outcome_join: "approximate_by_brain_symbol_and_24h_window"` so
    the operator never mistakes this for a forensic-grade attribution.

Endpoint:
    GET /api/admin/calibration/confidence-floor-sweep
        ?lane=crypto|equity          (optional; default: both)
        ?hours=168                    (optional; default: 168 / 7d)
        ?floors=0.00,0.10,0.20,...    (optional; default: stock sweep)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query

from auth import get_current_user
from db import db
from namespaces import SHARED_INTENTS, SHARED_OUTCOMES


router = APIRouter(prefix="/admin/calibration", tags=["calibration"])


# ────────────────────── doctrine constants ────────────────────────────


DIRECTIONAL_ACTIONS: frozenset[str] = frozenset({"BUY", "SELL", "SHORT", "COVER"})
"""Load-bearing invariant. HOLD is never directional; never passes any floor."""

DEFAULT_SWEEP: tuple[float, ...] = (0.00, 0.10, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45)


# ────────────────────── helpers ───────────────────────────────────────


def _parse_floors(raw: Optional[str]) -> List[float]:
    """`"0.00,0.20,0.40"` → `[0.00, 0.20, 0.40]`. Empty / malformed →
    DEFAULT_SWEEP. Negative / >1.0 silently clamped out."""
    if not raw:
        return list(DEFAULT_SWEEP)
    out: List[float] = []
    for tok in raw.split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            f = float(tok)
        except ValueError:
            continue
        if 0.0 <= f <= 1.0:
            out.append(round(f, 4))
    return out or list(DEFAULT_SWEEP)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(s: Any) -> Optional[datetime]:
    if not s:
        return None
    if isinstance(s, datetime):
        return s if s.tzinfo else s.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _conf(intent: Dict[str, Any], field: str) -> Optional[float]:
    """Read a confidence field; tolerate `evidence.<field>` fallback
    (some Camaro v1 rows put `raw_confidence` under evidence)."""
    v = intent.get(field)
    if v is None:
        ev = intent.get("evidence") or {}
        v = ev.get(field) if isinstance(ev, dict) else None
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _direction(intent: Dict[str, Any]) -> str:
    return str(intent.get("action") or intent.get("direction") or "").upper()


# ────────────────────── outcome approximate join ──────────────────────


async def _build_outcome_index(
    window_start: datetime,
    lane_filter: Optional[str],
) -> Dict[tuple, List[Dict[str, Any]]]:
    """Index outcomes by `(runtime, symbol_from_topic)` so we can do a
    per-intent lookup without scanning the whole outcomes collection.

    `topic` shape on outcomes is e.g. "symbol:TSLA"; we strip the
    namespace prefix. Outcomes without a `symbol:` topic are ignored.

    `lane_filter` is unused for outcomes (they aren't lane-tagged) but
    accepted for API symmetry.
    """
    del lane_filter  # outcomes are not lane-tagged; symmetry only
    cutoff_iso = window_start.isoformat()
    cursor = db[SHARED_OUTCOMES].find(
        {"resolved_at": {"$gte": cutoff_iso}},
        {"_id": 0, "runtime": 1, "topic": 1, "actual": 1, "resolved_at": 1},
    )

    idx: Dict[tuple, List[Dict[str, Any]]] = {}
    async for o in cursor:
        topic = str(o.get("topic") or "")
        if not topic.startswith("symbol:"):
            continue
        sym = topic.split(":", 1)[1].strip().upper()
        runtime = str(o.get("runtime") or "").lower()
        if not sym or not runtime:
            continue
        key = (runtime, sym)
        idx.setdefault(key, []).append(o)
    return idx


def _match_outcome(
    intent: Dict[str, Any],
    outcome_idx: Dict[tuple, List[Dict[str, Any]]],
) -> Optional[str]:
    """Return 'win' / 'loss' / 'breakeven' / None.

    Approximate: matches if same (stack, symbol) and outcome's
    resolved_at is within 24h AFTER the intent's ingest_ts.
    """
    stack = str(intent.get("stack") or "").lower()
    symbol = str(intent.get("symbol") or "").upper()
    if not stack or not symbol:
        return None
    candidates = outcome_idx.get((stack, symbol))
    if not candidates:
        return None

    ingest_ts = _parse_ts(intent.get("ingest_ts"))
    if not ingest_ts:
        return None

    window_end = ingest_ts + timedelta(hours=24)
    for o in candidates:
        resolved = _parse_ts(o.get("resolved_at"))
        if not resolved:
            continue
        if ingest_ts <= resolved <= window_end:
            return str(o.get("actual") or "").lower()
    return None


# ────────────────────── core sweep ────────────────────────────────────


def _empty_bucket(floor: float) -> Dict[str, Any]:
    return {
        "floor": round(floor, 4),
        "raw_pass": 0,
        "effective_pass": 0,
        "dampener_drop": 0,
        # Counted ONLY on rows that have BOTH raw and effective populated;
        # see `paired_rows` below. Legacy rows missing one are surfaced
        # separately at the response top via `data_quality`.
        "paired_raw_pass": 0,
        "paired_effective_pass": 0,
        "resolved": 0,
        "wins": 0,
        "losses": 0,
        "win_rate": None,
        "pnl_usd": None,  # outcomes don't carry PnL; reserved for future
    }


def _sweep_rows(
    rows: List[Dict[str, Any]],
    floors: List[float],
    outcome_idx: Dict[tuple, List[Dict[str, Any]]],
) -> Dict[str, Any]:
    """Run the sweep. Returns dict with:
        - `floors`: per-floor buckets
        - `data_quality`: row-level data integrity counts (missing
          raw/effective, outcome match rate)
    """
    buckets = [_empty_bucket(f) for f in floors]

    # Data-quality counters surfaced separately so they don't pollute
    # the floor accounting.
    dq = {
        "directional_executable": 0,  # the population the sweep operates on
        "missing_raw_confidence": 0,
        "missing_effective_confidence": 0,
        "missing_both": 0,
        "outcome_matched": 0,
    }

    for r in rows:
        # HOLD invariant — never passes any floor. Skip entirely.
        if _direction(r) not in DIRECTIONAL_ACTIONS:
            continue
        # Rejected at ingest never reached executor → not a "would have
        # passed" candidate. Summarized separately at the response top.
        if str(r.get("gate_state") or "") == "rejected_at_ingest":
            continue

        raw = _conf(r, "raw_confidence")
        eff = _conf(r, "confidence")

        if raw is None and eff is None:
            dq["missing_both"] += 1
            continue

        dq["directional_executable"] += 1
        if raw is None:
            dq["missing_raw_confidence"] += 1
        if eff is None:
            dq["missing_effective_confidence"] += 1

        paired = (raw is not None) and (eff is not None)
        outcome = _match_outcome(r, outcome_idx)
        if outcome in ("win", "loss", "breakeven"):
            dq["outcome_matched"] += 1

        for b in buckets:
            floor = b["floor"]
            raw_pass = raw is not None and raw >= floor
            eff_pass = eff is not None and eff >= floor

            if raw_pass:
                b["raw_pass"] += 1
            if eff_pass:
                b["effective_pass"] += 1

            # Paired counters: only count rows where BOTH raw and
            # effective exist. Dampener_drop is derived from these so
            # legacy rows missing one side don't produce negative drops.
            if paired:
                if raw_pass:
                    b["paired_raw_pass"] += 1
                if eff_pass:
                    b["paired_effective_pass"] += 1

            # Outcome counts only on rows that actually would have
            # passed at the EFFECTIVE confidence floor (since that's
            # what production filters on).
            if eff_pass and outcome in ("win", "loss", "breakeven"):
                b["resolved"] += 1
                if outcome == "win":
                    b["wins"] += 1
                elif outcome == "loss":
                    b["losses"] += 1

    # Derived fields after the pass
    for b in buckets:
        # Dampener drop computed ONLY on paired rows so we never produce
        # negative values from legacy data-shape mismatch. Clamped at 0
        # because the operator-visible invariant is "raw ≥ effective" —
        # a noisy intent where the dampener bumped effective slightly
        # higher than raw (rounding / late re-score) is data noise, not
        # a real negative drop.
        b["dampener_drop"] = max(0, b["paired_raw_pass"] - b["paired_effective_pass"])
        if b["resolved"] > 0:
            b["win_rate"] = round(b["wins"] / b["resolved"], 4)

    return {"floors": buckets, "data_quality": dq}


# ────────────────────── endpoint ──────────────────────────────────────


@router.get("/confidence-floor-sweep")
async def confidence_floor_sweep(
    lane: Optional[str] = Query(default=None, pattern="^(crypto|equity)$"),
    hours: int = Query(default=168, ge=1, le=24 * 90),
    floors: Optional[str] = Query(default=None),
    _user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Read-only sweep of candidate confidence floors over a window.

    Returns the per-floor row count, outcome stats (approximate),
    survivor-bias context (how many intents were rejected at ingest),
    and a per-brain breakdown. NEVER changes production behavior.
    """
    window_end = _now()
    window_start = window_end - timedelta(hours=hours)
    floor_list = _parse_floors(floors)

    q: Dict[str, Any] = {"ingest_ts": {"$gte": window_start.isoformat()}}
    if lane:
        q["lane"] = lane

    projection = {
        "_id": 0,
        "stack": 1,
        "lane": 1,
        "symbol": 1,
        "action": 1,
        "direction": 1,
        "confidence": 1,
        "raw_confidence": 1,
        "evidence": 1,
        "gate_state": 1,
        "ingest_ts": 1,
    }
    rows: List[Dict[str, Any]] = await db[SHARED_INTENTS].find(q, projection).to_list(50000)

    # Population summary (before any HOLD / rejected filtering)
    total = len(rows)
    direction_counts = {"directional": 0, "hold": 0, "other": 0}
    rejected_at_ingest = 0
    for r in rows:
        d = _direction(r)
        if d in DIRECTIONAL_ACTIONS:
            direction_counts["directional"] += 1
        elif d == "HOLD":
            direction_counts["hold"] += 1
        else:
            direction_counts["other"] += 1
        if str(r.get("gate_state") or "") == "rejected_at_ingest":
            rejected_at_ingest += 1

    # Outcome index for the approximate join
    outcome_idx = await _build_outcome_index(window_start, lane)

    # Aggregate sweep
    aggregate_result = _sweep_rows(rows, floor_list, outcome_idx)
    aggregate_floors = aggregate_result["floors"]
    aggregate_dq = aggregate_result["data_quality"]

    # Per-brain breakdown — same sweep applied to each stack subset
    by_brain: Dict[str, List[Dict[str, Any]]] = {}
    by_brain_stacks = sorted({str(r.get("stack") or "").lower() for r in rows if r.get("stack")})
    for stack in by_brain_stacks:
        subset = [r for r in rows if str(r.get("stack") or "").lower() == stack]
        by_brain[stack] = _sweep_rows(subset, floor_list, outcome_idx)["floors"]

    # Diagnostic: does ANY floor in the sweep actually cut data?
    # If raw_pass + effective_pass are identical across every floor,
    # the floor isn't biting in this window — the operator should
    # know that BEFORE picking a "balanced" floor from a flat curve.
    raw_pass_set = {b["raw_pass"] for b in aggregate_floors}
    eff_pass_set = {b["effective_pass"] for b in aggregate_floors}
    floor_bites = (len(raw_pass_set) > 1) or (len(eff_pass_set) > 1)

    return {
        "lane": lane or "all",
        "hours": hours,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "directional_actions": sorted(DIRECTIONAL_ACTIONS),
        "total_intents_in_window": total,
        "total_directional": direction_counts["directional"],
        "total_hold": direction_counts["hold"],
        "total_other_action": direction_counts["other"],
        "rejected_at_ingest": rejected_at_ingest,
        "floor_bites": floor_bites,
        "data_quality": aggregate_dq,
        "floors": aggregate_floors,
        "by_brain": by_brain,
        "outcome_join": "approximate_by_brain_symbol_and_24h_window",
        "notes": [
            "HOLD and non-directional actions are EXCLUDED from every floor "
            "regardless of confidence (load-bearing doctrine invariant).",
            "`raw_pass` counts directional intents whose `raw_confidence` ≥ floor.",
            "`effective_pass` counts directional intents whose post-dampener "
            "`confidence` ≥ floor. This is what the executor compares against.",
            "`dampener_drop = paired_raw_pass - paired_effective_pass` — computed "
            "ONLY on rows that have BOTH raw and effective populated. Legacy "
            "rows missing one side are surfaced in `data_quality` instead, "
            "so dampener_drop can never go negative.",
            "Outcome stats (`resolved`, `wins`, `losses`, `win_rate`) are joined "
            "on `effective_pass` rows only (matching production filter shape).",
            "Outcome join is APPROXIMATE: (stack, symbol) match within 24h of "
            "ingest. See `data_quality.outcome_matched` for the join match rate "
            "— interpret outcome stats with sample-size awareness.",
            "`floor_bites=false` means the sweep range did not cut any intents "
            "in this window — every floor admits the same rows. The confidence "
            "floor is NOT the binding constraint; check spread/governor gates.",
            "Rows with `gate_state == rejected_at_ingest` are excluded from all "
            "pass counts (they never reached the executor) but summarized at "
            "the response top to surface survivor bias.",
            "This endpoint is READ-ONLY. Production floor is set via "
            "`RISEDUAL_{LANE}_CONFIDENCE_FLOOR` env vars and is unaffected.",
        ],
    }

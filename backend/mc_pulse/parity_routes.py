"""Camino parity — pulse-emitted opinions vs runner-emitted intents.

Migration step 4 (design freeze `MC_PULSE.md` §10). Read-only:
this endpoint never writes, never advances state. It exists to
answer "is the pulse doing what the runner does?" honestly, and
to gate step 6 (arbiter flip) + step 7 (runner shutdown).

Metrics emitted:
    * Action distribution — how often each path emits BUY/SELL/
      HOLD/(no-op). Divergence here is the loudest signal that
      the two paths are seeing different market truth.
    * Confidence distribution — mean + std per path. Systematic
      shift indicates the personality multiplier or the rank-input
      mapping is off.
    * Rationale-token overlap — proxy for `reason_codes` overlap
      until the legacy core exposes reason_codes directly. Uses
      a Jaccard-index-esque comparison of tokenized rationales.
    * Timestamp drift — median delay between the runner's intent
      insertion and the closest pulse envelope on the same
      (symbol, direction).
    * Sample rows — last 20 paired observations for eyeballing.

Bounded reads (max_time_ms) and fail-soft — the endpoint MUST
NOT stall on Atlas slowness.
"""
from __future__ import annotations

import logging
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from statistics import mean, median, pstdev
from typing import Optional

from fastapi import APIRouter, Depends, Query

from auth import get_current_user
from db import db

logger = logging.getLogger("mc_pulse.parity")

router = APIRouter(prefix="/mc/parity", tags=["mc-parity"])

# Legacy intent path collection.
SHARED_INTENTS = "shared_intents"
# Pulse comparison collection.
MC_OPINIONS_COMPARE = "mc_opinions_compare"


def _tokenize(s: Optional[str]) -> set[str]:
    """Lowercase tokens 3+ chars long — good enough for
    rationale-token Jaccard until reason_codes lands."""
    if not s:
        return set()
    return {t for t in re.findall(r"[a-z0-9_]{3,}", s.lower())}


def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    union = a | b
    if not union:
        return 1.0
    return len(a & b) / len(union)


def _pulse_direction_to_intent_action(direction: str) -> Optional[str]:
    """Map pulse `direction` (LONG/SHORT/FLAT) to the runner's
    `action` vocabulary (BUY/SELL/HOLD) for apples-to-apples
    action-rate comparison."""
    d = (direction or "").upper()
    return {"LONG": "BUY", "SHORT": "SELL", "FLAT": "HOLD"}.get(d)


@router.get("/{brain_id}")
async def parity_report(
    brain_id: str,
    hours: int = Query(24, ge=1, le=168),
    sample_size: int = Query(20, ge=0, le=100),
    user: dict = Depends(get_current_user),
):
    """Parity comparison over the last `hours` hours.

    A `hours=24` window is the working default (one trading
    session). Widen to 72–168 during weekends when volume is low.
    """
    return await compute_parity(brain_id, hours=hours, sample_size=sample_size)


async def compute_parity(
    brain_id: str, *, hours: int = 24, sample_size: int = 20,
) -> dict:
    """Auth-free parity computation used by both the HTTP endpoint
    and the background snapshotter. Kept pure — no side effects,
    no writes. Bounded reads via `_safe_find`."""
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    brain_lc = brain_id.strip().lower()

    # Pull both tapes with bounded reads. Failure returns empty
    # tape (fail-soft) rather than a red banner.
    pulse_rows = await _safe_find(
        MC_OPINIONS_COMPARE,
        {"brain": brain_lc, "evaluated_at": {"$gte": since}},
    )
    intent_rows = await _safe_find(
        SHARED_INTENTS,
        {"stack": brain_lc, "ingest_ts": {"$gte": since}},
    )

    return {
        "brain": brain_lc,
        "window_hours": hours,
        "since": since,
        "pulse_count": len(pulse_rows),
        "runner_count": len(intent_rows),
        "action_distribution": _action_distribution(pulse_rows, intent_rows),
        "confidence_distribution": _confidence_distribution(pulse_rows, intent_rows),
        "rationale_token_overlap": _rationale_overlap(pulse_rows, intent_rows),
        "timestamp_drift_s": _timestamp_drift(pulse_rows, intent_rows),
        "samples": _sample_pairs(pulse_rows, intent_rows, sample_size),
    }


# ── 2026-07-12 doctrine: parity trend snapshotter ──────────────────
# Every N minutes (default 15), snapshot the parity metrics for
# each configured brain and persist a compact row to
# `mc_parity_snapshots`. Enables trend observation over 24-72h+
# without ANY manual polling. The gate criteria for the arbiter
# flip (match_score ≥ 0.60, pulse.confidence.std > 0.02,
# timestamp_drift.pairs_matched ≥ 20) can be read off the trend
# directly. Sample_size intentionally 0 in snapshots — we only
# care about the aggregate metrics, not paired examples.
MC_PARITY_SNAPSHOTS = "mc_parity_snapshots"

# Which brains to snapshot. All 4 pulse brains live post-P2 step 2.
# Add newcomers here as their pulse adapters ship.
PARITY_SNAPSHOT_BRAINS = ["camino", "gto", "barracuda", "hellcat"]


async def take_parity_snapshot(brain_id: str, *, hours: int = 24) -> dict:
    """Compute parity for `brain_id` and write ONE compact snapshot
    row to `mc_parity_snapshots`. Returns the persisted doc.
    Fail-soft: any exception is logged; None returned so caller can
    keep looping.
    """
    try:
        parity = await compute_parity(brain_id, hours=hours, sample_size=0)
        conf_pulse = parity.get("confidence_distribution", {}).get("pulse", {})
        drift = parity.get("timestamp_drift_s", {}) or {}
        action_dist = parity.get("action_distribution", {}) or {}
        # Gate criteria from MC_PULSE.md §11 — arbiter can only flip
        # to compare_only=False once ALL THREE hold simultaneously.
        match_score = float(action_dist.get("match_score") or 0.0)
        conf_std = float(conf_pulse.get("std") or 0.0)
        pairs_matched = int(drift.get("pairs_matched") or 0)
        gates_ok = (
            match_score >= 0.60
            and conf_std > 0.02
            and pairs_matched >= 20
        )
        doc = {
            "at": datetime.now(timezone.utc).isoformat(),
            "brain": brain_id.strip().lower(),
            "window_hours": hours,
            "pulse_count": parity.get("pulse_count", 0),
            "runner_count": parity.get("runner_count", 0),
            "match_score": match_score,
            "pulse_confidence_mean": float(conf_pulse.get("mean") or 0.0),
            "pulse_confidence_std": conf_std,
            "runner_confidence_mean": float(
                parity.get("confidence_distribution", {})
                .get("runner", {}).get("mean") or 0.0
            ),
            "timestamp_drift_median_s": float(drift.get("median_s") or 0.0),
            "pairs_matched": pairs_matched,
            "rationale_jaccard_mean": float(
                (parity.get("rationale_token_overlap") or {})
                .get("jaccard_mean") or 0.0
            ),
            "arbiter_flip_gates_pass": gates_ok,
            "gates": {
                "match_score_ok": match_score >= 0.60,
                "conf_std_ok": conf_std > 0.02,
                "pairs_matched_ok": pairs_matched >= 20,
            },
        }
        await db[MC_PARITY_SNAPSHOTS].insert_one(dict(doc))
        return doc
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "take_parity_snapshot failed brain=%s: %s", brain_id, exc,
        )
        return {}


@router.get("/{brain_id}/history")
async def parity_history(
    brain_id: str,
    limit: int = Query(96, ge=1, le=672),  # 96 x 15min = 24h; 672 = 7d
    user: dict = Depends(get_current_user),
):
    """Rolling parity trend for the given brain. Read-only.
    Newest first. Used by the operator dashboard to plot
    match_score/conf_std/pairs_matched over time and see when the
    arbiter-flip gates first hold.
    """
    brain_lc = brain_id.strip().lower()
    rows = await _safe_find(
        MC_PARITY_SNAPSHOTS, {"brain": brain_lc},
    )
    # _safe_find has no sort; sort in-memory (bounded).
    rows.sort(key=lambda r: r.get("at", ""), reverse=True)
    rows = rows[:limit]
    # Strip Mongo _id for cleanliness.
    for r in rows:
        r.pop("_id", None)
    return {
        "brain": brain_lc,
        "count": len(rows),
        "snapshots": rows,
    }


async def _safe_find(collection: str, query: dict) -> list[dict]:
    try:
        cursor = db[collection].find(query, {"_id": 0}).max_time_ms(2500).limit(5000)
        return await cursor.to_list(5000)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "parity %s read failed: %s (returning empty)", collection, exc,
        )
        return []


def _action_distribution(pulse: list[dict], runner: list[dict]) -> dict:
    """Compare rate of each action per path. Convert pulse
    directions to runner-action vocabulary so the comparison is
    honest."""
    pulse_actions: Counter[str] = Counter()
    for r in pulse:
        a = _pulse_direction_to_intent_action(r.get("direction", ""))
        if a:
            pulse_actions[a] += 1
    runner_actions: Counter[str] = Counter(
        (r.get("action") or "").upper() for r in runner if r.get("action")
    )
    all_actions = sorted(set(pulse_actions) | set(runner_actions))
    total_pulse = sum(pulse_actions.values()) or 1
    total_runner = sum(runner_actions.values()) or 1
    return {
        "pulse": {a: pulse_actions.get(a, 0) for a in all_actions},
        "runner": {a: runner_actions.get(a, 0) for a in all_actions},
        "pulse_pct": {
            a: round(pulse_actions.get(a, 0) / total_pulse * 100, 2)
            for a in all_actions
        },
        "runner_pct": {
            a: round(runner_actions.get(a, 0) / total_runner * 100, 2)
            for a in all_actions
        },
        # Match score: 1.0 means identical distributions.
        "match_score": _distribution_match(
            pulse_actions, runner_actions, all_actions,
        ),
    }


def _distribution_match(pa: Counter, ra: Counter, keys: list[str]) -> float:
    """1.0 - half the L1 distance between the two normalized
    distributions. 1.0 = identical, 0.0 = fully disjoint."""
    tp = sum(pa.values()) or 1
    tr = sum(ra.values()) or 1
    l1 = sum(abs(pa.get(k, 0) / tp - ra.get(k, 0) / tr) for k in keys)
    return round(1.0 - l1 / 2.0, 3)


def _confidence_distribution(pulse: list[dict], runner: list[dict]) -> dict:
    def _stats(vals: list[float]) -> dict:
        if not vals:
            return {"n": 0, "mean": None, "std": None, "min": None, "max": None}
        return {
            "n": len(vals),
            "mean": round(mean(vals), 4),
            "std": round(pstdev(vals), 4) if len(vals) > 1 else 0.0,
            "min": round(min(vals), 4),
            "max": round(max(vals), 4),
        }
    p_vals = [float(r["confidence"]) for r in pulse if r.get("confidence") is not None]
    r_vals = [float(r["confidence"]) for r in runner if r.get("confidence") is not None]
    return {"pulse": _stats(p_vals), "runner": _stats(r_vals)}


def _rationale_overlap(pulse: list[dict], runner: list[dict]) -> dict:
    """Mean pairwise Jaccard between pulse rationales and the
    NEAREST runner rationale (same symbol, ≤5 min ts drift).
    Serves as a stand-in for reason_codes overlap until legacy
    core exposes them."""
    if not pulse or not runner:
        return {"jaccard_mean": None, "pairs_matched": 0}
    # Bucket runner rows by symbol for fast pairing.
    by_symbol: dict[str, list[dict]] = defaultdict(list)
    for r in runner:
        by_symbol[(r.get("symbol") or "").upper()].append(r)

    jaccards: list[float] = []
    for p in pulse:
        sym = (p.get("symbol") or "").upper()
        candidates = by_symbol.get(sym, [])
        if not candidates:
            continue
        p_ts = _parse_ts(p.get("evaluated_at") or p.get("ts"))
        if p_ts is None:
            continue
        # Nearest runner intent by |Δt|, within 5 min.
        best = None
        best_gap = 999_999
        for r in candidates:
            r_ts = _parse_ts(r.get("ingest_ts") or r.get("ts"))
            if r_ts is None:
                continue
            gap = abs((r_ts - p_ts).total_seconds())
            if gap < best_gap and gap <= 300:
                best_gap = gap
                best = r
        if best is None:
            continue
        pt = _tokenize(p.get("rationale"))
        rt = _tokenize(best.get("rationale"))
        jaccards.append(_jaccard(pt, rt))
    if not jaccards:
        return {"jaccard_mean": None, "pairs_matched": 0}
    return {
        "jaccard_mean": round(mean(jaccards), 3),
        "pairs_matched": len(jaccards),
    }


def _timestamp_drift(pulse: list[dict], runner: list[dict]) -> dict:
    """Median |Δt| between each pulse envelope and its nearest
    same-symbol runner intent within 5 min."""
    if not pulse or not runner:
        return {"median_s": None, "pairs_matched": 0}
    by_symbol: dict[str, list[datetime]] = defaultdict(list)
    for r in runner:
        ts = _parse_ts(r.get("ingest_ts") or r.get("ts"))
        if ts is not None:
            by_symbol[(r.get("symbol") or "").upper()].append(ts)

    gaps: list[float] = []
    for p in pulse:
        sym = (p.get("symbol") or "").upper()
        p_ts = _parse_ts(p.get("evaluated_at") or p.get("ts"))
        if p_ts is None:
            continue
        candidates = by_symbol.get(sym, [])
        if not candidates:
            continue
        best_gap = min(
            (abs((rt - p_ts).total_seconds()) for rt in candidates),
            default=None,
        )
        if best_gap is not None and best_gap <= 300:
            gaps.append(best_gap)
    if not gaps:
        return {"median_s": None, "pairs_matched": 0}
    return {"median_s": round(median(gaps), 2), "pairs_matched": len(gaps)}


def _sample_pairs(pulse: list[dict], runner: list[dict], n: int) -> list[dict]:
    """Last `n` pulse rows with their nearest-runner-match (if any)
    for eyeball review. Small — meant for a dashboard table, not
    a full audit dump."""
    if n <= 0:
        return []
    pulse_sorted = sorted(
        pulse, key=lambda r: r.get("evaluated_at") or "", reverse=True,
    )[:n]
    by_symbol: dict[str, list[dict]] = defaultdict(list)
    for r in runner:
        by_symbol[(r.get("symbol") or "").upper()].append(r)
    out = []
    for p in pulse_sorted:
        sym = (p.get("symbol") or "").upper()
        p_ts = _parse_ts(p.get("evaluated_at"))
        near = None
        if p_ts is not None:
            for r in by_symbol.get(sym, []):
                r_ts = _parse_ts(r.get("ingest_ts"))
                if r_ts is None:
                    continue
                if abs((r_ts - p_ts).total_seconds()) <= 300:
                    near = r
                    break
        out.append({
            "symbol": sym,
            "pulse_direction": p.get("direction"),
            "pulse_action": _pulse_direction_to_intent_action(p.get("direction", "")),
            "pulse_confidence": p.get("confidence"),
            "pulse_evaluated_at": p.get("evaluated_at"),
            "runner_action": (near or {}).get("action") if near else None,
            "runner_confidence": (near or {}).get("confidence") if near else None,
            "runner_ingest_ts": (near or {}).get("ingest_ts") if near else None,
            "matched": near is not None,
        })
    return out


def _parse_ts(raw) -> Optional[datetime]:
    if not raw:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None

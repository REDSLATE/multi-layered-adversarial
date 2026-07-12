"""Pulse health metrics — the durable schema after the P3 runner deletion.

Replaces `mc_pulse.parity_routes` (2026-07-12 P4). "Parity" only
made sense while the runner and pulse coexisted; with runners
gone, the meaningful questions are:

  1. Is each brain evaluating at the expected cadence?
     (evaluation_count, no_data_rate)

  2. Is each brain thinking? Not saturated?
     (confidence_mean, confidence_std, stale_input_rate)

  3. Is each brain looking at fresh market state?
     (latest_source_bar_at, pulse_lag_ms)

  4. Are the four brains DISTINCT minds, not four lenses on one?
     (distinctness — cross-brain action agreement is a health
     signal, not a flip gate. Sustained collapse toward 1.0
     agreement means the personality multiplier isn't producing
     independent behavior and the strategy split (P7) is urgent.)

  5. Is the brain re-thinking the same bar redundantly?
     (duplicate_opinion_rate — high value suggests the cadence
     cool-down isn't binding OR the brain re-fingerprints the
     same input differently across ticks.)

  6. Are exceptions being swallowed silently?
     (exception_rate — surfaces containment.evaluate_brain
     failures that the pulse loop otherwise fails-soft on.)

**Backward-compat note**: `parity_routes.py` remains mounted with
a temporary alias router that redirects `/api/mc/parity/*` calls
to this module. Delete after 1 iteration.
"""
from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from statistics import mean, median, pstdev
from typing import Optional

from fastapi import APIRouter, Depends, Query

from auth import get_current_user
from db import db

logger = logging.getLogger("mc_pulse.pulse_health")

router = APIRouter(prefix="/mc/pulse-health", tags=["mc-pulse-health"])

MC_OPINIONS_COMPARE = "mc_opinions_compare"
MC_PULSES = "mc_pulses"
MC_PULSE_HEALTH_SNAPSHOTS = "mc_pulse_health_snapshots"

# Persistence list — all 4 brains currently registered in the pulse.
PULSE_HEALTH_SNAPSHOT_BRAINS = ["camino", "gto", "barracuda", "hellcat"]


# ─────────────── computation ───────────────

async def compute_pulse_health(brain_id: str, *, hours: int = 24) -> dict:
    """Compute the pulse-health metrics for `brain_id` over the last
    `hours` hours. Auth-free, side-effect-free — used by both the
    HTTP endpoint and the background snapshotter.

    Fail-soft: bounded reads (`max_time_ms`) with fallback to empty
    tape on Atlas timeout.
    """
    now = datetime.now(timezone.utc)
    since = (now - timedelta(hours=hours)).isoformat()
    brain_lc = brain_id.strip().lower()

    opinions = await _safe_find(
        MC_OPINIONS_COMPARE,
        {"brain": brain_lc, "evaluated_at": {"$gte": since}},
    )
    # Peer opinions — for distinctness calc. All brains except self.
    peer_opinions = await _safe_find(
        MC_OPINIONS_COMPARE,
        {"brain": {"$ne": brain_lc}, "evaluated_at": {"$gte": since}},
    )
    pulses = await _safe_find(
        MC_PULSES,
        {"started_at": {"$gte": since}},
    )

    return {
        "brain": brain_lc,
        "window_hours": hours,
        "since": since,
        "evaluation_count": len(opinions),
        "action_distribution": _action_distribution(opinions),
        "confidence_mean": _confidence_mean(opinions),
        "confidence_std": _confidence_std(opinions),
        "stale_input_rate": _stale_input_rate(opinions),
        "no_data_rate": _no_data_rate(opinions, pulses, brain_lc),
        "no_data_breakdown": _no_data_breakdown(pulses, brain_lc),
        "exception_rate": _exception_rate(pulses, brain_lc),
        "duplicate_opinion_rate": _duplicate_opinion_rate(opinions),
        "latest_source_bar_at": _latest_source_bar_at(opinions),
        "pulse_lag_ms": _pulse_lag_ms(opinions),
        "distinctness": _distinctness(opinions, peer_opinions),
    }


# ─────────────── metric primitives ───────────────

def _action_distribution(rows: list[dict]) -> dict:
    """Count of LONG/SHORT/FLAT + normalized percentages."""
    c = Counter()
    for r in rows:
        d = _extract_direction(r)
        if d:
            c[d] += 1
    total = sum(c.values()) or 1
    return {
        "counts": {k: c.get(k, 0) for k in ("LONG", "SHORT", "FLAT")},
        "pct": {k: round(100.0 * c.get(k, 0) / total, 2)
                for k in ("LONG", "SHORT", "FLAT")},
    }


def _extract_direction(row: dict) -> Optional[str]:
    op = row.get("opinion") or {}
    d = op.get("direction") or row.get("direction")
    return d.upper() if isinstance(d, str) else None


def _extract_confidence(row: dict) -> Optional[float]:
    op = row.get("opinion") or {}
    c = op.get("confidence") if "confidence" in op else row.get("confidence")
    try:
        return float(c) if c is not None else None
    except (TypeError, ValueError):
        return None


def _confidence_mean(rows: list[dict]) -> float:
    vals = [c for r in rows if (c := _extract_confidence(r)) is not None]
    return round(mean(vals), 4) if vals else 0.0


def _confidence_std(rows: list[dict]) -> float:
    vals = [c for r in rows if (c := _extract_confidence(r)) is not None]
    return round(pstdev(vals), 4) if len(vals) >= 2 else 0.0


def _stale_input_rate(rows: list[dict]) -> float:
    """Fraction of opinions where the brain hit INSUFFICIENT_DATA
    (i.e., the freshness / feature-required gate rejected the
    snapshot). A high value means feeders or the canonical builder
    are starving the brain."""
    if not rows:
        return 0.0
    n_stale = 0
    for r in rows:
        op = r.get("opinion") or {}
        status = op.get("status") or r.get("status")
        if status == "INSUFFICIENT_DATA":
            n_stale += 1
    return round(n_stale / len(rows), 4)


def _no_data_rate(
    opinions: list[dict], pulses: list[dict], brain_lc: str,
) -> float:
    """Fraction of pulses in the window where THIS brain contributed
    nothing (neither an opinion NOR an exception). Doctrine-aligned:
    a healthy brain should participate in every eligible pulse."""
    if not pulses:
        return 0.0
    # Set of pulse_ids where this brain wrote at least one opinion.
    opinion_pulse_ids = {r.get("pulse_id") for r in opinions if r.get("pulse_id")}
    # Set of pulse_ids where this brain FAILED (containment caught).
    failed_pulse_ids = set()
    for p in pulses:
        failed = p.get("brains_failed") or []
        for bf in failed:
            if isinstance(bf, dict) and (bf.get("brain_id") or bf.get("brain") or "").lower() == brain_lc:
                failed_pulse_ids.add(p.get("pulse_id"))
    contributed = opinion_pulse_ids | failed_pulse_ids
    total = len(pulses)
    silent = sum(1 for p in pulses if p.get("pulse_id") not in contributed)
    return round(silent / total, 4)


def _no_data_breakdown(
    pulses: list[dict], brain_lc: str,
) -> dict[str, dict]:
    """P1 (2026-02-11): break `no_data_rate` into its constituent
    reasons — the operator's question was never "how often is the
    brain silent" but "WHY is it silent". Reads `brains_silent`
    rows off `mc_pulses`; brains that predate the P1 stamp show
    an `unknown` bucket which decays to zero within one window.

    Returned shape:
        {
          "snapshot_stale":   {"count": 812, "percent": 32.4},
          "cadence_cooldown": {"count": 354, "percent": 14.1},
          "no_signal_return": {"count":  67, "percent":  2.7},
          "unknown":          {"count":   0, "percent":  0.0},
        }

    `percent` is a percentage of TOTAL pulses in the window (not
    of silent pulses) so the sum matches the headline
    `no_data_rate` × 100 for the brain. UI can render as either.
    """
    if not pulses:
        return {}
    total = len(pulses)
    # Set of pulse_ids where this brain contributed (opinion or
    # failure) — those pulses are NOT silent for this brain and
    # shouldn't have their silence rows counted.
    contributed: set = set()
    for p in pulses:
        # NB: opinion contributions are joined by the caller in
        # `_no_data_rate`; here we deduce from `brains_completed`
        # which is stamped on the receipt at completion.
        completed = p.get("brains_completed") or []
        if brain_lc in [str(x).lower() for x in completed]:
            contributed.add(p.get("pulse_id"))
        for bf in (p.get("brains_failed") or []):
            if isinstance(bf, dict):
                bid = (bf.get("brain_id") or bf.get("brain") or "").lower()
                if bid == brain_lc:
                    contributed.add(p.get("pulse_id"))

    counts: Counter = Counter()
    for p in pulses:
        pid = p.get("pulse_id")
        # Only count silences for pulses where this brain didn't
        # otherwise contribute (defensive: brains_silent should
        # never overlap brains_completed, but the check is cheap
        # and prevents double-counting if it ever does).
        if pid in contributed:
            continue
        # Collect ALL reasons this brain was silent for on this
        # pulse (one row per snapshot it was skipped on). Then
        # attribute the WHOLE pulse to the majority reason. This
        # keeps the percentages summing to `no_data_rate × 100`
        # regardless of how many symbols were in the pulse.
        per_pulse_reasons: Counter = Counter()
        for bs in (p.get("brains_silent") or []):
            if not isinstance(bs, dict):
                continue
            if (bs.get("brain_id") or "").lower() != brain_lc:
                continue
            per_pulse_reasons[bs.get("reason") or "unknown"] += 1

        if per_pulse_reasons:
            # Majority reason wins the pulse. Ties broken by
            # Counter's insertion order, which reflects the order
            # brains_silent was appended — snapshot_stale first
            # (pre-cadence check), so it wins ties naturally.
            top_reason, _ = per_pulse_reasons.most_common(1)[0]
            counts[top_reason] += 1
        elif pid is not None:
            # Silent pulse with no BrainSilence row = pre-P1 pulse.
            # Attribute to `unknown` so the totals still add up.
            # But only if the brain was expected to evaluate at all.
            # If brains_expected was 0, the pulse had no work for
            # anyone and shouldn't count against this brain.
            if int(p.get("brains_expected") or 0) > 0:
                counts["unknown"] += 1

    return {
        reason: {
            "count": count,
            "percent": round(100.0 * count / total, 2),
        }
        for reason, count in counts.most_common()
    }


def _exception_rate(pulses: list[dict], brain_lc: str) -> float:
    """Fraction of pulses where THIS brain raised (caught by
    containment). Non-zero = there's a bug the pulse loop is
    silently containing."""
    if not pulses:
        return 0.0
    n_exc = 0
    for p in pulses:
        for bf in (p.get("brains_failed") or []):
            if isinstance(bf, dict):
                bid = (bf.get("brain_id") or bf.get("brain") or "").lower()
                if bid == brain_lc:
                    n_exc += 1
    return round(n_exc / len(pulses), 4)


def _duplicate_opinion_rate(rows: list[dict]) -> float:
    """Fraction of opinions where (symbol, bar_key, direction) has
    been seen before in the window. Idempotency signal — high value
    means the cadence cool-down isn't binding or the brain
    re-fingerprints the same bar as a fresh input.
    """
    if len(rows) < 2:
        return 0.0
    seen: set[tuple] = set()
    dup = 0
    for r in rows:
        bc = _extract_bar_key(r)
        if not bc:
            continue
        key = (
            r.get("symbol") or "",
            bc,
            _extract_direction(r) or "",
        )
        if key in seen:
            dup += 1
        else:
            seen.add(key)
    return round(dup / len(rows), 4)


def _extract_bar_key(row: dict) -> Optional[str]:
    """The (symbol, bar_key) tuple is how we match opinions across
    brains for distinctness + dedup. The pulse envelope stamps
    `bucket_iso` (5-min UTC bucket start) as the canonical
    temporal key — every brain looking at the same bar shares
    this value. Fallback to `source_bar_close_at` if the schema
    ever adds it. Fallback to `ts` if bucket_iso is missing.
    """
    return (
        row.get("bucket_iso")
        or (row.get("opinion") or {}).get("source_bar_close_at")
        or row.get("source_bar_close_at")
        or row.get("ts")
    )


def _latest_source_bar_at(rows: list[dict]) -> Optional[str]:
    """Newest bar this brain evaluated in the window. Uses the
    `bucket_iso` field (5-min bucket start) — the canonical
    per-bar key on `mc_opinions_compare`.
    """
    latest = None
    for r in rows:
        bc = _extract_bar_key(r)
        if bc and (latest is None or bc > latest):
            latest = bc
    return latest


def _pulse_lag_ms(rows: list[dict]) -> Optional[float]:
    """Median lag between bar-bucket start and pulse emission.
    A 5-min bucket that closes at bucket_start+5min gives a
    theoretical minimum lag of ~0ms (bar closes → pulse fires).
    Higher = feeder + pulse chain is stale.
    """
    lags: list[float] = []
    for r in rows:
        ev = _parse_ts(r.get("evaluated_at"))
        bc = _parse_ts(_extract_bar_key(r))
        if ev and bc and ev >= bc:
            lags.append((ev - bc).total_seconds() * 1000.0)
    if not lags:
        return None
    return round(median(lags), 1)


def _distinctness(
    self_rows: list[dict], peer_rows: list[dict],
) -> dict:
    """Cross-brain action agreement — the operator's cognitive-
    separation signal. For every (symbol, bar_key) where THIS
    brain and any peer both emitted, count agreement.

    Returns:
      pairwise_agreement_rate: 0..1, fraction of (symbol, bar_key)
        matches where the peer agreed on direction with self.
      distinctness: 1 - pairwise_agreement_rate. Higher = more
        cognitively separate from peers.
      peer_matches: total (peer, symbol, bar_key) pairs evaluated.

    Doctrine: distinctness is a HEALTH signal, not a flip gate.
    Sustained collapse toward 0.0 means the 4 brains have merged
    into 1 brain in 4 skins; that's the trigger for P7 (strategy
    split).
    """
    if not self_rows or not peer_rows:
        return {"pairwise_agreement_rate": None, "distinctness": None,
                "peer_matches": 0}

    # index self by (symbol, bar_key) → direction
    self_ix: dict[tuple[str, str], str] = {}
    for r in self_rows:
        bc = _extract_bar_key(r)
        sym = (r.get("symbol") or "").upper()
        d = _extract_direction(r)
        if bc and sym and d:
            self_ix[(sym, bc)] = d

    if not self_ix:
        return {"pairwise_agreement_rate": None, "distinctness": None,
                "peer_matches": 0}

    matched = 0
    agreed = 0
    seen_pairs: set[tuple] = set()
    for r in peer_rows:
        bc = _extract_bar_key(r)
        sym = (r.get("symbol") or "").upper()
        d = _extract_direction(r)
        peer = (r.get("brain") or "").lower()
        if not (bc and sym and d and peer):
            continue
        # dedupe on (peer, symbol, bc)
        k = (peer, sym, bc)
        if k in seen_pairs:
            continue
        seen_pairs.add(k)
        self_dir = self_ix.get((sym, bc))
        if self_dir is None:
            continue
        matched += 1
        if self_dir == d:
            agreed += 1
    if matched == 0:
        return {"pairwise_agreement_rate": None, "distinctness": None,
                "peer_matches": 0}
    agree_rate = agreed / matched
    return {
        "pairwise_agreement_rate": round(agree_rate, 4),
        "distinctness": round(1.0 - agree_rate, 4),
        "peer_matches": matched,
    }


# ─────────────── db helpers ───────────────

async def _safe_find(collection: str, query: dict) -> list[dict]:
    """Bounded read + fail-soft. Returns empty list on timeout."""
    try:
        return await db[collection].find(query).max_time_ms(2500).to_list(20000)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "pulse_health safe_find %s failed: %s", collection, exc,
        )
        return []


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


# ─────────────── snapshotter ───────────────

async def take_pulse_health_snapshot(
    brain_id: str, *, hours: int = 24,
) -> dict:
    """Compute pulse-health for `brain_id` and persist ONE compact
    row to `mc_pulse_health_snapshots`. Returns the persisted doc.
    Fail-soft: any exception → returns {}; never crashes the loop.
    """
    try:
        health = await compute_pulse_health(brain_id, hours=hours)
        doc = {
            "at": datetime.now(timezone.utc).isoformat(),
            "brain": brain_id.strip().lower(),
            "window_hours": hours,
            "evaluation_count": health.get("evaluation_count", 0),
            "action_distribution": health.get("action_distribution", {}),
            "confidence_mean": health.get("confidence_mean", 0.0),
            "confidence_std": health.get("confidence_std", 0.0),
            "stale_input_rate": health.get("stale_input_rate", 0.0),
            "no_data_rate": health.get("no_data_rate", 0.0),
            "no_data_breakdown": health.get("no_data_breakdown", {}),
            "exception_rate": health.get("exception_rate", 0.0),
            "duplicate_opinion_rate": health.get("duplicate_opinion_rate", 0.0),
            "latest_source_bar_at": health.get("latest_source_bar_at"),
            "pulse_lag_ms": health.get("pulse_lag_ms"),
            "distinctness": health.get("distinctness", {}),
        }
        await db[MC_PULSE_HEALTH_SNAPSHOTS].insert_one(dict(doc))
        return doc
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "take_pulse_health_snapshot failed brain=%s: %s", brain_id, exc,
        )
        return {}


# ─────────────── routes ───────────────

@router.get("/{brain_id}")
async def pulse_health(
    brain_id: str,
    hours: int = Query(24, ge=1, le=168),
    user: dict = Depends(get_current_user),
):
    """Live pulse-health snapshot for `brain_id` over the last N hours."""
    return await compute_pulse_health(brain_id, hours=hours)


@router.get("/{brain_id}/history")
async def pulse_health_history(
    brain_id: str,
    limit: int = Query(96, ge=1, le=672),
    user: dict = Depends(get_current_user),
):
    """Rolling snapshot list for `brain_id`. Newest first.
    Default 96 = 24h at 15-min cadence; max 672 = 7d.
    """
    brain_lc = brain_id.strip().lower()
    rows = await _safe_find(MC_PULSE_HEALTH_SNAPSHOTS, {"brain": brain_lc})
    rows.sort(key=lambda r: r.get("at", ""), reverse=True)
    rows = rows[:limit]
    for r in rows:
        r.pop("_id", None)
    return {"brain": brain_lc, "count": len(rows), "snapshots": rows}

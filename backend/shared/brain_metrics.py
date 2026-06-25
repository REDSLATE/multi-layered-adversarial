"""Brain metrics computation — five KPIs for the operator's
multi-day observation window.

Pure, async-friendly helpers. No I/O here; the admin route loads
the raw intents + receipts and hands them to these functions.

Surfaced metrics (operator-requested 2026-02):
  1. HOLD count             — v2 `action="HOLD"` + v3 `plan.intent IN
                              [WATCH, DEFER, ABSTAIN]` (pre-wired so
                              v3 emits light up the moment they ship).
  2. Entropy average        — Shannon entropy over each brain's
                              action distribution, normalized to
                              [0, 1] by log2(global_action_cardinality).
                              Then meaned across brains.
  3. Reason-code distribution — Top-15 leaderboard of
                              `gate_state` (from shared_intents) +
                              `final_reason` (from pipeline_receipts).
  4. Lane-specific decisions — `{equity: {BUY: n, ...}, crypto: {...}}`
                              action histogram split by lane.
  5. Probability spread     — For each (symbol, hour-bucket) where
                              ≥2 brains emitted, max(confidence) -
                              min(confidence). Mean / median across
                              buckets. High = brains disagree.

These are operator decision-quality signals, not pipeline health
(that's what the funnel does). The whole point is to track them
over several days and watch trends.
"""
from __future__ import annotations

import math
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional


# ── Constants ───────────────────────────────────────────────────────
V3_HOLD_EQUIVALENT_INTENTS = {"WATCH", "DEFER", "ABSTAIN"}
REASON_CODE_TOP_N = 15
PROBABILITY_SPREAD_BUCKET_SECONDS = 3600  # 1 hour


# ── 1. HOLD count ───────────────────────────────────────────────────
def count_holds(intents: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Count HOLD-equivalent intents across v2 + v3 envelopes.

    v2: `action == "HOLD"`.
    v3: `plan.intent IN {WATCH, DEFER, ABSTAIN}`.

    Returned shape:
      {
        "v2_hold": int,
        "v3_watch": int,
        "v3_defer": int,
        "v3_abstain": int,
        "v3_total": int,
        "combined": int,
        "by_brain": { brain_id: { v2_hold, v3_*, combined } }
      }
    """
    out_v2 = 0
    out_v3 = Counter()
    by_brain: Dict[str, Dict[str, int]] = defaultdict(
        lambda: {"v2_hold": 0, "v3_watch": 0, "v3_defer": 0,
                 "v3_abstain": 0, "combined": 0}
    )

    for it in intents:
        # 2026-02-23 dual-field migration: prefer `stack_canonical`
        # so legacy + canonical docs aggregate into ONE bucket
        # instead of two (camaro + barracuda showing as separate
        # brains on the dashboard).
        brain = (
            it.get("stack_canonical")
            or it.get("stack")
            or it.get("brain_id")
            or "unknown"
        ).lower()
        action = str(it.get("action") or "").upper()
        plan = it.get("plan") if isinstance(it.get("plan"), dict) else {}
        plan_intent = str(plan.get("intent") or "").upper()

        if action == "HOLD":
            out_v2 += 1
            by_brain[brain]["v2_hold"] += 1
            by_brain[brain]["combined"] += 1
        if plan_intent in V3_HOLD_EQUIVALENT_INTENTS:
            key = f"v3_{plan_intent.lower()}"
            out_v3[plan_intent] += 1
            if key in by_brain[brain]:
                by_brain[brain][key] += 1
            by_brain[brain]["combined"] += 1

    return {
        "v2_hold": out_v2,
        "v3_watch": int(out_v3.get("WATCH", 0)),
        "v3_defer": int(out_v3.get("DEFER", 0)),
        "v3_abstain": int(out_v3.get("ABSTAIN", 0)),
        "v3_total": int(sum(out_v3.values())),
        "combined": out_v2 + int(sum(out_v3.values())),
        "by_brain": dict(by_brain),
    }


# ── 2. Entropy average ──────────────────────────────────────────────
def _shannon_entropy_normalized(counts: Dict[str, int], k: int) -> float:
    """Shannon entropy of a discrete distribution, normalized to
    [0, 1] by log2(k) where k = global action cardinality.
    """
    total = sum(counts.values())
    if total == 0 or k < 2:
        return 0.0
    h = 0.0
    for c in counts.values():
        if c <= 0:
            continue
        p = c / total
        h -= p * math.log2(p)
    return h / math.log2(k)


def entropy_average(intents: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Average decision-entropy across brains.

    High = brains are mixed/indecisive (close to uniform across
    BUY/SELL/HOLD/...). Low = brains are committed (one action
    dominates).

    Returned shape:
      {
        "global_action_cardinality": int,
        "per_brain": { brain_id: { entropy, n_intents, distribution } },
        "mean_across_brains": float | None,
        "median_across_brains": float | None
      }
    """
    # Compute global cardinality across BOTH v2 action and v3 plan.intent.
    # Doctrine: brains that emit ANY recognizable decision are counted.
    all_actions: set[str] = set()
    by_brain_actions: Dict[str, Counter] = defaultdict(Counter)

    for it in intents:
        # 2026-02-23 dual-field migration — canonical-aware aggregation.
        brain = (
            it.get("stack_canonical")
            or it.get("stack")
            or it.get("brain_id")
            or "unknown"
        ).lower()
        # Prefer v3 plan.intent if present; fall back to v2 action.
        plan = it.get("plan") if isinstance(it.get("plan"), dict) else {}
        decision = str(plan.get("intent") or it.get("action") or "").upper()
        if not decision:
            continue
        all_actions.add(decision)
        by_brain_actions[brain][decision] += 1

    k = max(2, len(all_actions))  # avoid log2(1) = 0
    per_brain: Dict[str, Dict[str, Any]] = {}
    entropies: List[float] = []

    for brain, counts in by_brain_actions.items():
        h = _shannon_entropy_normalized(counts, k)
        n = int(sum(counts.values()))
        per_brain[brain] = {
            "entropy": round(h, 4),
            "n_intents": n,
            "distribution": dict(counts),
        }
        if n > 0:
            entropies.append(h)

    if entropies:
        mean = sum(entropies) / len(entropies)
        sorted_e = sorted(entropies)
        mid = len(sorted_e) // 2
        if len(sorted_e) % 2 == 0:
            median = (sorted_e[mid - 1] + sorted_e[mid]) / 2.0
        else:
            median = sorted_e[mid]
    else:
        mean = None
        median = None

    return {
        "global_action_cardinality": k,
        "per_brain": per_brain,
        "mean_across_brains": round(mean, 4) if mean is not None else None,
        "median_across_brains": round(median, 4) if median is not None else None,
    }


# ── 3. Reason-code distribution ─────────────────────────────────────
def reason_code_distribution(
    intents: List[Dict[str, Any]],
    receipts_by_id: Dict[str, Dict[str, Any]],
    top_n: int = REASON_CODE_TOP_N,
) -> Dict[str, Any]:
    """Top-N leaderboard of WHY intents didn't execute.

    Sources:
      * `shared_intents.gate_state` (canonical operator-facing column)
      * `pipeline_receipts.final_reason` (the pipeline's own verdict)

    They're complementary — gate_state is the high-level status, the
    receipt reason is the specific blocker. We track both.

    Returned shape:
      {
        "top_gate_states":  [ {reason, count, pct}, ... ],
        "top_final_reasons":[ {reason, count, pct}, ... ],
        "total_intents":    int
      }
    """
    gate_counts: Counter = Counter()
    reason_counts: Counter = Counter()

    for it in intents:
        gate = (it.get("gate_state") or "unknown").strip().lower()
        gate_counts[gate] += 1
        r = receipts_by_id.get(it.get("intent_id"))
        if r:
            reason = (r.get("final_reason") or "unknown").strip().lower()
            reason_counts[reason] += 1

    total = max(1, len(intents))

    def _topn(c: Counter) -> List[Dict[str, Any]]:
        return [
            {
                "reason": k,
                "count": v,
                "pct_of_total": round(100.0 * v / total, 2),
            }
            for k, v in c.most_common(top_n)
        ]

    return {
        "top_gate_states": _topn(gate_counts),
        "top_final_reasons": _topn(reason_counts),
        "total_intents": len(intents),
    }


# ── 4. Lane-specific decisions ──────────────────────────────────────
def lane_specific_decisions(
    intents: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Action histogram split by lane.

    Returned shape:
      {
        "equity":  { "BUY": n, "SELL": n, "HOLD": n, ... , "total": n },
        "crypto":  { ... },
        "unknown": { ... }   (only when present)
      }
    """
    out: Dict[str, Dict[str, int]] = {}
    for it in intents:
        lane = (it.get("lane") or "unknown").lower()
        plan = it.get("plan") if isinstance(it.get("plan"), dict) else {}
        # Prefer v3 plan.intent if present; fall back to v2 action.
        decision = str(plan.get("intent") or it.get("action") or "UNKNOWN").upper()
        if lane not in out:
            out[lane] = {}
        out[lane][decision] = out[lane].get(decision, 0) + 1
        out[lane]["total"] = out[lane].get("total", 0) + 1
    return out


# ── 5. Probability spread ───────────────────────────────────────────
def _bucket_ts(ts_iso: Optional[str], bucket_seconds: int) -> Optional[int]:
    """Floor an ISO-8601 timestamp into bucket-aligned epoch seconds."""
    if not ts_iso:
        return None
    try:
        # Tolerate trailing Z (Mongo sometimes serializes that way).
        s = ts_iso[:-1] + "+00:00" if ts_iso.endswith("Z") else ts_iso
        dt = datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None
    epoch = int(dt.timestamp())
    return (epoch // bucket_seconds) * bucket_seconds


def probability_spread(
    intents: List[Dict[str, Any]],
    bucket_seconds: int = PROBABILITY_SPREAD_BUCKET_SECONDS,
) -> Dict[str, Any]:
    """Mean / median (symbol, hour-bucket) confidence spread.

    For each (symbol, hour-bucket) where ≥2 distinct brains emitted,
    compute max(confidence) − min(confidence). Then aggregate.

    Returned shape:
      {
        "n_disagreement_buckets": int,    # buckets with ≥2 brains
        "n_total_buckets": int,           # buckets with ≥1 brain
        "mean_spread": float | None,
        "median_spread": float | None,
        "max_spread": float | None,
        "bucket_seconds": int,
        "top_disagreement": [
          { symbol, ts_bucket, spread, brains: {brain_id: conf} }, ...
        ]  # top 10 widest spreads
      }
    """
    # Group: (symbol, bucket_ts) -> {brain_id: max_conf_seen}
    by_bucket: Dict[tuple, Dict[str, float]] = defaultdict(dict)

    for it in intents:
        sym = (it.get("symbol") or "").upper()
        if not sym:
            continue
        # 2026-02-23 dual-field migration — canonical-aware aggregation.
        brain = (
            it.get("stack_canonical")
            or it.get("stack")
            or it.get("brain_id")
            or "unknown"
        ).lower()
        bucket = _bucket_ts(it.get("ingest_ts"), bucket_seconds)
        if bucket is None:
            continue
        conf = it.get("confidence")
        if not isinstance(conf, (int, float)):
            continue
        key = (sym, bucket)
        # If a brain emits multiple times in a bucket, use the highest-
        # confidence emission (operator's read: most committed take).
        prev = by_bucket[key].get(brain)
        if prev is None or float(conf) > prev:
            by_bucket[key][brain] = float(conf)

    spreads: List[Dict[str, Any]] = []
    n_total = 0
    for (sym, bucket), brain_confs in by_bucket.items():
        if len(brain_confs) < 1:
            continue
        n_total += 1
        if len(brain_confs) < 2:
            continue
        vals = list(brain_confs.values())
        spread = max(vals) - min(vals)
        spreads.append({
            "symbol": sym,
            "ts_bucket": datetime.utcfromtimestamp(bucket).isoformat() + "Z",
            "spread": round(spread, 4),
            "brains": {b: round(c, 4) for b, c in brain_confs.items()},
        })

    spread_vals = [s["spread"] for s in spreads]
    if spread_vals:
        spread_vals_sorted = sorted(spread_vals)
        mid = len(spread_vals_sorted) // 2
        if len(spread_vals_sorted) % 2 == 0:
            median = (spread_vals_sorted[mid - 1] + spread_vals_sorted[mid]) / 2.0
        else:
            median = spread_vals_sorted[mid]
        mean = sum(spread_vals) / len(spread_vals)
        mx = max(spread_vals)
    else:
        median = None
        mean = None
        mx = None

    spreads.sort(key=lambda r: r["spread"], reverse=True)

    return {
        "n_disagreement_buckets": len(spreads),
        "n_total_buckets": n_total,
        "mean_spread": round(mean, 4) if mean is not None else None,
        "median_spread": round(median, 4) if median is not None else None,
        "max_spread": round(mx, 4) if mx is not None else None,
        "bucket_seconds": bucket_seconds,
        "top_disagreement": spreads[:10],
    }



# ── 6. Consensus boost applied rate ─────────────────────────────────
# Operator pin (2026-06-24): answers "are advisors actually
# influencing executor decisions?"
#   0–5%   → advisors mostly noise / not lining up
#   5–25%  → healthy selective influence
#   25–50% → executor leaning on advisors a lot
#   50%+   → executor may be too dependent on advisor boost
# Computed from `intent_consensus_telemetry` (the sidecar written by
# seat_policy on every executor seat-floor evaluation). TTL on that
# collection was bumped to 7d to support the full metric window.
APPLIED_RATE_HEALTH_BANDS = (
    ("noise", 0.0, 0.05),
    ("healthy", 0.05, 0.25),
    ("heavy", 0.25, 0.50),
    ("over_dependent", 0.50, 1.01),
)


def _classify_applied_rate(rate: Optional[float], total: int = 0) -> str:
    """Operator-pinned health band for the applied rate.

    Returns 'no_data' when the denominator is zero so the UI can
    distinguish 'nothing happened yet' from 'happens 0% of the time'.

    Operator pin (2026-02-22, observation phase):
      When `total < INSUFFICIENT_SAMPLES_THRESHOLD` (50 evaluations),
      the band returns `'insufficient_data'` regardless of the rate.
      The metric is observability-only at that sample size — even a
      100% applied rate could be a small-N artifact, not real
      over-dependence. Don't tune until we have ≥50 evaluations.

      When sample is undersized AND rate is suspicious (>50%), the
      band returns `'insufficient_data_suspicious'` so the UI can
      render yellow with the doctrine note "behaviour suspicious,
      sample too small to act on".
    """
    if rate is None:
        return "no_data"
    if total < INSUFFICIENT_SAMPLES_THRESHOLD:
        # Defer the over_dependent verdict until we have enough data.
        if rate > 0.5:
            return "insufficient_data_suspicious"
        return "insufficient_data"
    for label, lo, hi in APPLIED_RATE_HEALTH_BANDS:
        if lo <= rate < hi:
            return label
    return "noise"


# Operator pin (2026-02-22): below this many executor evaluations,
# the consensus_boost_applied_rate is observability-only — don't act
# on it (no tuning, no doctrine change). 50 mirrors the READY band
# threshold elsewhere in the v3 rollout (see admin_paradox_v3._BANDS).
INSUFFICIENT_SAMPLES_THRESHOLD = 50


async def consensus_boost_applied_rate(
    db,
    window_hours: int,
) -> Dict[str, Any]:
    """% of executor seat-floor evaluations where the boost moved
    confidence non-zero.

    Denominator: every executor intent that reached the seat floor
    check in the window (= every row in `intent_consensus_telemetry`
    in the window). Note: this is NOT 'all intents' — non-executor
    brains never reach the floor check and therefore aren't counted.
    This is the right denominator for the question "when consensus
    COULD apply, how often did it?".

    Numerator: rows where `applied = True` (set by seat_policy when
    `advisor_boost != 0`).

    Returned shape:
      {
        "applied_rate":     float | None,   # 0.0 — 1.0
        "applied_count":    int,
        "total_evaluated":  int,
        "health_band":      "no_data" | "noise" | "healthy" | "heavy" | "over_dependent",
        "window_hours":     int,
        "positive_boost_count": int,        # advisor_boost > 0
        "negative_boost_count": int,        # advisor_boost < 0
      }
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    cursor = db["intent_consensus_telemetry"].find(
        {"ts": {"$gte": cutoff}},
        {"_id": 0, "applied": 1, "advisor_boost": 1},
    )
    total = 0
    applied = 0
    pos = 0
    neg = 0
    async for row in cursor:
        total += 1
        boost = row.get("advisor_boost")
        if row.get("applied") is True or (
            isinstance(boost, (int, float)) and boost != 0
        ):
            applied += 1
        if isinstance(boost, (int, float)):
            if boost > 0:
                pos += 1
            elif boost < 0:
                neg += 1

    rate: Optional[float] = (applied / total) if total > 0 else None
    return {
        "applied_rate": round(rate, 4) if rate is not None else None,
        "applied_count": applied,
        "total_evaluated": total,
        "health_band": _classify_applied_rate(rate, total),
        "insufficient_samples_threshold": INSUFFICIENT_SAMPLES_THRESHOLD,
        "window_hours": int(window_hours),
        "positive_boost_count": pos,
        "negative_boost_count": neg,
    }

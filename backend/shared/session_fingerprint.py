"""Distribution Snapshot Job — per-session behavioral fingerprints.

Doctrine (2026-02-20, PRD Follow-up):
    Every doctrine change (a new gate, a threshold tweak, a label
    rename) shifts the distribution of what each brain FIRES vs
    BLOCKS. Without a persistent record of "what did the brain look
    like BEFORE the change", you can't tell whether a change
    tightened the strategy correctly or accidentally starved it.

    This module dumps per-(brain, lane) aggregate statistics on a
    fixed cadence to `session_fingerprints`. Windows are non-overlapping
    (each fingerprint covers a specific time range). Fingerprints
    are keyed by `(brain, lane, window_end_ts)` so re-runs are
    idempotent — same window emitted twice replaces the earlier doc.

Metrics captured (see `_compute_fingerprint` docstring for details):
    * intent_count                        — total intents in window
    * gate_state_dist                     — {blocked, submitted, ...}
    * quality_dist                        — {A_QUALITY, B_QUALITY, C_QUALITY}
    * top_fail_reasons                    — top-K from base_labels.reasons
    * top_objections                      — top-K from seats.adversary.objections
    * top_labels                          — top-K from base_labels.labels
    * execution_ready_rate                — fraction where exec_judge fired
    * gate_pass_rates                     — per-check (has_volume, spread_ok, ...)
    * confidence percentiles              — p10/p50/p90
    * risk_multiplier_p50                 — median governor clamp
    * rvol / gap_pct percentiles          — from snapshot.session_features
    * market_regime_dist                  — bull/bear/choppy counts

Env:
    SESSION_FINGERPRINT_ENABLED           default true
    SESSION_FINGERPRINT_INTERVAL_SEC      default 900 (15 min)
    SESSION_FINGERPRINT_WINDOW_MIN        default 15
    SESSION_FINGERPRINT_TOP_K             default 5

Anti-patterns this module WILL NOT do:
    * Per-symbol breakdown — that's the intent stream, not a fingerprint.
      A fingerprint is a HISTOGRAM view; use `/api/admin/intents` for
      symbol-level drill-down.
    * Retroactive rewrites of earlier fingerprints. If the operator
      changes a threshold at t=12:00, the 11:45–12:00 fingerprint
      still shows the OLD threshold's distribution. That's the point.
"""
from __future__ import annotations

import asyncio
import logging
import os
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Optional

from db import db
from namespaces import SHARED_INTENTS, SESSION_FINGERPRINTS

logger = logging.getLogger("session_fingerprint")


DEFAULT_INTERVAL_SEC = 900
DEFAULT_WINDOW_MIN = 15
DEFAULT_TOP_K = 5

BRAINS = ("camino", "barracuda", "hellcat", "gto")
LANES = ("equity", "crypto")


def _env_bool(key: str, default: bool) -> bool:
    val = os.environ.get(key)
    if val is None or val == "":
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key) or default)
    except (TypeError, ValueError):
        return default


def _percentiles(vals: list[float], ps: tuple[float, ...]) -> dict[str, float]:
    """Simple linear-interpolation percentiles. Vals need not be sorted."""
    if not vals:
        return {f"p{int(p * 100)}": None for p in ps}
    xs = sorted(vals)
    result = {}
    for p in ps:
        if p <= 0:
            result[f"p{int(p * 100)}"] = xs[0]
            continue
        if p >= 1:
            result[f"p{int(p * 100)}"] = xs[-1]
            continue
        idx = p * (len(xs) - 1)
        lo = int(idx)
        hi = min(lo + 1, len(xs) - 1)
        frac = idx - lo
        result[f"p{int(p * 100)}"] = xs[lo] * (1 - frac) + xs[hi] * frac
    return result


async def _compute_fingerprint(
    brain: str, lane: str,
    window_start_iso: str, window_end_iso: str,
    top_k: int,
) -> dict:
    """Aggregate one (brain, lane, window) fingerprint.

    Reads intents where `stack_canonical == brain`, `lane == lane`,
    and `ingest_ts` is inside the window. Computes distribution
    metrics without loading full docs into memory (aggregation
    pipeline where cheap; per-doc scans for percentiles because
    Mongo percentile is expensive across versions).
    """
    cursor = db[SHARED_INTENTS].find(
        {
            "stack_canonical": brain,
            "lane": lane,
            "ingest_ts": {"$gte": window_start_iso, "$lt": window_end_iso},
        },
        {
            "_id": 0,
            "gate_state": 1,
            "confidence": 1,
            "risk_multiplier": 1,
            "doctrine_packet.base_labels.quality": 1,
            "doctrine_packet.base_labels.labels": 1,
            "doctrine_packet.base_labels.reasons": 1,
            "doctrine_packet.seats.adversary.objections": 1,
            "doctrine_packet.seats.execution_judge.execution_ready": 1,
            "doctrine_packet.seats.execution_judge.execution_checks": 1,
            "snapshot.relative_volume": 1,
            "snapshot.gap_pct": 1,
            "snapshot.market_regime": 1,
        },
    )

    intent_count = 0
    gate_state_counter: Counter = Counter()
    quality_counter: Counter = Counter()
    labels_counter: Counter = Counter()
    reasons_counter: Counter = Counter()
    objections_counter: Counter = Counter()
    regime_counter: Counter = Counter()

    execution_ready_count = 0
    check_pass_counters: dict[str, int] = {}
    check_seen_counters: dict[str, int] = {}

    confidences: list[float] = []
    risk_multipliers: list[float] = []
    rvols: list[float] = []
    gaps: list[float] = []

    async for doc in cursor:
        intent_count += 1
        gate_state_counter[str(doc.get("gate_state") or "unknown")] += 1

        dp = (doc.get("doctrine_packet") or {})
        base = (dp.get("base_labels") or {})
        quality_counter[str(base.get("quality") or "unknown")] += 1

        for lbl in (base.get("labels") or []):
            labels_counter[lbl] += 1
        for rsn in (base.get("reasons") or []):
            reasons_counter[rsn] += 1

        seats = (dp.get("seats") or {})
        for obj in ((seats.get("adversary") or {}).get("objections") or []):
            objections_counter[obj] += 1

        ej = (seats.get("execution_judge") or {})
        if ej.get("execution_ready") is True:
            execution_ready_count += 1
        for check_name, val in (ej.get("execution_checks") or {}).items():
            check_seen_counters[check_name] = check_seen_counters.get(check_name, 0) + 1
            if val is True:
                check_pass_counters[check_name] = check_pass_counters.get(check_name, 0) + 1

        c = doc.get("confidence")
        if isinstance(c, (int, float)):
            confidences.append(float(c))
        rm = doc.get("risk_multiplier")
        if isinstance(rm, (int, float)):
            risk_multipliers.append(float(rm))

        snap = (doc.get("snapshot") or {})
        rv = snap.get("relative_volume")
        if isinstance(rv, (int, float)):
            rvols.append(float(rv))
        gp = snap.get("gap_pct")
        if isinstance(gp, (int, float)):
            gaps.append(float(gp))
        regime = snap.get("market_regime")
        if regime is not None:
            regime_counter[str(regime)] += 1

    def _top_k(counter: Counter, k: int) -> list[dict]:
        return [{"key": k_, "count": v} for k_, v in counter.most_common(k)]

    def _pass_rates(pass_c: dict, seen_c: dict) -> dict[str, float]:
        return {
            k: round(pass_c.get(k, 0) / seen_c[k], 4) if seen_c[k] > 0 else 0.0
            for k in seen_c
        }

    now_iso = datetime.now(timezone.utc).isoformat()
    return {
        "_id": f"{brain}:{lane}:{window_end_iso}",
        "brain": brain,
        "lane": lane,
        "window_start_ts": window_start_iso,
        "window_end_ts": window_end_iso,
        "computed_at": now_iso,
        "intent_count": intent_count,
        "gate_state_dist": dict(gate_state_counter),
        "quality_dist": dict(quality_counter),
        "top_labels": _top_k(labels_counter, top_k),
        "top_fail_reasons": _top_k(reasons_counter, top_k),
        "top_objections": _top_k(objections_counter, top_k),
        "execution_ready_rate": (
            round(execution_ready_count / intent_count, 4)
            if intent_count > 0 else 0.0
        ),
        "gate_pass_rates": _pass_rates(check_pass_counters, check_seen_counters),
        "confidence_percentiles": _percentiles(
            confidences, (0.1, 0.5, 0.9),
        ),
        "risk_multiplier_p50": _percentiles(
            risk_multipliers, (0.5,),
        )["p50"],
        "rvol_percentiles": _percentiles(rvols, (0.1, 0.5, 0.9)),
        "gap_pct_percentiles": _percentiles(gaps, (0.1, 0.5, 0.9)),
        "market_regime_dist": dict(regime_counter),
    }


async def _cadence_drift_sentinel() -> None:
    """Emit a WARNING log if any brain's most-recent intent is older
    than 3× the observed median tick interval for that brain.

    Doctrine (2026-02-20, operator P1 backlog): The handoff called
    out an "11-hour silent write halt" on Camino/Hellcat that had
    been diagnosed but not root-caused. Rather than trace something
    that isn't currently reproducing, this sentinel piggybacks on
    the fingerprint tick to LOG when a halt is happening — turning
    a silent failure into an operator-visible signal on the same
    surface as `latest_intent_age_s` (the /status telemetry).

    Approach: compare `latest_ts` (most recent intent) against the
    p50 inter-intent gap over the last hour. If age > 3× median gap,
    log a warning. Cheap — one aggregate per brain per tick.
    """
    now = datetime.now(timezone.utc)
    hour_ago = (now - timedelta(hours=1)).isoformat()

    for brain in BRAINS:
        cursor = db[SHARED_INTENTS].find(
            {"stack_canonical": brain, "ingest_ts": {"$gte": hour_ago}},
            {"_id": 0, "ingest_ts": 1},
        ).sort("ingest_ts", -1).limit(200)
        rows = [r async for r in cursor]
        if len(rows) < 3:
            continue

        # Inter-arrival gaps (seconds). Rows are DESCENDING.
        gaps: list[float] = []
        try:
            for prev, curr in zip(rows[:-1], rows[1:]):
                t_prev = datetime.fromisoformat(prev["ingest_ts"])
                t_curr = datetime.fromisoformat(curr["ingest_ts"])
                gaps.append((t_prev - t_curr).total_seconds())
        except (TypeError, ValueError):
            continue

        if not gaps:
            continue
        median_gap = sorted(gaps)[len(gaps) // 2]
        try:
            latest_ts = datetime.fromisoformat(rows[0]["ingest_ts"])
        except (TypeError, ValueError):
            continue
        age_s = (now - latest_ts).total_seconds()

        # 3× median gap OR >600s absolute floor (whichever is larger).
        # Absolute floor guards against a brain whose median gap is
        # measured in seconds — 3× 5s = 15s would false-fire on any
        # normal jitter.
        threshold_s = max(3.0 * median_gap, 600.0)
        if age_s > threshold_s:
            logger.warning(
                "cadence_drift_sentinel: brain=%s latest_age=%.1fs "
                "median_gap=%.1fs threshold=%.1fs — possible silent halt",
                brain, age_s, median_gap, threshold_s,
            )


async def _tick() -> dict:
    """One aggregation cycle: emit fingerprints for the just-completed
    aligned window across all (brain, lane) pairs.

    Windows are aligned to the interval — a 15-min window ending at
    12:15 covers 12:00–12:15. This ensures reruns of the same window
    are idempotent (same `_id` under the same tick alignment).
    """
    window_min = _env_int("SESSION_FINGERPRINT_WINDOW_MIN", DEFAULT_WINDOW_MIN)
    top_k = _env_int("SESSION_FINGERPRINT_TOP_K", DEFAULT_TOP_K)

    now = datetime.now(timezone.utc)
    # Align window END to the previous multiple of `window_min`.
    aligned_end_min = (now.minute // window_min) * window_min
    end_dt = now.replace(minute=aligned_end_min, second=0, microsecond=0)
    start_dt = end_dt - timedelta(minutes=window_min)

    written = 0
    for brain in BRAINS:
        for lane in LANES:
            try:
                fp = await _compute_fingerprint(
                    brain=brain, lane=lane,
                    window_start_iso=start_dt.isoformat(),
                    window_end_iso=end_dt.isoformat(),
                    top_k=top_k,
                )
                # Idempotent upsert — same _id replaces earlier doc.
                await db[SESSION_FINGERPRINTS].replace_one(
                    {"_id": fp["_id"]}, fp, upsert=True,
                )
                if fp["intent_count"] > 0:
                    written += 1
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "session_fingerprint compute failed brain=%s lane=%s: %r",
                    brain, lane, e,
                )
    return {
        "window_start_ts": start_dt.isoformat(),
        "window_end_ts": end_dt.isoformat(),
        "fingerprints_written": written,
    }


async def _tick_with_sentinel() -> dict:
    """One aggregation cycle + one cadence-drift sentinel pass."""
    result = await _tick()
    try:
        await _cadence_drift_sentinel()
    except Exception as e:  # noqa: BLE001
        logger.warning("cadence_drift_sentinel failed: %r", e)
    return result


_stop_flag: bool = False
_task: Optional[asyncio.Task] = None


async def _worker_loop() -> None:
    global _stop_flag
    interval = _env_int(
        "SESSION_FINGERPRINT_INTERVAL_SEC", DEFAULT_INTERVAL_SEC,
    )
    logger.info(
        "session_fingerprint started: interval=%ss window_min=%s",
        interval,
        _env_int("SESSION_FINGERPRINT_WINDOW_MIN", DEFAULT_WINDOW_MIN),
    )
    while not _stop_flag:
        try:
            result = await _tick_with_sentinel()
            if result.get("fingerprints_written", 0) > 0:
                logger.info(
                    "session_fingerprint tick: window=%s→%s written=%s",
                    result["window_start_ts"], result["window_end_ts"],
                    result["fingerprints_written"],
                )
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("session_fingerprint tick error: %r", e)
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            break


def start_worker_if_enabled() -> None:
    """Spawn the aggregator. Idempotent — safe on hot reload."""
    global _task, _stop_flag
    if _task is not None and not _task.done():
        return
    if not _env_bool("SESSION_FINGERPRINT_ENABLED", True):
        logger.info(
            "session_fingerprint disabled via "
            "SESSION_FINGERPRINT_ENABLED=false",
        )
        return
    _stop_flag = False
    _task = asyncio.create_task(
        _worker_loop(), name="session_fingerprint",
    )


async def stop_worker() -> None:
    global _task, _stop_flag
    _stop_flag = True
    if _task is not None and not _task.done():
        _task.cancel()
        try:
            await _task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _task = None


async def run_now() -> dict:
    """Manual one-shot invocation — used by admin re-trigger."""
    return await _tick()


# --------------------------------------------------------------------
# Fingerprint diffing — before/after doctrine-change validation
# --------------------------------------------------------------------
#
# Aggregates a set of fingerprints (already-computed per-window docs)
# in a time range into a single composite view, then diffs two of
# these composites (BEFORE vs AFTER). The diff is meant for the
# operator question: "I changed threshold X at t=T. Did the funnel
# shift how I expected, or did I accidentally starve a lane?"
#
# This module does NOT recompute from `shared_intents`. It only reads
# `session_fingerprints`, so `session_fingerprints` must have coverage
# for the requested windows. If not, the composite's `intent_count`
# will be zero and the diff surfaces that honestly.
#
# Doctrine anti-patterns explicitly avoided:
#   * No implicit "smoothing" of missing windows — a missing window
#     stays missing; the composite reports `windows_used` so the
#     operator sees coverage.
#   * No cross-brain / cross-lane composition. Diff a single
#     (brain, lane) pair at a time. Cross-brain rollups are a
#     different question.


def _aggregate_dict_counters(dicts: list[dict]) -> dict:
    """Sum values across a list of {key: count} dicts."""
    out: Counter = Counter()
    for d in dicts:
        if not isinstance(d, dict):
            continue
        for k, v in d.items():
            if isinstance(v, (int, float)):
                out[str(k)] += v
    return dict(out)


def _aggregate_top_lists(lists: list[list], top_k: int) -> list[dict]:
    """Merge a list of `[{key, count}, ...]` fingerprint fields into
    one top-K list (summed counts)."""
    merged: Counter = Counter()
    for lst in lists:
        if not isinstance(lst, list):
            continue
        for item in lst:
            if not isinstance(item, dict):
                continue
            k = item.get("key")
            c = item.get("count")
            if k is None or not isinstance(c, (int, float)):
                continue
            merged[str(k)] += c
    return [{"key": k, "count": v} for k, v in merged.most_common(top_k)]


def _pct_field(field: dict, total: int) -> dict:
    """Convert {key: count} into {key: pct} for the given total."""
    if total <= 0:
        return {k: 0.0 for k in field}
    return {k: round(v / total, 4) for k, v in field.items()}


def _aggregate_composite(fingerprints: list[dict], top_k: int = 5) -> dict:
    """Sum a list of fingerprint docs into one composite aggregate.

    Percentiles cannot be exactly recovered from stored fingerprints
    (raw values are gone), so the composite falls back to a
    WEIGHTED MEAN of each per-window percentile — a defensible-but-
    approximate summary. This is documented in the response.
    """
    total_intents = sum(int(fp.get("intent_count") or 0) for fp in fingerprints)

    gate_state_dist = _aggregate_dict_counters(
        [fp.get("gate_state_dist") or {} for fp in fingerprints]
    )
    quality_dist = _aggregate_dict_counters(
        [fp.get("quality_dist") or {} for fp in fingerprints]
    )
    market_regime_dist = _aggregate_dict_counters(
        [fp.get("market_regime_dist") or {} for fp in fingerprints]
    )
    top_fail_reasons = _aggregate_top_lists(
        [fp.get("top_fail_reasons") or [] for fp in fingerprints], top_k
    )
    top_objections = _aggregate_top_lists(
        [fp.get("top_objections") or [] for fp in fingerprints], top_k
    )
    top_labels = _aggregate_top_lists(
        [fp.get("top_labels") or [] for fp in fingerprints], top_k
    )

    # Execution-ready rate: n-weighted mean across windows.
    if total_intents > 0:
        exec_ready_weighted = sum(
            (fp.get("execution_ready_rate") or 0.0) * (fp.get("intent_count") or 0)
            for fp in fingerprints
        )
        execution_ready_rate = round(exec_ready_weighted / total_intents, 4)
    else:
        execution_ready_rate = 0.0

    # Gate pass rates: same n-weighted approach per check name.
    all_check_names = set()
    for fp in fingerprints:
        gpr = fp.get("gate_pass_rates") or {}
        all_check_names.update(gpr.keys())
    gate_pass_rates: dict = {}
    for check in all_check_names:
        if total_intents == 0:
            gate_pass_rates[check] = 0.0
            continue
        weighted = sum(
            ((fp.get("gate_pass_rates") or {}).get(check) or 0.0)
            * (fp.get("intent_count") or 0)
            for fp in fingerprints
        )
        gate_pass_rates[check] = round(weighted / total_intents, 4)

    def _weighted_percentiles(field_name: str) -> dict:
        keys = ("p10", "p50", "p90")
        if total_intents == 0:
            return {k: None for k in keys}
        out: dict = {}
        for k in keys:
            weighted_sum = 0.0
            weight_total = 0
            for fp in fingerprints:
                pct = ((fp.get(field_name) or {}).get(k))
                n = fp.get("intent_count") or 0
                if pct is None or n == 0:
                    continue
                weighted_sum += pct * n
                weight_total += n
            out[k] = round(weighted_sum / weight_total, 4) if weight_total else None
        return out

    confidence_percentiles = _weighted_percentiles("confidence_percentiles")
    rvol_percentiles = _weighted_percentiles("rvol_percentiles")
    gap_pct_percentiles = _weighted_percentiles("gap_pct_percentiles")

    # risk_multiplier: single p50 per window → weighted mean.
    if total_intents > 0:
        rm_weighted = 0.0
        rm_weight = 0
        for fp in fingerprints:
            rm = fp.get("risk_multiplier_p50")
            n = fp.get("intent_count") or 0
            if rm is None or n == 0:
                continue
            rm_weighted += rm * n
            rm_weight += n
        risk_multiplier_p50 = (
            round(rm_weighted / rm_weight, 4) if rm_weight else None
        )
    else:
        risk_multiplier_p50 = None

    return {
        "windows_used": len(fingerprints),
        "intent_count": total_intents,
        "gate_state_dist": gate_state_dist,
        "gate_state_dist_pct": _pct_field(gate_state_dist, total_intents),
        "quality_dist": quality_dist,
        "quality_dist_pct": _pct_field(quality_dist, total_intents),
        "market_regime_dist": market_regime_dist,
        "top_labels": top_labels,
        "top_fail_reasons": top_fail_reasons,
        "top_objections": top_objections,
        "execution_ready_rate": execution_ready_rate,
        "gate_pass_rates": gate_pass_rates,
        "confidence_percentiles": confidence_percentiles,
        "rvol_percentiles": rvol_percentiles,
        "gap_pct_percentiles": gap_pct_percentiles,
        "risk_multiplier_p50": risk_multiplier_p50,
    }


async def _load_fingerprints_in_range(
    brain: str, lane: str,
    start_ts_iso: str, end_ts_iso: str,
) -> list[dict]:
    """Load all `session_fingerprints` docs for (brain, lane) whose
    `window_end_ts` is within [start_ts, end_ts]. Inclusive on both
    ends — the natural operator intent when picking two timestamps.
    """
    cursor = db[SESSION_FINGERPRINTS].find(
        {
            "brain": brain,
            "lane": lane,
            "window_end_ts": {"$gte": start_ts_iso, "$lte": end_ts_iso},
        },
        sort=[("window_end_ts", 1)],
    )
    return [r async for r in cursor]


def _diff_percentiles(before: dict, after: dict) -> dict:
    out: dict = {}
    for k in ("p10", "p50", "p90"):
        b = before.get(k)
        a = after.get(k)
        if b is None or a is None:
            out[k] = None
        else:
            out[k] = round(a - b, 4)
    return out


def _diff_pct_dict(before: dict, after: dict) -> dict:
    """Delta = after_pct - before_pct for every key present in either."""
    keys = set(before.keys()) | set(after.keys())
    return {k: round((after.get(k) or 0.0) - (before.get(k) or 0.0), 4) for k in keys}


def _diff_top_reasons(
    before_top: list[dict], after_top: list[dict],
) -> dict:
    """Diff top-K reason lists. Returns:
        {
          "new_in_after": [keys only in after],
          "dropped_from_before": [keys only in before],
          "count_deltas": {key: delta_count, ...} across the union,
        }
    """
    b_map = {r["key"]: r["count"] for r in before_top if "key" in r}
    a_map = {r["key"]: r["count"] for r in after_top if "key" in r}
    new_in_after = sorted(set(a_map) - set(b_map))
    dropped = sorted(set(b_map) - set(a_map))
    all_keys = set(b_map) | set(a_map)
    count_deltas = {k: a_map.get(k, 0) - b_map.get(k, 0) for k in all_keys}
    return {
        "new_in_after": new_in_after,
        "dropped_from_before": dropped,
        "count_deltas": count_deltas,
    }


async def diff_fingerprints(
    brain: str, lane: str,
    before_start_ts: str, before_end_ts: str,
    after_start_ts: str, after_end_ts: str,
    top_k: int = 5,
) -> dict:
    """Compute a before/after diff of aggregated fingerprints.

    Returns a shape with three keys: `before`, `after`, `deltas`.
    The composite aggregates are approximations for percentiles
    (weighted mean) but exact sums for counts / distributions.
    """
    if brain not in BRAINS:
        raise ValueError(f"invalid brain {brain!r}")
    if lane not in LANES:
        raise ValueError(f"invalid lane {lane!r}")

    before_docs = await _load_fingerprints_in_range(
        brain, lane, before_start_ts, before_end_ts,
    )
    after_docs = await _load_fingerprints_in_range(
        brain, lane, after_start_ts, after_end_ts,
    )
    before_agg = _aggregate_composite(before_docs, top_k=top_k)
    after_agg = _aggregate_composite(after_docs, top_k=top_k)

    deltas = {
        "intent_count": (
            after_agg["intent_count"] - before_agg["intent_count"]
        ),
        "execution_ready_rate": round(
            after_agg["execution_ready_rate"]
            - before_agg["execution_ready_rate"], 4,
        ),
        "quality_dist_pct": _diff_pct_dict(
            before_agg["quality_dist_pct"], after_agg["quality_dist_pct"],
        ),
        "gate_state_dist_pct": _diff_pct_dict(
            before_agg["gate_state_dist_pct"], after_agg["gate_state_dist_pct"],
        ),
        "gate_pass_rates": _diff_pct_dict(
            before_agg["gate_pass_rates"], after_agg["gate_pass_rates"],
        ),
        "confidence_percentiles": _diff_percentiles(
            before_agg["confidence_percentiles"],
            after_agg["confidence_percentiles"],
        ),
        "rvol_percentiles": _diff_percentiles(
            before_agg["rvol_percentiles"],
            after_agg["rvol_percentiles"],
        ),
        "gap_pct_percentiles": _diff_percentiles(
            before_agg["gap_pct_percentiles"],
            after_agg["gap_pct_percentiles"],
        ),
        "risk_multiplier_p50": (
            None
            if before_agg["risk_multiplier_p50"] is None
            or after_agg["risk_multiplier_p50"] is None
            else round(
                after_agg["risk_multiplier_p50"]
                - before_agg["risk_multiplier_p50"], 4,
            )
        ),
        "top_fail_reasons": _diff_top_reasons(
            before_agg["top_fail_reasons"], after_agg["top_fail_reasons"],
        ),
        "top_labels": _diff_top_reasons(
            before_agg["top_labels"], after_agg["top_labels"],
        ),
        "top_objections": _diff_top_reasons(
            before_agg["top_objections"], after_agg["top_objections"],
        ),
    }

    return {
        "brain": brain,
        "lane": lane,
        "before": {
            "start_ts": before_start_ts,
            "end_ts": before_end_ts,
            **before_agg,
        },
        "after": {
            "start_ts": after_start_ts,
            "end_ts": after_end_ts,
            **after_agg,
        },
        "deltas": deltas,
        "note": (
            "percentile diffs use weighted-mean composites (raw values "
            "are not retained in session_fingerprints); count-based "
            "distributions and top-K lists are exact sums."
        ),
    }

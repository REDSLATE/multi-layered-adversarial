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

from fastapi import APIRouter, Depends, HTTPException, Query

from auth import get_current_user
from db import db

logger = logging.getLogger("mc_pulse.pulse_health")

# ── Migration Release 2 (2026-02-11): brain → brain_id ──
# The `BrainFailure` receipt schema was standardised on `brain_id`,
# but the legacy `bf.get("brain")` field slipped through in a few
# health-metric reads. Release 1 (iter-28l) added the fallback.
# Release 2 (this) logs a warning EVERY TIME the legacy field is
# hit — one warning per callsite per process boot (spam-safe via
# a module-level set). Release 3 will delete the fallback branch
# entirely once the warning stops firing for a full TTL window.
_LEGACY_BRAIN_FIELD_WARNED: set = set()


def _read_brain_id_from_failure(bf: dict, *, callsite: str) -> str:
    """Read `bf.brain_id`, falling back to legacy `bf.brain`. Emits
    a one-shot warning per callsite whenever the fallback fires,
    so operator logs harvest any remaining legacy writers before
    Release 3 removes the fallback."""
    bid = bf.get("brain_id")
    if bid:
        return str(bid).lower()
    legacy = bf.get("brain")
    if legacy:
        if callsite not in _LEGACY_BRAIN_FIELD_WARNED:
            _LEGACY_BRAIN_FIELD_WARNED.add(callsite)
            logger.warning(
                "brain→brain_id migration: legacy `brain` field "
                "encountered at %s. Release 3 will remove the "
                "fallback; check receipt writers.", callsite,
            )
        return str(legacy).lower()
    return ""

router = APIRouter(prefix="/mc/pulse-health", tags=["mc-pulse-health"])

MC_OPINIONS_COMPARE = "mc_opinions_compare"
MC_PULSES = "mc_pulses"
MC_SEATS = "mc_seats"
MC_PULSE_HEALTH_SNAPSHOTS = "mc_pulse_health_snapshots"
# P3 (2026-02-11): dissent correctness reads from these two.
SHARED_BRAIN_OPINIONS = "shared_brain_opinions"
SHARED_BRAIN_OUTCOMES = "shared_brain_outcomes"

# Persistence list — all 4 brains currently registered in the pulse.
PULSE_HEALTH_SNAPSHOT_BRAINS = ["camino", "gto", "barracuda", "hellcat"]

# P3 (2026-02-11): minimum resolved dissents needed before we
# render a rate on the tile. Smaller samples get a "gathering
# samples (N / 50)" placeholder — statistical noise dominates at
# low N and would mislead the operator.
DISSENT_MIN_SAMPLES = 50
# Concurrency window for peer-opinion detection. Two opinions on
# the same topic posted within this window are "concurrent" and
# eligible for majority-vote comparison. 15min matches typical
# bar cadence for equities and is generous for crypto.
DISSENT_CONCURRENCY_WINDOW_SEC = 900


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
    # P4 (2026-02-11): arbiter alignment reads decisions off the seat
    # tape. Each decision has `winner_brain` + `field[].brain`, so we
    # can compute participation + wins in one pass. Empty result is
    # fine — brand-new deployments have no decisions yet.
    arbiter_decisions = await _safe_find(
        MC_SEATS,
        {
            "decision.arbitrated_at": {"$gte": since},
            "decision.field.brain": brain_lc,
        },
        projection={
            "_id": 0,
            "decision.winner_brain": 1,
            "decision.field.brain": 1,
            "decision.arbitrated_at": 1,
        },
    )

    # P2 (2026-02-11): stamp the market regime AT SNAPSHOT TIME so
    # downstream slicing (distinctness-by-regime, alignment-by-regime)
    # is possible without reprocessing. `get_regime` is TTL-cached
    # (default 15min) so each snapshot is a cheap read. Regime is a
    # SYSTEM property, not a per-brain property — every brain shares
    # the same regime for the same wall-clock — but we stamp it on
    # each brain's row so operators can query "camino during choppy".
    try:
        from shared.market_regime import get_regime  # noqa: WPS433
        regime_dict = await get_regime()
        market_regime = regime_dict.get("regime")
    except Exception as exc:  # noqa: BLE001
        logger.warning("market_regime lookup failed: %s", exc)
        market_regime = None

    # P3 (2026-02-11): informative-divergence / dissent-correctness.
    # We only compute it when the SAMPLE is big enough (N ≥ 50);
    # smaller samples return `{resolved: N, gathering_samples: True}`
    # so the tile shows a "gathering samples" placeholder. The join
    # reads `shared_brain_opinions` (all brains, this window) +
    # `shared_brain_outcomes` (via opinion_id) and detects dissents
    # against concurrent peer opinions (±15 min on same topic).
    dissent = await _dissent_correctness(brain_lc, since)

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
        "arbiter_alignment": _arbiter_alignment(arbiter_decisions, brain_lc),
        "market_regime": market_regime,
        "dissent_correctness": dissent,
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
            if isinstance(bf, dict) and _read_brain_id_from_failure(
                bf, callsite="_no_data_rate",
            ) == brain_lc:
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
                bid = _read_brain_id_from_failure(
                    bf, callsite="_no_data_breakdown",
                )
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


def _arbiter_alignment(decisions: list[dict], brain_lc: str) -> dict:
    """P4 (2026-02-11): Brain Influence — arbiter alignment.

    Answers: "when this brain contributed an opinion to the council,
    how often did the arbiter pick it as the winner?"

        alignment_rate = wins / participated

    Where:
      * `participated` = # decisions in window whose `field[].brain`
        contains this brain (this brain was in the vote).
      * `wins` = subset of those where `winner_brain == this brain`.

    A brain with high alignment_rate is *materially influencing* the
    council — the arbiter is regularly siding with its read. A brain
    with high `no_data_rate` but non-zero alignment is punching above
    its weight (rare speaker but persuasive when it speaks). A brain
    with high participation but near-zero alignment is contributing
    diverse readings that the arbiter systematically discounts —
    which may be either "the brain is wrong" or "the brain is the
    consistent minority voice on the council" and the operator gets
    to interpret.

    Returned shape:
        {
          "participated": 148,   # count of decisions this brain was in
          "wins": 27,            # count where this brain won
          "alignment_rate": 0.1824,
        }

    Edge cases:
      * `participated == 0` → alignment_rate = None (not 0.0), so the
        tile can render "—" instead of a misleading 0%.
      * `decisions` empty → same as above (system had no arbitrations
        in the window; not a signal about this brain).
    """
    participated = 0
    wins = 0
    for doc in decisions:
        d = (doc or {}).get("decision") or {}
        # Was this brain in the field?
        field = d.get("field") or []
        in_field = any(
            isinstance(f, dict) and (f.get("brain") or "").lower() == brain_lc
            for f in field
        )
        if not in_field:
            continue
        participated += 1
        winner = (d.get("winner_brain") or "").lower()
        if winner == brain_lc:
            wins += 1
    if participated == 0:
        return {"participated": 0, "wins": 0, "alignment_rate": None}
    return {
        "participated": participated,
        "wins": wins,
        "alignment_rate": round(wins / participated, 4),
    }



async def _dissent_correctness(brain_lc: str, since: str) -> dict:
    """P3 (2026-02-11): informative-divergence / dissent correctness.

    Answers: "when this brain disagrees with its peers, is it usually
    right?" — the metric that supersedes raw distinctness in the
    long-run tile hierarchy. A brain that dissents and is validated
    by outcomes is a genuinely-differentiated council seat; a brain
    that dissents and is systematically wrong may be over-tuned.

    Method:
      1. Read this brain's directional opinions in the window.
      2. Read peer opinions in the window.
      3. For each self opinion, find concurrent peer opinions on the
         SAME topic. PREFERRED: exact `source_bar_close_at` equality
         (deterministic same-bar match). FALLBACK: ±15min time
         proximity when either side lacks the bar_close field.
      4. Compute peer majority direction. If self direction differs
         AND peer count ≥ 2 → this is a "dissent".
      5. Join the self opinion to its outcome via `opinion_id`.
      6. Correct = `outcome.actual == "win"`.

    Anchor-price contract (audited 2026-02-11, corrected 2026-02-11):
      * All opinions that carry `anchor_price` write it at post time
        via `shared.opinion_resolver._fetch_current_price`, which is
        the SAME function the grader uses at T+24h. P&L basis is
        consistent across all graded opinions.
      * Broker layer: routing is Webull (equity) + Kraken (crypto).
        No Alpaca client, no paper adapter, no fallback. Both `alpaca_paper`
        aliases and stale `ALPACA_INGEST_*` env vars were purged
        2026-02-19 per operator directive (LIVE ONLY).
      * Equity anchor coverage is limited by ARCHITECTURE, not a
        timeout: `observation_resolver._fetch_price` for equity
        calls `adapter.get_latest_trade()` (missing on Webull →
        AttributeError → fall through) then `adapter.list_positions()`
        and returns `current_price` from the position row. Symbols
        we DON'T already hold return None. So equity anchors only
        appear for symbols already in the Webull portfolio. Crypto
        anchors work everywhere via `_crypto_price_for` (Kraken
        public ticker, no position gate).
      * The `join_mix` field surfaces both join-path counts so
        operators can weight interpretation.

    Returned shape:
        {
          "resolved": 63,     # # resolved dissents
          "correct":  41,     # # of those where brain was validated
          "correctness_rate": 0.6508,
          "gathering_samples": False,  # True if resolved < 50
          "min_samples": 50,
        }

    Fail-soft: any DB timeout returns
    `{"resolved": 0, ..., "gathering_samples": True}`.
    """
    try:
        # 1. Self opinions in window — DIRECTIONAL only (long/short).
        #    That's all `opinion_resolver` grades, so the join is
        #    only meaningful on directional stances anyway.
        self_ops = await _safe_find(
            SHARED_BRAIN_OPINIONS,
            {
                "runtime": brain_lc,
                "stance": {"$in": ["long", "short"]},
                "posted_at": {"$gte": since},
            },
        )
        if not self_ops:
            return {
                "resolved": 0, "correct": 0,
                "correctness_rate": None,
                "gathering_samples": True,
                "min_samples": DISSENT_MIN_SAMPLES,
            }
        # 2. Peer opinions in the same window (all brains, all
        #    directional stances — we'll filter to non-self during
        #    the concurrency scan).
        peer_ops = await _safe_find(
            SHARED_BRAIN_OPINIONS,
            {
                "runtime": {"$ne": brain_lc},
                "stance": {"$in": ["long", "short"]},
                "posted_at": {"$gte": since},
            },
        )
        # Index peers by topic for the concurrency lookup.
        peers_by_topic: dict[str, list[dict]] = {}
        for op in peer_ops:
            t = op.get("topic")
            if t:
                peers_by_topic.setdefault(t, []).append(op)
        for t in peers_by_topic:
            peers_by_topic[t].sort(key=lambda x: x.get("posted_at") or "")

        # 3. For each self opinion, detect dissent + collect
        #    opinion_ids that qualify.
        #    2026-02-11 (P3 hardening): prefer `source_bar_close_at`
        #    equality join when available on BOTH sides — that's a
        #    deterministic "same bar" match. Fall back to ±15min time
        #    proximity only when either side lacks the bar_close
        #    field. Two counters track which join path fired so
        #    operators can see the mix during rollout.
        dissenting_opinion_ids: list[str] = []
        n_bar_close_matches = 0
        n_time_proximity_matches = 0
        for so in self_ops:
            topic = so.get("topic")
            self_stance = (so.get("stance") or "").lower()
            self_ts = _parse_ts(so.get("posted_at"))
            self_bar_close = _extract_source_bar_close(so)
            if not (topic and self_ts and self_stance in ("long", "short")):
                continue

            # Preferred path: exact source_bar_close_at match.
            peers_for_topic = peers_by_topic.get(topic, [])
            if self_bar_close:
                exact = [
                    p for p in peers_for_topic
                    if _extract_source_bar_close(p) == self_bar_close
                ]
                if len(exact) >= 2:
                    concurrent = exact
                    n_bar_close_matches += 1
                else:
                    concurrent = _concurrent_peers(
                        peers_for_topic, self_ts,
                        DISSENT_CONCURRENCY_WINDOW_SEC,
                    )
                    if len(concurrent) >= 2:
                        n_time_proximity_matches += 1
            else:
                concurrent = _concurrent_peers(
                    peers_for_topic, self_ts,
                    DISSENT_CONCURRENCY_WINDOW_SEC,
                )
                if len(concurrent) >= 2:
                    n_time_proximity_matches += 1

            if len(concurrent) < 2:
                continue  # not enough peer signal to establish majority
            peer_stances = [
                (p.get("stance") or "").lower() for p in concurrent
            ]
            majority = _majority_direction(peer_stances)
            if majority is None:
                continue  # peers themselves split — no dissent to grade
            if self_stance != majority:
                op_id = so.get("opinion_id")
                if op_id:
                    dissenting_opinion_ids.append(op_id)

        if not dissenting_opinion_ids:
            return {
                "resolved": 0, "correct": 0,
                "correctness_rate": None,
                "gathering_samples": True,
                "min_samples": DISSENT_MIN_SAMPLES,
            }

        # 4. Join to outcomes.
        outcomes = await _safe_find(
            SHARED_BRAIN_OUTCOMES,
            {"opinion_id": {"$in": dissenting_opinion_ids}},
        )
        resolved = 0
        correct = 0
        for out in outcomes:
            actual = (out.get("actual") or "").lower()
            if actual not in ("win", "loss", "no-event"):
                continue
            resolved += 1
            if actual == "win":
                correct += 1

        if resolved == 0:
            return {
                "resolved": 0, "correct": 0,
                "correctness_rate": None,
                "gathering_samples": True,
                "min_samples": DISSENT_MIN_SAMPLES,
            }

        return {
            "resolved": resolved,
            "correct": correct,
            "correctness_rate": round(correct / resolved, 4)
                if resolved >= DISSENT_MIN_SAMPLES else None,
            "gathering_samples": resolved < DISSENT_MIN_SAMPLES,
            "min_samples": DISSENT_MIN_SAMPLES,
            # P3 hardening (2026-02-11): join-mix telemetry so
            # operators can see how many dissents were matched via
            # the deterministic `source_bar_close_at` path vs. the
            # ±15min time-proximity fallback. High fallback share =
            # metric is trustworthy only to the degree bar_close is
            # actually plumbed through by the brain writers.
            "join_mix": {
                "bar_close_equality": n_bar_close_matches,
                "time_proximity_fallback": n_time_proximity_matches,
            },
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("_dissent_correctness failed brain=%s: %s", brain_lc, exc)
        return {
            "resolved": 0, "correct": 0,
            "correctness_rate": None,
            "gathering_samples": True,
            "min_samples": DISSENT_MIN_SAMPLES,
            "error": str(exc)[:200],
        }


def _concurrent_peers(
    sorted_peers: list[dict],
    self_ts: datetime,
    window_sec: int,
) -> list[dict]:
    """Return peer opinions posted within ±`window_sec` of `self_ts`.
    Sorted-input assumption lets us do a linear scan; the peer list
    per topic is bounded by 3 (three peer brains) × pulse count."""
    concurrent = []
    for p in sorted_peers:
        p_ts = _parse_ts(p.get("posted_at"))
        if p_ts is None:
            continue
        delta = abs((p_ts - self_ts).total_seconds())
        if delta <= window_sec:
            concurrent.append(p)
    return concurrent


def _majority_direction(stances: list[str]) -> Optional[str]:
    """Simple plurality: if long > short, return long; if short > long,
    return short. Ties (equal counts) return None — no majority to
    dissent against."""
    n_long = sum(1 for s in stances if s == "long")
    n_short = sum(1 for s in stances if s == "short")
    if n_long > n_short:
        return "long"
    if n_short > n_long:
        return "short"
    return None


def _extract_source_bar_close(opinion: dict) -> Optional[str]:
    """P3 hardening (2026-02-11): fetch `source_bar_close_at` from
    an opinion, checking BOTH the top-level and `evidence` sub-doc
    (writers have historically stamped it in either place).
    Returns None when absent — the dissent join then falls back to
    time-proximity matching."""
    top = opinion.get("source_bar_close_at")
    if top:
        return str(top)
    ev = opinion.get("evidence") or {}
    if isinstance(ev, dict):
        v = ev.get("source_bar_close_at")
        if v:
            return str(v)
    return None




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
                bid = _read_brain_id_from_failure(bf, callsite="_exception_rate")
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

async def _safe_find(
    collection: str, query: dict,
    projection: Optional[dict] = None,
) -> list[dict]:
    """Bounded read + fail-soft. Returns empty list on timeout."""
    try:
        cur = db[collection].find(query, projection) if projection else db[collection].find(query)
        return await cur.max_time_ms(2500).to_list(20000)
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
            "arbiter_alignment": health.get("arbiter_alignment", {}),
            "duplicate_opinion_rate": health.get("duplicate_opinion_rate", 0.0),
            "latest_source_bar_at": health.get("latest_source_bar_at"),
            "pulse_lag_ms": health.get("pulse_lag_ms"),
            "distinctness": health.get("distinctness", {}),
            # P2 (2026-02-11): regime stamped AT SNAPSHOT TIME so
            # historical slicing "distinctness during choppy vs bull"
            # is possible without reprocessing.
            "market_regime": health.get("market_regime"),
            # P3 (2026-02-11): dissent correctness — sample-gated;
            # when `gathering_samples=True` the tile renders a
            # placeholder instead of a misleading rate.
            "dissent_correctness": health.get("dissent_correctness", {}),
        }
        await db[MC_PULSE_HEALTH_SNAPSHOTS].insert_one(dict(doc))
        return doc
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "take_pulse_health_snapshot failed brain=%s: %s", brain_id, exc,
        )
        return {}


# ─────────────── routes ───────────────

@router.get("/ticks")
async def get_recent_ticks(
    limit: int = Query(20, ge=1, le=100),
    _user: dict = Depends(get_current_user),
) -> dict:
    """Recent pulse ticks — the single most operator-useful readout
    for verifying the pulse → arbiter → intent loop is closed.

    Returns the last `limit` pulses (most recent first) with the
    exact fields the operator needs to diagnose silence at a glance:
    started_at, snapshot_count, brains_completed count,
    arbitrations_completed, intents_emitted, runtime_mode,
    orchestration_ok, overrun.

    Non-zero `intents_emitted` on a tick means the loop is closed
    end-to-end (envelope → seat → arbiter → intent → shared_intents).
    Zero for extended stretches while brains are completing means
    the arbiter is DISARMED or brains are all_flat — check the
    runtime_mode column.

    NOTE: registered BEFORE `/{brain_id}` so FastAPI doesn't treat
    "ticks" as a brain identifier.
    """
    # Bounded query — sort on `started_at` on mc_pulses can be
    # expensive if the collection is large. 2s ceiling means the
    # operator sees "no recent ticks" rather than a 25s wait when
    # Atlas is degraded.
    try:
        rows = await db[MC_PULSES].find(
            {},
            projection={
                "_id": 0,
                "pulse_id": 1,
                "started_at": 1,
                "completed_at": 1,
                "runtime_mode": 1,
                "snapshot_count": 1,
                "brains_completed": 1,
                "brains_failed": 1,
                "arbitrations_completed": 1,
                "intents_emitted": 1,
                "orchestration_ok": 1,
                "overrun": 1,
                "orchestration_error": 1,
            },
            sort=[("started_at", -1)],
        ).max_time_ms(2000).to_list(limit)
    except Exception as exc:  # noqa: BLE001
        logger.warning("get_recent_ticks read failed: %s", exc)
        rows = []

    for r in rows:
        bc = r.get("brains_completed") or []
        bf = r.get("brains_failed") or []
        r["brains_completed_count"] = len(bc) if isinstance(bc, list) else 0
        r["brains_failed_count"] = len(bf) if isinstance(bf, list) else 0
        r["brains_completed"] = bc if isinstance(bc, list) else []
        r["brains_failed"] = bf if isinstance(bf, list) else []

    return {
        "ok": True,
        "count": len(rows),
        "ticks": rows,
    }


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



@router.get("/{brain_id}/by-regime")
async def pulse_health_by_regime(
    brain_id: str,
    days: int = Query(7, ge=1, le=30),
    user: dict = Depends(get_current_user),
):
    """P2 (2026-02-11): regime-sliced distinctness + alignment.

    Reads `mc_pulse_health_snapshots` for this brain over the last
    N days, groups by `market_regime`, and returns the mean
    `distinctness.mean_symbol_disagreement` + mean
    `arbiter_alignment.alignment_rate` PER REGIME.

    This answers the operator's question: "is Barracuda actually
    contributing more in ranging markets, like its doctrine says
    it should?" Requires N ≥ 3 snapshots per regime before we
    report a mean — otherwise the sample is too small to trust.

    Response:
        {
          "brain": "camino",
          "days": 7,
          "by_regime": {
            "bull":    {"n": 128, "distinctness": 0.18, "alignment_rate": 0.32},
            "bear":    {"n": 12,  "distinctness": 0.24, "alignment_rate": 0.18},
            "choppy":  {"n": 44,  "distinctness": 0.35, "alignment_rate": 0.29},
            "unknown": {"n":  6,  "distinctness": null, "alignment_rate": null},
          }
        }
    """
    brain_lc = brain_id.strip().lower()
    since = (
        datetime.now(timezone.utc) - timedelta(days=days)
    ).isoformat()
    rows = await _safe_find(
        MC_PULSE_HEALTH_SNAPSHOTS,
        {"brain": brain_lc, "at": {"$gte": since}},
    )

    # Group by regime bucket.
    buckets: dict[str, list[dict]] = {}
    for r in rows:
        regime = r.get("market_regime") or "unknown"
        buckets.setdefault(regime, []).append(r)

    def _mean(values: list[float]) -> Optional[float]:
        vals = [v for v in values if v is not None]
        if not vals:
            return None
        return round(sum(vals) / len(vals), 4)

    MIN_SAMPLES = 3
    by_regime = {}
    for regime, snaps in buckets.items():
        # `distinctness` is a dict; the headline metric is
        # `distinctness` inside the dict (the recall vs peers). Fall
        # back to the top-level scalar for older snapshots that
        # didn't nest it.
        dist_vals = []
        align_vals = []
        for s in snaps:
            d = s.get("distinctness") or {}
            if isinstance(d, dict):
                dv = d.get("distinctness")
                if dv is None:
                    # Legacy field name from earlier iterations.
                    dv = d.get("mean_symbol_disagreement")
                if dv is not None:
                    dist_vals.append(dv)
            elif isinstance(d, (int, float)):
                dist_vals.append(float(d))
            a = s.get("arbiter_alignment") or {}
            if isinstance(a, dict):
                av = a.get("alignment_rate")
                if av is not None:
                    align_vals.append(av)
        by_regime[regime] = {
            "n": len(snaps),
            "distinctness": _mean(dist_vals) if len(snaps) >= MIN_SAMPLES else None,
            "alignment_rate": _mean(align_vals) if len(snaps) >= MIN_SAMPLES else None,
            "insufficient_samples": len(snaps) < MIN_SAMPLES,
        }
    return {
        "brain": brain_lc,
        "days": days,
        "min_samples_per_regime": MIN_SAMPLES,
        "by_regime": by_regime,
    }


@router.post("/e2e-trace")
async def post_e2e_trace(
    symbol: str = Query("AAPL"),
    lane: str = Query("equity"),
    user: dict = Depends(get_current_user),
):
    """Run a controlled end-to-end execution trace with a mocked
    broker. Answers the operator's diagnostic question: "which link
    in the pulse → arbiter → intent → router → broker chain is
    currently broken?" via `broke_at` + `next_expected`.

    Safety:
      * Broker layer ALWAYS mocked from this endpoint. Live-broker
        runs require setting `E2E_TRACE_ALLOW_LIVE_BROKER=1` AND
        invoking the trace from a shell (never from HTTP).
      * All trace rows are tagged with a unique `trace_id` and
        cleaned up automatically after the trace returns.

    Requires admin auth (via `get_current_user`).
    """
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="admin only")
    from mc_pulse.e2e_trace import run_e2e_trace
    result = await run_e2e_trace(
        symbol=symbol, lane=lane,
        broker_mock=True, cleanup=True,
    )
    return result.to_dict()



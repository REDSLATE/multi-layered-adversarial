"""Regression tests for `shared/brain_metrics.py` — the five
operator-tracked KPIs.

Doctrine pins:
  * Entropy normalized to [0, 1]. Uniform → 1.0. Single action → 0.0.
  * v3 HOLD-equivalents (WATCH/DEFER/ABSTAIN) counted DISTINCTLY
    from v2 HOLD — but combined surfaces both.
  * Probability spread skips buckets with <2 brains. Single-brain
    bucket = no disagreement signal.
  * Reason-code distribution emits BOTH gate_state and final_reason
    leaderboards (they're complementary, not redundant).
  * Lane decisions split by lane, with v3 plan.intent preferred over
    v2 action when present.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone, timedelta

import pytest

from shared.brain_metrics import (
    count_holds,
    entropy_average,
    lane_specific_decisions,
    probability_spread,
    reason_code_distribution,
    _bucket_ts,
    PROBABILITY_SPREAD_BUCKET_SECONDS,
)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _intent(
    intent_id: str = "i1",
    brain: str = "camino",
    lane: str = "equity",
    action: str = "BUY",
    symbol: str = "AAPL",
    confidence: float = 0.6,
    ingest_ts: str | None = None,
    plan: dict | None = None,
    gate_state: str = "emitted",
):
    if ingest_ts is None:
        ingest_ts = _iso(datetime.now(timezone.utc))
    out = {
        "intent_id": intent_id,
        "stack": brain,
        "lane": lane,
        "action": action,
        "symbol": symbol,
        "confidence": confidence,
        "ingest_ts": ingest_ts,
        "gate_state": gate_state,
    }
    if plan is not None:
        out["plan"] = plan
    return out


# ── 1. HOLD count ───────────────────────────────────────────────────
class TestCountHolds:
    def test_pure_v2_holds(self):
        intents = [
            _intent("a", "camino", action="HOLD"),
            _intent("b", "camino", action="HOLD"),
            _intent("c", "barracuda", action="BUY"),
        ]
        out = count_holds(intents)
        assert out["v2_hold"] == 2
        assert out["v3_total"] == 0
        assert out["combined"] == 2
        assert out["by_brain"]["camino"]["v2_hold"] == 2
        assert out["by_brain"]["camino"]["combined"] == 2
        assert "barracuda" not in out["by_brain"]  # never had a HOLD

    def test_v3_watch_defer_abstain_split(self):
        intents = [
            _intent("a", "camino", plan={"intent": "WATCH"}),
            _intent("b", "camino", plan={"intent": "DEFER"}),
            _intent("c", "barracuda", plan={"intent": "ABSTAIN"}),
            _intent("d", "camino", plan={"intent": "ENTER"}),
        ]
        out = count_holds(intents)
        assert out["v3_watch"] == 1
        assert out["v3_defer"] == 1
        assert out["v3_abstain"] == 1
        assert out["v3_total"] == 3
        assert out["combined"] == 3
        assert out["by_brain"]["camino"]["combined"] == 2

    def test_mixed_v2_and_v3(self):
        intents = [
            _intent("a", "camino", action="HOLD"),
            _intent("b", "camino", action="BUY",
                    plan={"intent": "WATCH"}),  # plan.intent wins, no double-count
            _intent("c", "barracuda", action="HOLD",
                    plan={"intent": "ABSTAIN"}),  # counted under BOTH (audit honesty)
        ]
        out = count_holds(intents)
        # v2_hold counts plain HOLD actions; v3 counts plan.intent.
        # An intent stamping BOTH gets counted in BOTH columns — by
        # design, the operator wants to see the v2→v3 transition explicitly.
        assert out["v2_hold"] == 2
        assert out["v3_total"] == 2  # WATCH + ABSTAIN
        assert out["combined"] == 4

    def test_unknown_action_ignored(self):
        intents = [_intent("a", action="WAIT_FOR_TRIGGER")]
        out = count_holds(intents)
        assert out["combined"] == 0


# ── 2. Entropy average ──────────────────────────────────────────────
class TestEntropyAverage:
    def test_single_action_is_zero_entropy(self):
        intents = [
            _intent("a", "camino", action="BUY"),
            _intent("b", "camino", action="BUY"),
            _intent("c", "camino", action="BUY"),
        ]
        out = entropy_average(intents)
        assert out["per_brain"]["camino"]["entropy"] == 0.0
        assert out["mean_across_brains"] == 0.0

    def test_uniform_distribution_is_one(self):
        # Two distinct actions, exactly balanced → entropy / log2(2) = 1.0
        intents = [
            _intent("a", "camino", action="BUY"),
            _intent("b", "camino", action="SELL"),
        ]
        out = entropy_average(intents)
        assert out["global_action_cardinality"] == 2
        assert math.isclose(out["per_brain"]["camino"]["entropy"], 1.0, abs_tol=1e-6)
        assert math.isclose(out["mean_across_brains"], 1.0, abs_tol=1e-6)

    def test_skewed_between_zero_and_one(self):
        # 3 BUYs + 1 SELL — non-uniform but non-zero entropy.
        intents = [
            _intent("a", "camino", action="BUY"),
            _intent("b", "camino", action="BUY"),
            _intent("c", "camino", action="BUY"),
            _intent("d", "camino", action="SELL"),
        ]
        out = entropy_average(intents)
        h = out["per_brain"]["camino"]["entropy"]
        assert 0.0 < h < 1.0
        # Expected: H = -(0.75*log2(0.75) + 0.25*log2(0.25)) / log2(2)
        # = 0.811...
        assert math.isclose(h, 0.8113, abs_tol=1e-3)

    def test_mean_across_brains(self):
        # Brain A: pure BUY → 0.0. Brain B: balanced → 1.0. Mean → 0.5.
        intents = [
            _intent("a", "camino", action="BUY"),
            _intent("b", "barracuda", action="BUY"),
            _intent("c", "barracuda", action="SELL"),
        ]
        out = entropy_average(intents)
        assert math.isclose(out["mean_across_brains"], 0.5, abs_tol=1e-3)

    def test_v3_plan_intent_preferred(self):
        # v3 plan.intent should be used when present (so WATCH counts
        # toward the brain's action diversity).
        intents = [
            _intent("a", "camino", action="HOLD",
                    plan={"intent": "WATCH"}),
            _intent("b", "camino", action="HOLD",
                    plan={"intent": "WAIT_FOR_TRIGGER"}),
        ]
        out = entropy_average(intents)
        # With WATCH and WAIT_FOR_TRIGGER as the two emitted decisions,
        # cardinality must be 2 (not 1 collapsed into HOLD).
        assert out["global_action_cardinality"] == 2
        assert math.isclose(out["per_brain"]["camino"]["entropy"], 1.0, abs_tol=1e-6)

    def test_empty_input(self):
        out = entropy_average([])
        assert out["mean_across_brains"] is None
        assert out["per_brain"] == {}


# ── 3. Reason-code distribution ─────────────────────────────────────
class TestReasonCodeDistribution:
    def test_ranks_top_n(self):
        intents = [
            _intent("a", gate_state="blocked"),
            _intent("b", gate_state="blocked"),
            _intent("c", gate_state="blocked"),
            _intent("d", gate_state="no_trade"),
            _intent("e", gate_state="advisory_only"),
        ]
        out = reason_code_distribution(intents, receipts_by_id={}, top_n=15)
        # blocked is most common.
        assert out["top_gate_states"][0]["reason"] == "blocked"
        assert out["top_gate_states"][0]["count"] == 3
        assert math.isclose(
            out["top_gate_states"][0]["pct_of_total"], 60.0, abs_tol=0.1
        )

    def test_top_n_truncation(self):
        intents = [_intent(f"i{n}", gate_state=f"g{n}") for n in range(20)]
        out = reason_code_distribution(intents, receipts_by_id={}, top_n=5)
        assert len(out["top_gate_states"]) == 5

    def test_pulls_final_reasons_from_receipts(self):
        intents = [
            _intent("a", gate_state="blocked"),
            _intent("b", gate_state="blocked"),
        ]
        receipts = {
            "a": {"final_reason": "ROADGUARD_SPREAD_FLOOR"},
            "b": {"final_reason": "WEBULL_NOTIONAL_ABOVE_CAP"},
        }
        out = reason_code_distribution(intents, receipts)
        reasons = {r["reason"] for r in out["top_final_reasons"]}
        assert "roadguard_spread_floor" in reasons
        assert "webull_notional_above_cap" in reasons

    def test_empty_input_safe(self):
        out = reason_code_distribution([], {}, top_n=5)
        assert out["top_gate_states"] == []
        assert out["total_intents"] == 0


# ── 4. Lane-specific decisions ──────────────────────────────────────
class TestLaneSpecificDecisions:
    def test_splits_by_lane(self):
        intents = [
            _intent("a", lane="equity", action="BUY"),
            _intent("b", lane="equity", action="BUY"),
            _intent("c", lane="equity", action="HOLD"),
            _intent("d", lane="crypto", action="SELL"),
        ]
        out = lane_specific_decisions(intents)
        assert out["equity"]["BUY"] == 2
        assert out["equity"]["HOLD"] == 1
        assert out["equity"]["total"] == 3
        assert out["crypto"]["SELL"] == 1
        assert out["crypto"]["total"] == 1

    def test_v3_plan_intent_preferred_over_v2_action(self):
        intents = [
            _intent("a", lane="equity", action="HOLD",
                    plan={"intent": "WATCH"}),
        ]
        out = lane_specific_decisions(intents)
        # Should record WATCH (the v3 decision), not HOLD.
        assert out["equity"].get("WATCH") == 1
        assert "HOLD" not in out["equity"]


# ── 5. Probability spread ───────────────────────────────────────────
class TestProbabilitySpread:
    def test_single_brain_bucket_excluded(self):
        ts = _iso(datetime(2026, 2, 24, 13, 30, tzinfo=timezone.utc))
        intents = [
            _intent("a", "camino", symbol="AAPL", confidence=0.7,
                    ingest_ts=ts),
        ]
        out = probability_spread(intents)
        # 1 bucket total (1 brain) but 0 disagreement buckets.
        assert out["n_total_buckets"] == 1
        assert out["n_disagreement_buckets"] == 0
        assert out["mean_spread"] is None

    def test_multi_brain_spread_computed(self):
        ts = _iso(datetime(2026, 2, 24, 13, 30, tzinfo=timezone.utc))
        intents = [
            _intent("a", "camino", symbol="AAPL", confidence=0.7,
                    ingest_ts=ts),
            _intent("b", "barracuda", symbol="AAPL", confidence=0.4,
                    ingest_ts=ts),
            _intent("c", "hellcat", symbol="AAPL", confidence=0.5,
                    ingest_ts=ts),
        ]
        out = probability_spread(intents)
        assert out["n_disagreement_buckets"] == 1
        # 0.7 - 0.4 = 0.3
        assert math.isclose(out["max_spread"], 0.3, abs_tol=1e-6)
        assert math.isclose(out["mean_spread"], 0.3, abs_tol=1e-6)

    def test_separate_symbols_separate_buckets(self):
        ts = _iso(datetime(2026, 2, 24, 13, 30, tzinfo=timezone.utc))
        intents = [
            _intent("a", "camino", symbol="AAPL", confidence=0.7,
                    ingest_ts=ts),
            _intent("b", "barracuda", symbol="AAPL", confidence=0.4,
                    ingest_ts=ts),
            _intent("c", "camino", symbol="MSFT", confidence=0.9,
                    ingest_ts=ts),
            _intent("d", "barracuda", symbol="MSFT", confidence=0.1,
                    ingest_ts=ts),
        ]
        out = probability_spread(intents)
        # Two separate disagreement buckets, one per symbol.
        assert out["n_disagreement_buckets"] == 2
        # AAPL spread 0.3, MSFT spread 0.8 → mean 0.55, max 0.8
        assert math.isclose(out["max_spread"], 0.8, abs_tol=1e-6)
        assert math.isclose(out["mean_spread"], 0.55, abs_tol=1e-6)

    def test_different_hours_separate_buckets(self):
        ts_h1 = _iso(datetime(2026, 2, 24, 13, 30, tzinfo=timezone.utc))
        ts_h2 = _iso(datetime(2026, 2, 24, 14, 30, tzinfo=timezone.utc))
        intents = [
            _intent("a", "camino", symbol="AAPL", confidence=0.7,
                    ingest_ts=ts_h1),
            _intent("b", "barracuda", symbol="AAPL", confidence=0.4,
                    ingest_ts=ts_h2),  # different hour
        ]
        out = probability_spread(intents)
        # Two single-brain buckets (different hours) → no disagreement.
        assert out["n_total_buckets"] == 2
        assert out["n_disagreement_buckets"] == 0

    def test_top_disagreement_ordered_widest_first(self):
        ts = _iso(datetime(2026, 2, 24, 13, 30, tzinfo=timezone.utc))
        intents = [
            _intent("a", "camino", symbol="AAPL", confidence=0.8,
                    ingest_ts=ts),
            _intent("b", "barracuda", symbol="AAPL", confidence=0.7,
                    ingest_ts=ts),
            _intent("c", "camino", symbol="MSFT", confidence=0.9,
                    ingest_ts=ts),
            _intent("d", "barracuda", symbol="MSFT", confidence=0.1,
                    ingest_ts=ts),
        ]
        out = probability_spread(intents)
        top = out["top_disagreement"]
        assert top[0]["symbol"] == "MSFT"
        assert top[0]["spread"] > top[1]["spread"]

    def test_invalid_ts_skipped(self):
        intents = [
            _intent("a", "camino", symbol="AAPL", confidence=0.5,
                    ingest_ts="not-a-date"),
        ]
        out = probability_spread(intents)
        assert out["n_total_buckets"] == 0

    def test_bucket_ts_helper_hour_alignment(self):
        # 13:30 UTC should bucket to 13:00 UTC (start of hour).
        ts = _iso(datetime(2026, 2, 24, 13, 30, tzinfo=timezone.utc))
        bucket = _bucket_ts(ts, PROBABILITY_SPREAD_BUCKET_SECONDS)
        expected = int(datetime(2026, 2, 24, 13, 0,
                                tzinfo=timezone.utc).timestamp())
        assert bucket == expected

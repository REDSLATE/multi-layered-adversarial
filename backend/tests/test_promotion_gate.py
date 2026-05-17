"""Tests for the Bounded Promotion Gate (expectancy-driven, seat-doctrinal).

Doctrine pin: expectancy > accuracy. A 45% / 4.5R doctrine outperforms
a 75% / 0.8R doctrine — these tests assert the gate logic respects
that. Promotion targets `(lane, doctrine_version)`; brain identity
never enters the verdict.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

from pymongo import MongoClient

from tests.conftest import BASE_URL


DOCTRINE_SIDECARS = "doctrine_sidecars"


def _db():
    return MongoClient(os.environ["MONGO_URL"])[os.environ["DB_NAME"]]


def _row(*, intent_id, lane, doctrine_version, pnl, ts):
    return {
        "intent_id": intent_id,
        "lane": lane,
        "doctrine_version": doctrine_version,
        "quality": "B_QUALITY",
        "stack": "alpha",
        "symbol": "TEST",
        "action": "BUY",
        "governor_action": "modulate",
        "adversary_challenge_required": False,
        "execution_judge_ready": True,
        "governor_holder": "chevelle",
        "adversary_holder": "redeye",
        "execution_judge_holder": "alpha",
        "strategist_holder": "camaro",
        "ts": ts,
        "outcome_join": {
            "joined_at": ts,
            "outcome_label": "win" if pnl > 0 else "loss",
            "pnl_usd": float(pnl),
        },
    }


def _seed(rows):
    _db()[DOCTRINE_SIDECARS].insert_many([dict(r) for r in rows])


def _cleanup(prefix):
    _db()[DOCTRINE_SIDECARS].delete_many(
        {"intent_id": {"$regex": f"^{prefix}"}},
    )


# ─── pure-math helpers (no DB) ──────────────────────────────────────

def test_expectancy_math_45pct_with_45R_outperforms_75pct_with_08R():
    """Doctrine pin: expectancy wins, not accuracy."""
    from shared.doctrine.promotion import _compute_expectancy_and_drawdown
    # 45% win rate at 4.5R: 45 wins of $450, 55 losses of $100
    pnls_a = [450.0] * 45 + [-100.0] * 55
    exp_a, _, wr_a, _, _, _ = _compute_expectancy_and_drawdown(pnls_a)
    # 75% win rate at 0.8R: 75 wins of $80, 25 losses of $100
    pnls_b = [80.0] * 75 + [-100.0] * 25
    exp_b, _, wr_b, _, _, _ = _compute_expectancy_and_drawdown(pnls_b)
    assert wr_a < wr_b  # B has better accuracy
    assert exp_a > exp_b  # but A has better expectancy
    assert exp_a > 1.0  # A is a 1.475R/trade doctrine — strong


def test_drawdown_counts_consecutive_losses():
    from shared.doctrine.promotion import _compute_expectancy_and_drawdown
    pnls = [100.0, -100.0, -100.0, -100.0, 100.0, -100.0]
    _, dd, _, _, _, _ = _compute_expectancy_and_drawdown(pnls)
    assert dd == 3.0  # three consecutive -1R losses


def test_consistency_score_high_when_stable():
    from shared.doctrine.promotion import _compute_consistency
    # 50 wins followed by 50 losses → wildly different rolling WR → low
    swing = [100.0] * 50 + [-100.0] * 50
    score_swing = _compute_consistency(swing, window=30)
    # 50/50 evenly interleaved → stable rolling WR → high consistency
    even = []
    for _ in range(50):
        even.append(100.0)
        even.append(-100.0)
    score_even = _compute_consistency(even, window=30)
    assert score_even > score_swing


def test_consistency_returns_none_below_window():
    from shared.doctrine.promotion import _compute_consistency
    assert _compute_consistency([100.0] * 10, window=30) is None


# ─── verdict bands ───────────────────────────────────────────────────

def test_verdict_learning_below_100_samples():
    from shared.doctrine.promotion import _verdict
    v, blockers = _verdict(samples=50, expectancy_R=2.0,
                           max_drawdown_R=1.0, consistency=0.9)
    assert v == "LEARNING"
    assert any("100" in b for b in blockers)


def test_verdict_candidate_retirement_on_negative_expectancy():
    from shared.doctrine.promotion import _verdict
    v, reasons = _verdict(samples=120, expectancy_R=-0.30,
                          max_drawdown_R=2.0, consistency=0.8)
    assert v == "CANDIDATE_RETIREMENT"
    assert any("expectancy" in r for r in reasons)


def test_verdict_candidate_retirement_on_catastrophic_drawdown():
    from shared.doctrine.promotion import _verdict
    v, reasons = _verdict(samples=120, expectancy_R=0.50,
                          max_drawdown_R=9.0, consistency=0.8)
    assert v == "CANDIDATE_RETIREMENT"
    assert any("drawdown" in r for r in reasons)


def test_verdict_candidate_promotion_on_strong_doctrine():
    from shared.doctrine.promotion import _verdict
    v, blockers = _verdict(samples=150, expectancy_R=0.45,
                           max_drawdown_R=3.5, consistency=0.65)
    assert v == "CANDIDATE_PROMOTION"
    assert blockers == []


def test_verdict_watching_when_neither_promote_nor_retire():
    from shared.doctrine.promotion import _verdict
    v, blockers = _verdict(samples=150, expectancy_R=0.15,
                           max_drawdown_R=3.0, consistency=0.70)
    assert v == "WATCHING"
    # should explain why it's NOT a promotion
    assert any("expectancy" in b for b in blockers)


# ─── endpoint shape ──────────────────────────────────────────────────

def test_promotion_status_endpoint_returns_known_doctrines(auth_client):
    r = auth_client.get(f"{BASE_URL}/api/admin/doctrine/promotion-status")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "slices" in body
    assert "thresholds" in body
    assert body["thresholds"]["min_samples"] == 100
    assert body["endpoint_version"] == "promotion_status_v1_expectancy_driven"
    # Even zero-sample doctrines surface so the UI can show "0/100".
    dvs = {s["doctrine_version"] for s in body["slices"]}
    assert "small_account_sidecar_v1" in dvs
    assert "gap_and_go_v1" in dvs
    assert "micro_pullback_v1" in dvs


def test_promotion_status_requires_auth(api_client):
    r = api_client.get(f"{BASE_URL}/api/admin/doctrine/promotion-status")
    assert r.status_code in (401, 403)


def test_promotion_status_emits_promotion_candidate_when_doctrine_clears_gates(auth_client):
    """End-to-end: seed 100+ winning samples → endpoint emits
    CANDIDATE_PROMOTION verdict for that doctrine_version."""
    prefix = f"prom-go-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    # 60% wins × 2R + 40% losses × 1R → expectancy ≈ 0.80R per trade
    base_ts = datetime.now(timezone.utc)
    rows = []
    # interleave so rolling WR is stable
    for i in range(150):
        if i % 5 < 3:  # 60% wins
            pnl = 200.0
        else:
            pnl = -100.0
        rows.append(_row(
            intent_id=f"{prefix}-{i}", lane="equity",
            doctrine_version="prom_test_v1", pnl=pnl,
            ts=(base_ts + timedelta(minutes=i)).isoformat(),
        ))
    _seed(rows)
    try:
        r = auth_client.get(
            f"{BASE_URL}/api/admin/doctrine/promotion-status",
            params={"lane": "equity"},
        )
        body = r.json()
        slc = next(
            (s for s in body["slices"]
             if s["doctrine_version"] == "prom_test_v1"),
            None,
        )
        assert slc is not None, body
        assert slc["samples"] == 150
        assert slc["verdict"] == "CANDIDATE_PROMOTION", slc
        assert slc["expectancy_R"] >= 0.30
    finally:
        _cleanup(prefix)


def test_promotion_status_emits_retirement_candidate_when_doctrine_fails(auth_client):
    """Seed 100+ losing samples → endpoint emits CANDIDATE_RETIREMENT."""
    prefix = f"prom-bad-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    base_ts = datetime.now(timezone.utc)
    rows = []
    # 30% wins × 1R + 70% losses × 1R → expectancy ≈ -0.40R
    for i in range(120):
        pnl = 100.0 if i % 10 < 3 else -100.0
        rows.append(_row(
            intent_id=f"{prefix}-{i}", lane="equity",
            doctrine_version="prom_bad_test_v1", pnl=pnl,
            ts=(base_ts + timedelta(minutes=i)).isoformat(),
        ))
    _seed(rows)
    try:
        r = auth_client.get(
            f"{BASE_URL}/api/admin/doctrine/promotion-status",
            params={"lane": "equity"},
        )
        body = r.json()
        slc = next(
            (s for s in body["slices"]
             if s["doctrine_version"] == "prom_bad_test_v1"),
            None,
        )
        assert slc is not None, body
        assert slc["verdict"] == "CANDIDATE_RETIREMENT"
        assert slc["expectancy_R"] < -0.10
    finally:
        _cleanup(prefix)


def test_promotion_status_zero_sample_doctrines_render_learning(auth_client):
    r = auth_client.get(
        f"{BASE_URL}/api/admin/doctrine/promotion-status",
        params={"lane": "equity"},
    )
    body = r.json()
    # The strategy doctrines are seeded zero in tests — should show LEARNING.
    gap = next(
        (s for s in body["slices"]
         if s["doctrine_version"] == "gap_and_go_v1"),
        None,
    )
    assert gap is not None
    if gap["samples"] == 0:
        assert gap["verdict"] == "LEARNING"
        assert gap["progress_to_min_samples"] == 0.0
        # Onboarding payload present
        assert gap["ideal"]["title"] == "Gap-and-Go v1"
        assert any("STRONG_GAPPER" in w or "premarket" in w for w in gap["ideal"]["wants"])

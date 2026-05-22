"""Tripwires for Phase 2 (Observation Resolver) + Phase 3 (Learning Ladder).

Doctrine pin (2026-02-18):
    Phase 2: a background worker grades observation receipts against
    market price at +1h/+4h/+1d/+5d. Outcome is `win | loss | neutral`.
    `anchor_missing` is a structural failure mode that stops retry.

    Phase 3: a per-(brain, lane) stage tracker with promotion logic.
    Stages: observation_only → micro_paper → micro_live → normal_live.
    Operator may always promote / demote manually. Auto-promotion
    eligibility computed but NOT auto-triggered (capital-risk
    transitions must be deliberate).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


# ─── Phase 2: resolver grading math ─────────────────────────────────


@pytest.mark.tripwire
def test_sided_pnl_buy_uses_positive_direction():
    from shared.observation_resolver import _sided_pnl_pct
    pnl = _sided_pnl_pct("BUY", anchor=100.0, current=105.0)
    assert abs(pnl - 0.05) < 1e-9


@pytest.mark.tripwire
def test_sided_pnl_sell_inverts_direction():
    """SELL/SHORT win when price falls. The math must flip the sign."""
    from shared.observation_resolver import _sided_pnl_pct
    pnl = _sided_pnl_pct("SELL", anchor=100.0, current=95.0)
    assert abs(pnl - 0.05) < 1e-9, "SELL with price falling = +5% pnl"
    pnl2 = _sided_pnl_pct("SHORT", anchor=100.0, current=110.0)
    assert abs(pnl2 - (-0.10)) < 1e-9, "SHORT with price rising = -10% pnl"


@pytest.mark.tripwire
def test_outcome_classification_thresholds():
    from shared.observation_resolver import _classify_outcome
    # Crypto threshold = 2.0%
    assert _classify_outcome(0.025, "crypto") == "win"
    assert _classify_outcome(-0.025, "crypto") == "loss"
    assert _classify_outcome(0.005, "crypto") == "neutral"
    # Equity threshold = 1.0%
    assert _classify_outcome(0.015, "equity") == "win"
    assert _classify_outcome(-0.015, "equity") == "loss"
    assert _classify_outcome(0.005, "equity") == "neutral"


@pytest.mark.tripwire
async def test_resolver_marks_anchor_missing_as_resolved():
    """When anchor_price is missing the receipt must NOT loop forever —
    flip to resolved=True with diagnostic outcome 'anchor_missing'."""
    from shared.observation_resolver import _grade_receipt
    receipt = {
        "intent_id": "tw-resolver-anchor",
        "brain": "camaro", "lane": "crypto", "symbol": "BTC/USD",
        "side": "BUY",
        "anchor_price": None,
        "created_at": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
        "resolved": False,
        "horizon_prices": {},
    }
    update = await _grade_receipt(receipt)
    assert update is not None
    assert update["resolved"] is True
    assert update["outcome"] == "anchor_missing"


@pytest.mark.tripwire
def test_resolver_horizon_set_locked():
    """The horizon set is part of the doctrine. Locking the keys so
    the resolver can't silently change what gets graded."""
    from shared.observation_resolver import HORIZONS
    assert set(HORIZONS.keys()) == {"1h", "4h", "1d", "5d"}


# ─── Phase 3: ladder state + transitions ─────────────────────────────


@pytest.fixture(autouse=True)
async def _reset_ladder():
    """Clean ladder + audit state per test."""
    from db import db
    from namespaces import LEARNING_LADDER, LEARNING_LADDER_AUDIT
    await db[LEARNING_LADDER].delete_many({})
    await db[LEARNING_LADDER_AUDIT].delete_many({})
    yield
    await db[LEARNING_LADDER].delete_many({})
    await db[LEARNING_LADDER_AUDIT].delete_many({})


@pytest.mark.tripwire
async def test_ladder_defaults_observation_only():
    """A fresh (brain, lane) MUST start at the bottom rung."""
    from shared.learning_ladder import get_stage
    state = await get_stage("camaro", "crypto")
    assert state["stage"] == "observation_only"
    assert state["created"] is False  # row materialized in memory only


@pytest.mark.tripwire
def test_ladder_route_requires_auth(base_url):
    import requests
    r = requests.get(f"{base_url}/api/admin/learning-ladder", timeout=15)
    assert r.status_code in (401, 403)


@pytest.mark.tripwire
def test_ladder_lists_all_brain_lane_combinations(auth_client, base_url):
    """The list endpoint MUST return every (brain, lane) combo so the
    operator UI can render the full ladder grid without backfill."""
    r = auth_client.get(f"{base_url}/api/admin/learning-ladder", timeout=15)
    assert r.status_code == 200, r.text
    body = r.json()
    pairs = sorted((it["brain"], it["lane"]) for it in body["items"])
    expected_pairs = sorted([(b, l) for b in ("alpha", "camaro", "chevelle", "redeye")
                             for l in ("equity", "crypto")])
    assert pairs == expected_pairs


@pytest.mark.tripwire
def test_ladder_promote_advances_one_rung(auth_client, base_url):
    """Operator-forced promotion must advance exactly one rung."""
    r = auth_client.post(
        f"{base_url}/api/admin/learning-ladder/promote",
        json={"brain": "camaro", "lane": "crypto", "reason": "tripwire"},
        timeout=15,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["previous"] == "observation_only"
    assert body["current"] == "micro_paper"


@pytest.mark.tripwire
def test_ladder_demote_reverses_one_rung(auth_client, base_url):
    auth_client.post(
        f"{base_url}/api/admin/learning-ladder/promote",
        json={"brain": "camaro", "lane": "crypto", "reason": "set up"},
        timeout=15,
    )
    r = auth_client.post(
        f"{base_url}/api/admin/learning-ladder/demote",
        json={"brain": "camaro", "lane": "crypto", "reason": "safety pull"},
        timeout=15,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["previous"] == "micro_paper"
    assert body["current"] == "observation_only"


@pytest.mark.tripwire
def test_ladder_cannot_promote_past_top(auth_client, base_url):
    for _ in range(3):
        auth_client.post(
            f"{base_url}/api/admin/learning-ladder/promote",
            json={"brain": "camaro", "lane": "crypto", "reason": "climb"},
            timeout=15,
        )
    # 4th promote MUST 400.
    r = auth_client.post(
        f"{base_url}/api/admin/learning-ladder/promote",
        json={"brain": "camaro", "lane": "crypto", "reason": "overshoot"},
        timeout=15,
    )
    assert r.status_code == 400


@pytest.mark.tripwire
def test_ladder_cannot_demote_below_bottom(auth_client, base_url):
    r = auth_client.post(
        f"{base_url}/api/admin/learning-ladder/demote",
        json={"brain": "camaro", "lane": "crypto", "reason": "underflow"},
        timeout=15,
    )
    assert r.status_code == 400


@pytest.mark.tripwire
def test_ladder_history_records_transitions(auth_client, base_url):
    auth_client.post(
        f"{base_url}/api/admin/learning-ladder/promote",
        json={"brain": "camaro", "lane": "crypto", "reason": "promote-A"},
        timeout=15,
    )
    auth_client.post(
        f"{base_url}/api/admin/learning-ladder/demote",
        json={"brain": "camaro", "lane": "crypto", "reason": "demote-B"},
        timeout=15,
    )
    r = auth_client.get(
        f"{base_url}/api/admin/learning-ladder/history", timeout=15,
    )
    body = r.json()
    camaro_crypto = [
        h for h in body["items"]
        if h["brain"] == "camaro" and h["lane"] == "crypto"
    ]
    assert len(camaro_crypto) >= 2
    # Most-recent flip first (sort desc on ts).
    assert camaro_crypto[0]["next"] == "observation_only"


@pytest.mark.tripwire
async def test_obs_progress_thresholds_match_doctrine():
    """Doctrine pin: 100 resolved obs + win_rate > 0.55 = unlock.
    Locking the threshold constants so they can't drift silently."""
    from shared.learning_ladder import (
        OBS_UNLOCK_COUNT, OBS_UNLOCK_WIN_RATE,
        PAPER_UNLOCK_COUNT, PAPER_UNLOCK_EXPECTANCY_R,
    )
    assert OBS_UNLOCK_COUNT == 100
    assert OBS_UNLOCK_WIN_RATE == 0.55
    assert PAPER_UNLOCK_COUNT == 50
    assert PAPER_UNLOCK_EXPECTANCY_R == 0.30


@pytest.mark.tripwire
def test_ladder_unknown_brain_rejected(auth_client, base_url):
    r = auth_client.post(
        f"{base_url}/api/admin/learning-ladder/promote",
        json={"brain": "nonsense", "lane": "crypto", "reason": "x"},
        timeout=15,
    )
    assert r.status_code == 400

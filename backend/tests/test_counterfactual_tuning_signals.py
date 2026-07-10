"""Counterfactual tuning-signal tests — proposer + doctrine overlay integration.

Locks in operator doctrine (2026-02-19):
    * Groups by (blocked_reason, lane), scans resolved
      counterfactual signals via the specified horizon.
    * Emits RELAX_GATE when missed-win wilson-lower >= 0.60 AND
      shrunk avg bps >= +5.
    * Emits PRESERVE_GATE when correct-block wilson-lower >= 0.60
      AND shrunk avg bps <= -5.
    * Idempotent: re-runs update evidence, keep state (proposed by
      default; approved / rejected survive re-runs).
    * Doctrine overlay picks up approved signals and returns a
      clamped ±0.20 gate-threshold delta.

All tests are scoped to `_PFX` so we never touch production.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, "/app/backend")

from db import db  # noqa: E402
from namespaces import (  # noqa: E402
    COUNTERFACTUAL_SIGNALS, COUNTERFACTUAL_TUNING_SIGNALS,
)
from shared.counterfactuals.tuning_signals import (  # noqa: E402
    MIN_SAMPLE_SIZE, propose_tuning_signals,
)
from shared.learning import doctrine_overlay  # noqa: E402


_PFX = "cf-tune-test-"


def _iso(dt):
    return dt.isoformat()


def _now():
    return datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
async def _purge():
    # Signal seeds live in counterfactual_signals; tuning proposals
    # live in counterfactual_tuning_signals. Purge both by the
    # deterministic prefix — signals use `signal_id`, tuning docs
    # are grouped by (reason, lane) so we purge whole test reasons.
    await db[COUNTERFACTUAL_SIGNALS].delete_many(
        {"signal_id": {"$regex": f"^{_PFX}"}},
    )
    await db[COUNTERFACTUAL_TUNING_SIGNALS].delete_many(
        {"group.blocked_reason": {"$regex": f"^{_PFX}"}},
    )
    doctrine_overlay.reload_gate_cache_for_tests()
    yield
    await db[COUNTERFACTUAL_SIGNALS].delete_many(
        {"signal_id": {"$regex": f"^{_PFX}"}},
    )
    await db[COUNTERFACTUAL_TUNING_SIGNALS].delete_many(
        {"group.blocked_reason": {"$regex": f"^{_PFX}"}},
    )
    doctrine_overlay.reload_gate_cache_for_tests()


def _sig(idx, *, verdict, bps, blocked_reason, lane="equity", horizon="15m"):
    """Seed a single resolved counterfactual signal row."""
    sid = f"{_PFX}{idx:04d}"
    return {
        "signal_id": sid,
        "source_intent_id": sid,
        "brain": "camino",
        "symbol": "TESTSYM",
        "lane": lane,
        "direction": "BUY",
        "entry_reference_price": 100.0,
        "blocked_reason": blocked_reason,
        "features": {},
        "status": "resolved",
        "created_at": _iso(_now()),
        "outcomes": {
            horizon: {
                "mark_price": 100.0 + bps / 100.0,
                "return_bps": bps,
                "verdict": verdict,
                "mark_source": "polygon_prev_close",
                "resolved_at": _iso(_now()),
            },
        },
    }


# ─── proposer: undersample ────────────────────────────────────────


@pytest.mark.asyncio
async def test_undersample_skipped():
    """Fewer than MIN_SAMPLE_SIZE resolved signals in a group → no proposal."""
    br = f"{_PFX}br1"
    rows = [
        _sig(i, verdict="MISSED_WIN", bps=25.0, blocked_reason=br)
        for i in range(MIN_SAMPLE_SIZE - 1)
    ]
    await db[COUNTERFACTUAL_SIGNALS].insert_many(rows)

    counts = await propose_tuning_signals(db, horizon="15m")
    assert counts["skipped_undersample"] >= 1
    assert counts["relax_proposals"] == 0
    assert counts["preserve_proposals"] == 0


# ─── proposer: RELAX_GATE ────────────────────────────────────────


@pytest.mark.asyncio
async def test_relax_gate_proposal_emitted():
    """30+ resolved signals, mostly MISSED_WIN, positive shrunk EV → RELAX."""
    br = f"{_PFX}br-relax"
    # 28 MISSED_WIN at +30 bps, 2 CORRECT_BLOCK at -25 bps → 30 total,
    # mw_rate=0.933, wilson_lower ~0.79, avg_bps ~+26.3, shrunk ~+6.1
    rows = [
        _sig(i, verdict="MISSED_WIN", bps=30.0, blocked_reason=br)
        for i in range(28)
    ] + [
        _sig(100 + i, verdict="CORRECT_BLOCK", bps=-25.0, blocked_reason=br)
        for i in range(2)
    ]
    await db[COUNTERFACTUAL_SIGNALS].insert_many(rows)

    counts = await propose_tuning_signals(db, horizon="15m")
    assert counts["relax_proposals"] == 1
    assert counts["preserve_proposals"] == 0

    signal = await db[COUNTERFACTUAL_TUNING_SIGNALS].find_one(
        {"group.blocked_reason": br},
    )
    assert signal is not None
    assert signal["kind"] == "relax_gate"
    assert signal["state"] == "proposed"
    assert signal["proposal"]["direction"] == "relax"
    assert signal["proposal"]["modifier"] > 0
    ev = signal["evidence"]
    assert ev["samples"] == 30
    assert ev["missed_wins"] == 28
    assert ev["correct_blocks"] == 2
    assert ev["missed_win_rate"] == pytest.approx(28 / 30)
    assert ev["wilson_lower_missed_win"] >= 0.60
    assert ev["shrunk_avg_bps"] >= 5.0


# ─── proposer: PRESERVE_GATE ─────────────────────────────────────


@pytest.mark.asyncio
async def test_preserve_gate_proposal_emitted():
    """30+ signals mostly CORRECT_BLOCK with negative avg → PRESERVE."""
    br = f"{_PFX}br-preserve"
    rows = [
        _sig(i, verdict="CORRECT_BLOCK", bps=-30.0, blocked_reason=br)
        for i in range(28)
    ] + [
        _sig(100 + i, verdict="MISSED_WIN", bps=25.0, blocked_reason=br)
        for i in range(2)
    ]
    await db[COUNTERFACTUAL_SIGNALS].insert_many(rows)

    counts = await propose_tuning_signals(db, horizon="15m")
    assert counts["preserve_proposals"] == 1
    assert counts["relax_proposals"] == 0

    signal = await db[COUNTERFACTUAL_TUNING_SIGNALS].find_one(
        {"group.blocked_reason": br},
    )
    assert signal is not None
    assert signal["kind"] == "preserve_gate"
    assert signal["proposal"]["direction"] == "preserve"
    assert signal["proposal"]["modifier"] < 0
    ev = signal["evidence"]
    assert ev["wilson_lower_correct_block"] >= 0.60
    assert ev["shrunk_avg_bps"] <= -5.0


# ─── proposer: noise skipped ─────────────────────────────────────


@pytest.mark.asyncio
async def test_noise_group_skipped():
    """50-50 split, small avg → no proposal, counted as noise."""
    br = f"{_PFX}br-noise"
    rows = [
        _sig(i, verdict="MISSED_WIN", bps=3.0, blocked_reason=br)
        for i in range(15)
    ] + [
        _sig(100 + i, verdict="CORRECT_BLOCK", bps=-3.0, blocked_reason=br)
        for i in range(15)
    ]
    await db[COUNTERFACTUAL_SIGNALS].insert_many(rows)

    counts = await propose_tuning_signals(db, horizon="15m")
    assert counts["skipped_noise"] >= 1
    assert counts["relax_proposals"] == 0
    assert counts["preserve_proposals"] == 0


# ─── proposer: idempotency + state preservation ──────────────────


@pytest.mark.asyncio
async def test_reruns_preserve_approved_state():
    """Approving a proposal, then re-running the analyzer, must NOT
    reset state back to 'proposed'."""
    br = f"{_PFX}br-approve"
    rows = [
        _sig(i, verdict="MISSED_WIN", bps=30.0, blocked_reason=br)
        for i in range(28)
    ] + [
        _sig(100 + i, verdict="CORRECT_BLOCK", bps=-25.0, blocked_reason=br)
        for i in range(2)
    ]
    await db[COUNTERFACTUAL_SIGNALS].insert_many(rows)

    await propose_tuning_signals(db, horizon="15m")
    doc = await db[COUNTERFACTUAL_TUNING_SIGNALS].find_one(
        {"group.blocked_reason": br},
    )
    signal_id = doc["_id"]

    # Approve.
    await db[COUNTERFACTUAL_TUNING_SIGNALS].update_one(
        {"_id": signal_id},
        {"$set": {"state": "approved", "approved_by": "operator",
                  "approved_at": _iso(_now())}},
    )

    # Re-propose.
    await propose_tuning_signals(db, horizon="15m")
    doc2 = await db[COUNTERFACTUAL_TUNING_SIGNALS].find_one({"_id": signal_id})
    assert doc2["state"] == "approved"
    assert doc2["approved_by"] == "operator"


# ─── doctrine overlay integration ────────────────────────────────


@pytest.mark.asyncio
async def test_gate_threshold_delta_zero_when_no_approval():
    """No approved tuning signal for the (reason, lane) → delta 0.0."""
    br = f"{_PFX}br-unknown"
    delta = await doctrine_overlay.get_gate_threshold_delta(
        br, "equity", db=db,
    )
    assert delta == 0.0


@pytest.mark.asyncio
async def test_gate_threshold_delta_uses_approved_modifier():
    """Approved RELAX signal → positive delta; PRESERVE → negative delta."""
    br = f"{_PFX}br-live"
    # Seed the tuning signal directly with state=approved.
    approved = {
        "_id": "sig-approved-relax-test",
        "group": {"blocked_reason": br, "lane": "equity"},
        "kind": "relax_gate",
        "proposal": {
            "kind": "relax_gate",
            "target_gate": br,
            "target_lane": "equity",
            "direction": "relax",
            "modifier": 0.15,
            "suggested_action": "test relax",
        },
        "evidence": {"samples": 30},
        "state": "approved",
        "proposed_at": _iso(_now()),
        "approved_at": _iso(_now()),
        "approved_by": "operator",
    }
    try:
        await db[COUNTERFACTUAL_TUNING_SIGNALS].insert_one(approved)
        doctrine_overlay.reload_gate_cache_for_tests()

        delta = await doctrine_overlay.get_gate_threshold_delta(
            br, "equity", db=db,
        )
        assert delta == pytest.approx(0.15)

        # Wrong lane → no match, 0.0.
        delta_other = await doctrine_overlay.get_gate_threshold_delta(
            br, "crypto", db=db,
        )
        assert delta_other == 0.0
    finally:
        await db[COUNTERFACTUAL_TUNING_SIGNALS].delete_one(
            {"_id": "sig-approved-relax-test"},
        )
        doctrine_overlay.reload_gate_cache_for_tests()


@pytest.mark.asyncio
async def test_gate_threshold_delta_clamped_to_max():
    """Runaway modifier can never exceed ±0.20."""
    br = f"{_PFX}br-clamped"
    doc = {
        "_id": "sig-clamp-test",
        "group": {"blocked_reason": br, "lane": "equity"},
        "kind": "relax_gate",
        "proposal": {"modifier": 0.99},  # runaway
        "state": "approved",
        "proposed_at": _iso(_now()),
    }
    try:
        await db[COUNTERFACTUAL_TUNING_SIGNALS].insert_one(doc)
        doctrine_overlay.reload_gate_cache_for_tests()
        delta = await doctrine_overlay.get_gate_threshold_delta(
            br, "equity", db=db,
        )
        assert delta == pytest.approx(0.20)
    finally:
        await db[COUNTERFACTUAL_TUNING_SIGNALS].delete_one(
            {"_id": "sig-clamp-test"},
        )
        doctrine_overlay.reload_gate_cache_for_tests()


@pytest.mark.asyncio
async def test_gate_threshold_delta_ignores_rejected():
    """Rejected signals contribute nothing to the cache."""
    br = f"{_PFX}br-rejected"
    doc = {
        "_id": "sig-rejected-test",
        "group": {"blocked_reason": br, "lane": "equity"},
        "kind": "relax_gate",
        "proposal": {"modifier": 0.15},
        "state": "rejected",
        "proposed_at": _iso(_now()),
    }
    try:
        await db[COUNTERFACTUAL_TUNING_SIGNALS].insert_one(doc)
        doctrine_overlay.reload_gate_cache_for_tests()
        delta = await doctrine_overlay.get_gate_threshold_delta(
            br, "equity", db=db,
        )
        assert delta == 0.0
    finally:
        await db[COUNTERFACTUAL_TUNING_SIGNALS].delete_one(
            {"_id": "sig-rejected-test"},
        )
        doctrine_overlay.reload_gate_cache_for_tests()

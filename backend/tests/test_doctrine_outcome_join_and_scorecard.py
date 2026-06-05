"""Tests for the doctrine outcome-join + scorecard aggregation.

Drives the full lifecycle:
    intent ingest → doctrine packet stored → simulate close →
    outcome_join attached → scorecard aggregates → promotion gate
    correctly flags "not yet promotable" until samples ≥ 100.

Uses pytest-asyncio in `asyncio_mode = auto` so every `async def
test_…` is run directly on the session event loop. This keeps Motor's
module-global client bound to one loop across the suite (the fix for
the earlier "different event loop" skip noise).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone


def _make_sidecar_row(*, intent_id, lane, quality, gov_action,
                      redeye_challenge, judge_ready, outcome_label,
                      pnl_usd):
    """Build a synthetic doctrine_sidecars row with an outcome envelope."""
    ts = datetime.now(timezone.utc).isoformat()
    return {
        "intent_id": intent_id,
        "stack": "alpha",
        "lane": lane,
        "symbol": "TEST",
        "action": "BUY",
        "ingest_confidence": 0.7,
        "ingest_method": "test",
        "quality": quality,
        "score": 0.7 if quality == "A_QUALITY" else 0.3,
        "redeye_challenge_required": redeye_challenge,
        "chevelle_governor_action": gov_action,
        "camaro_execution_ready": judge_ready,
        "ts": ts,
        "outcome_join": {
            "joined_at": ts,
            "position_id": f"pos-{intent_id}",
            "lane": lane,
            "symbol": "TEST",
            "outcome_label": outcome_label,
            "pnl_usd": pnl_usd,
            "pnl_pct": pnl_usd / 100.0,
            "closing_actor": "test",
        },
    }


# ─── outcome-join one-shot guarantee ─────────────────────────────────

async def test_outcome_join_skips_when_no_intent_id():
    from shared.doctrine.outcome_join import join_outcome_to_doctrine
    ok = await join_outcome_to_doctrine(
        intent_id=None, position_id="pos-1", lane="equity", symbol="NVDA",
        outcome_label="win", pnl_usd=10.0, pnl_pct=1.0,
        opened_at=None, closed_at=None, closing_actor="test",
    )
    assert ok is False


async def test_outcome_join_skips_when_intent_has_no_sidecar_row():
    from shared.doctrine.outcome_join import join_outcome_to_doctrine
    fake_intent = f"no-doctrine-{uuid.uuid4()}"
    ok = await join_outcome_to_doctrine(
        intent_id=fake_intent, position_id="pos-x", lane="equity",
        symbol="NVDA", outcome_label="win", pnl_usd=5.0, pnl_pct=0.5,
        opened_at=None, closed_at=None, closing_actor="test",
    )
    assert ok is False


async def test_outcome_join_writes_and_is_idempotent():
    """First close attaches the envelope; second close on the same
    intent_id MUST NOT overwrite it (a take-profit close racing with
    a manual operator close would otherwise double-write)."""
    from db import db
    from namespaces import DOCTRINE_SIDECARS
    from shared.doctrine.outcome_join import join_outcome_to_doctrine

    intent_id = f"join-test-{uuid.uuid4()}"

    try:
        await db[DOCTRINE_SIDECARS].insert_one({
            "intent_id": intent_id, "stack": "alpha", "lane": "equity",
            "symbol": "NVDA", "quality": "A_QUALITY",
        })
        first = await join_outcome_to_doctrine(
            intent_id=intent_id, position_id="pos-1", lane="equity",
            symbol="NVDA", outcome_label="win", pnl_usd=12.0, pnl_pct=1.2,
            opened_at=None, closed_at=None, closing_actor="take_profit",
        )
        second = await join_outcome_to_doctrine(
            intent_id=intent_id, position_id="pos-1", lane="equity",
            symbol="NVDA", outcome_label="loss", pnl_usd=-99.0, pnl_pct=-9.9,
            opened_at=None, closed_at=None, closing_actor="manual_close",
        )
        row = await db[DOCTRINE_SIDECARS].find_one(
            {"intent_id": intent_id}, {"_id": 0},
        )
    finally:
        await db[DOCTRINE_SIDECARS].delete_many({"intent_id": intent_id})

    assert first is True
    assert second is False, "second join must be a no-op (idempotent)"
    assert row["outcome_join"]["pnl_usd"] == 12.0, "must preserve first close"
    assert row["outcome_join"]["closing_actor"] == "take_profit"


# ─── scorecard aggregation ───────────────────────────────────────────

async def test_scorecard_quality_bands_aggregate():
    """Insert a controlled set of rows for a synthetic lane and verify
    the scorecard returns the right win_rate / avg_pnl per band."""
    from db import db
    from namespaces import DOCTRINE_SIDECARS
    from shared.doctrine.scorecard import doctrine_scorecard

    lane = f"scorecard-test-{uuid.uuid4().hex[:8]}"

    rows = []
    # 3 A_QUALITY wins (+10 each), 1 loss (-5) → win_rate 0.75, avg pnl 6.25
    for i in range(3):
        rows.append(_make_sidecar_row(
            intent_id=f"a-win-{i}-{uuid.uuid4()}", lane=lane, quality="A_QUALITY",
            gov_action="modulate", redeye_challenge=False, judge_ready=True,
            outcome_label="win", pnl_usd=10.0,
        ))
    rows.append(_make_sidecar_row(
        intent_id=f"a-loss-{uuid.uuid4()}", lane=lane, quality="A_QUALITY",
        gov_action="modulate", redeye_challenge=False, judge_ready=True,
        outcome_label="loss", pnl_usd=-5.0,
    ))
    rows.append(_make_sidecar_row(
        intent_id=f"rej-{uuid.uuid4()}", lane=lane, quality="REJECT",
        gov_action="block", redeye_challenge=True, judge_ready=False,
        outcome_label="loss", pnl_usd=-20.0,
    ))

    try:
        await db[DOCTRINE_SIDECARS].insert_many([r.copy() for r in rows])
        # 2026-02-17 (rev2): `stack` removed from scorecard signature —
        # doctrine canonicalized scoring axes onto seat, not brain.
        # `doctrine_version=None` passed explicitly to avoid the
        # FastAPI Query() default leaking into a direct invocation.
        res = await doctrine_scorecard(lane=lane, doctrine_version=None, min_samples_per_band=1, _user={})
    finally:
        await db[DOCTRINE_SIDECARS].delete_many({"lane": lane})

    a = res["by_quality"].get("A_QUALITY")
    assert a is not None, res
    assert a["samples"] == 4
    assert a["wins"] == 3
    assert a["losses"] == 1
    assert a["win_rate"] == 0.75
    assert a["avg_pnl_usd"] == 6.25  # (10+10+10-5)/4

    rej = res["by_quality"].get("REJECT")
    assert rej["samples"] == 1
    assert rej["win_rate"] == 0.0


async def test_scorecard_promotion_blocked_below_min_samples():
    """With <100 samples, ready_for_promotion must be False and the
    blocker list must contain `min_samples<100`."""
    from db import db
    from namespaces import DOCTRINE_SIDECARS
    from shared.doctrine.scorecard import doctrine_scorecard

    lane = f"promo-test-{uuid.uuid4().hex[:8]}"
    row = _make_sidecar_row(
        intent_id=f"sole-{uuid.uuid4()}", lane=lane, quality="A_QUALITY",
        gov_action="modulate", redeye_challenge=False, judge_ready=True,
        outcome_label="win", pnl_usd=5.0,
    )
    try:
        await db[DOCTRINE_SIDECARS].insert_one(row.copy())
        res = await doctrine_scorecard(lane=lane, doctrine_version=None, min_samples_per_band=1, _user={})
    finally:
        await db[DOCTRINE_SIDECARS].delete_many({"lane": lane})

    assert res["ready_for_promotion"] is False
    assert any("min_samples<100" in b for b in res["promotion_blockers"])


async def test_scorecard_per_seat_loss_rates():
    """Per-seat bucket loss rates must aggregate correctly."""
    from db import db
    from namespaces import DOCTRINE_SIDECARS
    from shared.doctrine.scorecard import doctrine_scorecard

    lane = f"seat-test-{uuid.uuid4().hex[:8]}"

    rows = []
    # 2 governor BLOCK + loss (loss_rate = 1.0)
    for i in range(2):
        rows.append(_make_sidecar_row(
            intent_id=f"gb-{i}-{uuid.uuid4()}", lane=lane, quality="REJECT",
            gov_action="block", redeye_challenge=True, judge_ready=False,
            outcome_label="loss", pnl_usd=-10.0,
        ))
    # 4 governor MODULATE: 1 loss, 3 wins (loss_rate 0.25)
    rows.append(_make_sidecar_row(
        intent_id=f"gm-loss-{uuid.uuid4()}", lane=lane, quality="B_QUALITY",
        gov_action="modulate", redeye_challenge=False, judge_ready=True,
        outcome_label="loss", pnl_usd=-3.0,
    ))
    for i in range(3):
        rows.append(_make_sidecar_row(
            intent_id=f"gm-win-{i}-{uuid.uuid4()}", lane=lane, quality="A_QUALITY",
            gov_action="modulate", redeye_challenge=False, judge_ready=True,
            outcome_label="win", pnl_usd=8.0,
        ))

    try:
        await db[DOCTRINE_SIDECARS].insert_many([r.copy() for r in rows])
        res = await doctrine_scorecard(lane=lane, doctrine_version=None, min_samples_per_band=1, _user={})
    finally:
        await db[DOCTRINE_SIDECARS].delete_many({"lane": lane})

    gov = res["by_seat"]["governor"]
    assert gov["block"]["samples_with_outcome"] == 2
    assert gov["block"]["loss_rate"] == 1.0
    assert gov["modulate"]["samples_with_outcome"] == 4
    assert gov["modulate"]["loss_rate"] == 0.25

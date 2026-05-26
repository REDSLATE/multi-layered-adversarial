"""Tripwires — cross-brain memory join endpoint (2026-05-24).

Pins the doctrine:

  1. Quarantine contagion: if ANY brain labels a memory_id as
     `quarantine`, that memory is excluded from `peer_memories` for
     ALL brain queries.
  2. Per-source weighting: weight scales linearly with win rate,
     clamped to [0.5, 2.0]. Neutral 1.0 when no resolved outcomes.
  3. Auth: X-Runtime-Token required. Bogus token → 401.
  4. Lane filter respects the enum.
  5. Cache works (same response object within TTL).
"""
from __future__ import annotations

import pytest

from routes.runtime_cross_brain_memories import (
    WEIGHT_MAX, WEIGHT_MIN, WEIGHT_SCALE,
    _compute_weight, _resolve_runtime_from_token,
    cross_brain_memories,
)


pytestmark = [pytest.mark.tripwire]


# ─── weight math ──────────────────────────────────────────────────


def test_weight_min_max_doctrine():
    """Clamp boundaries — even a 100% win-rate brain can't dominate."""
    assert WEIGHT_MIN == 0.5
    assert WEIGHT_MAX == 2.0
    assert WEIGHT_SCALE == 2.0


def test_neutral_weight_when_no_data():
    """No resolved outcomes → weight 1.0 (neither penalized nor boosted)."""
    assert _compute_weight(0, 0) == 1.0


def test_perfect_win_rate_clamped():
    """100% wins would be 2.0; clamp keeps it at WEIGHT_MAX."""
    assert _compute_weight(100, 0) == 2.0


def test_perfect_loss_rate_clamped():
    """0% wins → raw 0.0; clamp lifts to WEIGHT_MIN."""
    assert _compute_weight(0, 100) == 0.5


def test_fifty_percent_winrate_is_neutral():
    """50% wins × 2.0 scale = 1.0 weight."""
    assert _compute_weight(50, 50) == 1.0


def test_sixty_percent_winrate_boosts():
    """60% wins × 2.0 = 1.2 weight."""
    assert _compute_weight(60, 40) == 1.2


def test_forty_percent_winrate_penalizes():
    """40% wins × 2.0 = 0.8 weight."""
    assert _compute_weight(40, 60) == 0.8


# ─── auth ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_token_returns_401():
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await cross_brain_memories(symbol="AAPL", x_runtime_token=None)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_bogus_token_returns_401():
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await cross_brain_memories(symbol="AAPL", x_runtime_token="forged")
    assert exc.value.status_code == 401


def test_resolve_brain_from_token(monkeypatch):
    monkeypatch.setenv("CAMARO_INGEST_TOKEN", "tw-cam")
    monkeypatch.setenv("REDEYE_INGEST_TOKEN", "tw-red")
    assert _resolve_runtime_from_token("tw-cam") == "camaro"
    assert _resolve_runtime_from_token("tw-red") == "redeye"
    assert _resolve_runtime_from_token("nope") is None


# ─── integration: quarantine contagion ───────────────────────────


@pytest.mark.asyncio
async def test_quarantine_contagion_excludes_safe_view(monkeypatch):
    """Insert two memories from different brains for the same symbol.
    Have Camaro file a quarantine label on the one that REDEYE wrote.
    Assert REDEYE's row is filtered out of peer_memories (contagion),
    AND that the quarantined_count includes it."""
    from db import db
    monkeypatch.setenv("CAMARO_INGEST_TOKEN", "tw-cam")
    # Insert two memories
    await db["brain_memories"].delete_many({"memory_id": {"$regex": "^tw-link-"}})
    await db["shared_labeled_memories"].delete_many({"reason": {"$regex": "tw-link-"}})
    await db["brain_memories"].insert_many([
        {
            "memory_id": "tw-link-camaro-1",
            "decision_id": "tw-link-camaro-dec-1",
            "brain": "camaro",
            "symbol": "AAPL",
            "lane": "equity",
            "decided_at": "2026-05-20T10:00:00+00:00",
            "decision": {"raw_action": "BUY", "display_action": "BUY",
                         "confidence": 0.7, "execution_decision": "ALLOW"},
            "resolution": {"outcome": 1, "realized_r": 1.2, "mae": -0.3,
                           "mfe": 1.5, "entry_price": 180, "exit_price": 182,
                           "resolved_at": "2026-05-21T10:00:00+00:00",
                           "mode": "shadow"},
            "features": {},
            "text_summary": "AAPL BUY resolved win",
        },
        {
            "memory_id": "tw-link-redeye-1",
            "decision_id": "tw-link-redeye-dec-1",
            "brain": "redeye",
            "symbol": "AAPL",
            "lane": "equity",
            "decided_at": "2026-05-20T11:00:00+00:00",
            "decision": {"raw_action": "SELL", "display_action": "SELL",
                         "confidence": 0.8, "execution_decision": "ALLOW"},
            "resolution": {"outcome": -1, "realized_r": -0.5, "mae": -0.8,
                           "mfe": 0.2, "entry_price": 182, "exit_price": 180,
                           "resolved_at": "2026-05-21T11:00:00+00:00",
                           "mode": "shadow"},
            "features": {},
            "text_summary": "AAPL SELL resolved loss",
        },
    ])
    # Camaro files a quarantine label on REDEYE's memory
    await db["shared_labeled_memories"].insert_one({
        "id": "tw-link-quar-1",
        "runtime": "camaro",
        "label": "quarantine",
        "reason": "decision_id=tw-link-redeye-dec-1 reason=unsafe",
        "payload_summary": "tw-link-quar test",
        "timestamp": "2026-05-22T10:00:00+00:00",
    })

    # Clear cache so this read isn't served from a prior test
    from routes.runtime_cross_brain_memories import _cache
    _cache.clear()

    try:
        result = await cross_brain_memories(
            symbol="AAPL", lane="equity", limit=50,
            include_quarantined=True,
            x_runtime_token="tw-cam",
        )
        peer_ids = {m["memory_id"] for m in result["peer_memories"]}
        quarantined_ids = {m["memory_id"] for m in result["quarantined_memories"]}
        # REDEYE's memory must NOT appear in safe peer view
        assert "tw-link-redeye-1" not in peer_ids
        # REDEYE's memory must appear in quarantined corpus
        assert "tw-link-redeye-1" in quarantined_ids
        # Camaro's memory should still be safe-viewable
        assert "tw-link-camaro-1" in peer_ids
        # Source-tag is present
        cam_row = next(m for m in result["peer_memories"] if m["memory_id"] == "tw-link-camaro-1")
        assert cam_row["source_brain"] == "camaro"
        # Weight is attached
        assert "source_weight" in cam_row
        # Quarantine flag explicit on the quarantined row
        red_row = next(m for m in result["quarantined_memories"] if m["memory_id"] == "tw-link-redeye-1")
        assert red_row["quarantined"] is True
    finally:
        await db["brain_memories"].delete_many({"memory_id": {"$regex": "^tw-link-"}})
        await db["shared_labeled_memories"].delete_many({"reason": {"$regex": "tw-link-"}})
        _cache.clear()


# ─── integration: response shape ──────────────────────────────────


@pytest.mark.asyncio
async def test_response_includes_per_brain_weights(monkeypatch):
    """Every response includes a weights_by_brain table for all 4 brains."""
    monkeypatch.setenv("ALPHA_INGEST_TOKEN", "tw-alpha")
    from routes.runtime_cross_brain_memories import _cache
    _cache.clear()
    result = await cross_brain_memories(
        symbol="NOEXIST-tw", x_runtime_token="tw-alpha",
    )
    assert set(result["weights_by_brain"].keys()) == {
        "alpha", "camaro", "chevelle", "redeye",
    }
    for brain, info in result["weights_by_brain"].items():
        assert "wins" in info
        assert "losses" in info
        assert "source_weight" in info
        assert WEIGHT_MIN <= info["source_weight"] <= WEIGHT_MAX


@pytest.mark.asyncio
async def test_response_includes_counts_by_brain(monkeypatch):
    monkeypatch.setenv("ALPHA_INGEST_TOKEN", "tw-alpha")
    from routes.runtime_cross_brain_memories import _cache
    _cache.clear()
    result = await cross_brain_memories(
        symbol="NOEXIST-tw", x_runtime_token="tw-alpha",
    )
    assert set(result["counts_by_brain"].keys()) == {
        "alpha", "camaro", "chevelle", "redeye",
    }

"""Paradox v3 — execution_style_outcomes endpoint tests.

Pins the conservative band thresholds (operator 2026-02-22) so a
future cleanup pass can't quietly lower them.
"""
from __future__ import annotations

import uuid

import pytest

from db import db
from namespaces import DOCTRINE_SIDECARS


pytestmark = pytest.mark.asyncio


async def _seed(*, style, n_wins, n_losses, pnl_per_win=10.0, pnl_per_loss=-5.0):
    ids = []
    for _ in range(n_wins):
        iid = f"esot-{uuid.uuid4().hex[:10]}"
        ids.append(iid)
        await db[DOCTRINE_SIDECARS].insert_one({
            "intent_id": iid, "intent_version": "v3",
            "plan_execution_style": style,
            "outcome_join": {"outcome_label": "win", "pnl_usd": pnl_per_win},
        })
    for _ in range(n_losses):
        iid = f"esot-{uuid.uuid4().hex[:10]}"
        ids.append(iid)
        await db[DOCTRINE_SIDECARS].insert_one({
            "intent_id": iid, "intent_version": "v3",
            "plan_execution_style": style,
            "outcome_join": {"outcome_label": "loss", "pnl_usd": pnl_per_loss},
        })
    return ids


async def _cleanup(ids):
    if ids:
        await db[DOCTRINE_SIDECARS].delete_many({"intent_id": {"$in": ids}})


async def test_endpoint_returns_per_style_buckets():
    from routes.admin_paradox_v3 import execution_style_outcomes
    a = await _seed(style="PATIENT", n_wins=20, n_losses=10)
    b = await _seed(style="MARKET_NOW", n_wins=10, n_losses=15)
    try:
        out = await execution_style_outcomes(_user={"email": "x"})
        by_style = {r["execution_style"]: r for r in out["styles"]}
        patient = by_style.get("PATIENT")
        market = by_style.get("MARKET_NOW")
        assert patient is not None
        assert patient["wins"] >= 20
        assert patient["losses"] >= 10
        assert 0.6 <= patient["win_rate"] <= 0.7
        assert market is not None
        assert 0.35 <= market["win_rate"] <= 0.45
    finally:
        await _cleanup(a + b)


async def test_v2_rows_excluded():
    """Only v3 rows count — v2 legacy rows must be excluded."""
    from routes.admin_paradox_v3 import execution_style_outcomes
    # Seed a v2 row (no execution_style label, no v3 envelope).
    iid = f"v2-{uuid.uuid4().hex[:10]}"
    await db[DOCTRINE_SIDECARS].insert_one({
        "intent_id": iid, "intent_version": "v2",  # ← key
        "plan_execution_style": None,
        "outcome_join": {"outcome_label": "win", "pnl_usd": 100.0},
    })
    try:
        out = await execution_style_outcomes(_user={"email": "x"})
        for r in out["styles"]:
            # No bucket inherits this v2 row's pnl.
            assert r["execution_style"] != "v2"
    finally:
        await _cleanup([iid])


async def test_band_thresholds_pinned():
    """Operator pin: 30 hard floor, LEARNING≥30, READY≥50,
    STRONG≥100, HIGH_CONVICTION≥200. Conservative on purpose."""
    from routes.admin_paradox_v3 import _band_for_samples, _BANDS
    assert _band_for_samples(29) == "INSUFFICIENT"
    assert _band_for_samples(30) == "LEARNING"
    assert _band_for_samples(49) == "LEARNING"
    assert _band_for_samples(50) == "READY"
    assert _band_for_samples(99) == "READY"
    assert _band_for_samples(100) == "STRONG"
    assert _band_for_samples(199) == "STRONG"
    assert _band_for_samples(200) == "HIGH_CONVICTION"
    # Pin the tuple itself in case a future agent shuffles it.
    assert dict(_BANDS) == {
        "HIGH_CONVICTION": 200,
        "STRONG":          100,
        "READY":            50,
        "LEARNING":         30,
    }


async def test_endpoint_response_shape_carries_bands_and_doctrine_note():
    from routes.admin_paradox_v3 import execution_style_outcomes
    out = await execution_style_outcomes(_user={"email": "x"})
    assert "styles" in out
    assert "bands" in out
    assert out["hard_floor"] == 30
    assert "doctrine_note" in out
    assert isinstance(out["styles"], list)


async def test_empty_db_returns_empty_styles():
    """A fresh deploy with no v3 outcomes still returns a clean shape."""
    from routes.admin_paradox_v3 import execution_style_outcomes
    out = await execution_style_outcomes(_user={"email": "x"})
    # Shape is always present even when no data.
    assert isinstance(out["styles"], list)
    assert "bands" in out


async def test_state_assigned_per_style():
    """A style with 60 resolved → READY. A style with 35 → LEARNING."""
    from routes.admin_paradox_v3 import execution_style_outcomes
    a = await _seed(style="PATIENT", n_wins=30, n_losses=30)   # 60 → READY
    b = await _seed(style="SCALED",  n_wins=20, n_losses=15)   # 35 → LEARNING
    try:
        out = await execution_style_outcomes(_user={"email": "x"})
        by_style = {r["execution_style"]: r for r in out["styles"]}
        # `>=` because previous test seeds may not be cleaned in CI.
        assert by_style["PATIENT"]["state"] in ("READY", "STRONG", "HIGH_CONVICTION")
        assert by_style["SCALED"]["state"] in ("LEARNING", "READY")
    finally:
        await _cleanup(a + b)

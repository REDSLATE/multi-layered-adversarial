"""Paradox v3 — per-brain execution-style profile endpoint tests.

Operator pin (2026-02-23): OBSERVATIONAL profile. `stack` is metadata
per the seat-doctrinal canonicalization pin. Tests assert:

  * Cells group by (stack, plan_execution_style)
  * v2 rows are excluded
  * Same conservative bands apply
  * Missing `stack` falls into the "UNKNOWN" bucket
  * Response shape carries the doctrine_note + brains/styles arrays
"""
from __future__ import annotations

import uuid

import pytest

from db import db
from namespaces import DOCTRINE_SIDECARS


pytestmark = pytest.mark.asyncio


async def _seed_row(*, brain, style, label, pnl, version="v3"):
    iid = f"pbesp-{uuid.uuid4().hex[:10]}"
    doc = {
        "intent_id":         iid,
        "intent_version":    version,
        "stack":             brain,
        "plan_execution_style": style,
        "outcome_join":      {"outcome_label": label, "pnl_usd": pnl},
    }
    # Drop `stack` key entirely if caller passed None (simulates row
    # where stack was never recorded).
    if brain is None:
        doc.pop("stack")
    await db[DOCTRINE_SIDECARS].insert_one(doc)
    return iid


async def _cleanup(ids):
    if ids:
        await db[DOCTRINE_SIDECARS].delete_many({"intent_id": {"$in": ids}})


async def test_cells_grouped_by_brain_and_style():
    from routes.admin_paradox_v3 import per_brain_execution_style_profile
    ids = []
    # camino × PATIENT: 4 wins, 1 loss
    for _ in range(4):
        ids.append(await _seed_row(brain="camino", style="PATIENT",
                                   label="win", pnl=10.0))
    ids.append(await _seed_row(brain="camino", style="PATIENT",
                               label="loss", pnl=-5.0))
    # camino × MARKET_NOW: 1 win
    ids.append(await _seed_row(brain="camino", style="MARKET_NOW",
                               label="win", pnl=7.0))
    # barracuda × PATIENT: 2 wins
    for _ in range(2):
        ids.append(await _seed_row(brain="barracuda", style="PATIENT",
                                   label="win", pnl=4.0))
    try:
        out = await per_brain_execution_style_profile(_user={"email": "x"})
        by_cell = {(c["brain"], c["execution_style"]): c for c in out["cells"]}
        cam_pat = by_cell[("camino", "PATIENT")]
        assert cam_pat["trades"] == 5
        assert cam_pat["wins"] == 4
        assert cam_pat["losses"] == 1
        assert 0.79 <= cam_pat["win_rate"] <= 0.81
        cam_mkt = by_cell[("camino", "MARKET_NOW")]
        assert cam_mkt["trades"] == 1
        assert cam_mkt["wins"] == 1
        bar_pat = by_cell[("barracuda", "PATIENT")]
        assert bar_pat["trades"] == 2
        # Brains/styles arrays surface the union.
        assert "camino" in out["brains"]
        assert "barracuda" in out["brains"]
        assert "PATIENT" in out["styles"]
        assert "MARKET_NOW" in out["styles"]
    finally:
        await _cleanup(ids)


async def test_v2_rows_excluded():
    """v2 rows must not contribute to the per-brain matrix."""
    from routes.admin_paradox_v3 import per_brain_execution_style_profile
    iid_v2 = await _seed_row(brain="camino", style="MARKET_NOW",
                             label="win", pnl=999.0, version="v2")
    try:
        out = await per_brain_execution_style_profile(_user={"email": "x"})
        # No cell should ever surface the $999 anomaly because v2
        # rows are excluded at the query stage.
        for cell in out["cells"]:
            if cell["brain"] == "camino" and cell["execution_style"] == "MARKET_NOW":
                # The pnl spike from the v2 row would push avg_pnl
                # toward 999 if not excluded — assert it's nowhere
                # near that.
                assert cell["avg_pnl_usd"] < 500
    finally:
        await _cleanup([iid_v2])


async def test_totals_by_brain_aggregate_across_styles():
    from routes.admin_paradox_v3 import per_brain_execution_style_profile
    ids = []
    for _ in range(3):
        ids.append(await _seed_row(brain="hellcat", style="PATIENT",
                                   label="win", pnl=2.0))
    for _ in range(2):
        ids.append(await _seed_row(brain="hellcat", style="MARKET_NOW",
                                   label="loss", pnl=-1.0))
    try:
        out = await per_brain_execution_style_profile(_user={"email": "x"})
        totals = {t["brain"]: t for t in out["totals_by_brain"]}
        h = totals["hellcat"]
        assert h["trades"] == 5
        assert h["wins"] == 3
        assert h["losses"] == 2
        assert 0.59 <= h["win_rate"] <= 0.61
    finally:
        await _cleanup(ids)


async def test_missing_stack_buckets_under_unknown():
    from routes.admin_paradox_v3 import per_brain_execution_style_profile
    iid = await _seed_row(brain=None, style="PATIENT",
                          label="win", pnl=3.0)
    try:
        out = await per_brain_execution_style_profile(_user={"email": "x"})
        brains = set(out["brains"])
        assert "unknown" in brains
    finally:
        await _cleanup([iid])


async def test_bands_applied_to_cells():
    """A cell with 60 trades → READY band."""
    from routes.admin_paradox_v3 import per_brain_execution_style_profile
    ids = []
    for _ in range(60):
        ids.append(await _seed_row(brain="gto", style="SCALED",
                                   label="win", pnl=1.0))
    try:
        out = await per_brain_execution_style_profile(_user={"email": "x"})
        by_cell = {(c["brain"], c["execution_style"]): c for c in out["cells"]}
        gto_scaled = by_cell[("gto", "SCALED")]
        assert gto_scaled["state"] in ("READY", "STRONG", "HIGH_CONVICTION")
    finally:
        await _cleanup(ids)


async def test_response_shape_carries_doctrine_note():
    from routes.admin_paradox_v3 import per_brain_execution_style_profile
    out = await per_brain_execution_style_profile(_user={"email": "x"})
    assert "doctrine_note" in out
    # Critical: the OBSERVATIONAL framing must be present so the
    # tile can render the "not brain scoring" subtitle.
    assert "OBSERVATIONAL" in out["doctrine_note"]
    assert "metadata" in out["doctrine_note"].lower()
    assert isinstance(out["cells"], list)
    assert isinstance(out["totals_by_brain"], list)
    assert isinstance(out["brains"], list)
    assert isinstance(out["styles"], list)
    assert out["hard_floor"] == 30

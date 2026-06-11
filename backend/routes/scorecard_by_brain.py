"""Per-brain doctrine scorecard.

Companion to `shared/doctrine/scorecard.py` which aggregates by
`(lane, seat, doctrine_version)`. This endpoint adds the BRAIN ATTRIBUTION
axis the operator asked for — `(lane, stack, doctrine_version)` — so we
can answer "what's Camino's win rate vs Barracuda's on gap_and_go?"
without breaking the doctrine pin that seats own performance.

This is METADATA only: it does NOT influence promotion / retirement
verdicts. Those still key on `(lane, seat, doctrine_version)` per
Patent J. This endpoint exists for operator visibility / triage.

Naming note: backend stores brain identity in `stack` (alpha/camaro/
chevelle/redeye). Frontend should map to operator-facing names
(Camino/Barracuda/Hellcat/GTO) at render time.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, Query

from auth import get_current_user
from db import db
from namespaces import DOCTRINE_SIDECARS

router = APIRouter(prefix="/admin/doctrine", tags=["doctrine-by-brain"])


# Operator-facing rename map for frontend convenience. Backend
# never trusts this — wire still flows on `stack`.
BRAIN_DISPLAY_NAMES = {
    "alpha": "Camino",
    "camaro": "Barracuda",
    "chevelle": "Hellcat",
    "redeye": "GTO",
}


def _is_win(label: Optional[str]) -> bool:
    return (label or "").lower() == "win"


def _is_loss(label: Optional[str]) -> bool:
    return (label or "").lower() in ("loss", "stopped_out")


@router.get("/scorecard-by-brain")
async def scorecard_by_brain(
    lane: Optional[Literal["equity", "crypto"]] = Query(default=None),
    doctrine_version: Optional[str] = Query(default=None),
    _user: dict = Depends(get_current_user),  # noqa: B008
) -> Dict[str, Any]:
    """Aggregate joined outcomes keyed on `(lane, stack, doctrine_version)`.

    METADATA only — never used for promotion gates.
    """
    q: Dict[str, Any] = {"outcome_join": {"$exists": True}}
    if lane:
        q["lane"] = lane
    if doctrine_version:
        q["doctrine_version"] = doctrine_version

    rows = await db[DOCTRINE_SIDECARS].find(q, {"_id": 0}).to_list(50_000)

    # ── primary aggregation: (lane, stack, doctrine_version) ──
    grouped: Dict[tuple, Dict[str, Any]] = defaultdict(
        lambda: {
            "samples": 0,
            "wins": 0,
            "losses": 0,
            "scratches": 0,
            "pnl_usd_sum": 0.0,
            "pnl_usd_wins": 0.0,
            "pnl_usd_losses": 0.0,
        }
    )
    for r in rows:
        oj = r.get("outcome_join") or {}
        key = (
            r.get("lane") or "unknown",
            (r.get("stack") or oj.get("extra", {}).get("stack") or "unknown"),
            r.get("doctrine_version") or "unknown",
        )
        agg = grouped[key]
        agg["samples"] += 1
        label = oj.get("outcome_label")
        pnl = oj.get("pnl_usd")
        pnl = float(pnl) if isinstance(pnl, (int, float)) else 0.0
        agg["pnl_usd_sum"] += pnl
        if _is_win(label):
            agg["wins"] += 1
            agg["pnl_usd_wins"] += pnl
        elif _is_loss(label):
            agg["losses"] += 1
            agg["pnl_usd_losses"] += pnl
        else:
            agg["scratches"] += 1

    slices: List[Dict[str, Any]] = []
    for (lane_k, stack, dv), agg in grouped.items():
        wr = (agg["wins"] / agg["samples"]) if agg["samples"] else 0.0
        decisive = agg["wins"] + agg["losses"]
        wr_decisive = (agg["wins"] / decisive) if decisive else None
        avg_win = (agg["pnl_usd_wins"] / agg["wins"]) if agg["wins"] else None
        avg_loss = (agg["pnl_usd_losses"] / agg["losses"]) if agg["losses"] else None
        slices.append({
            "lane": lane_k,
            "stack": stack,
            "brain_display_name": BRAIN_DISPLAY_NAMES.get(stack, stack),
            "doctrine_version": dv,
            "samples": agg["samples"],
            "wins": agg["wins"],
            "losses": agg["losses"],
            "scratches": agg["scratches"],
            "win_rate": round(wr, 4),
            "win_rate_decisive": round(wr_decisive, 4) if wr_decisive is not None else None,
            "avg_win_usd": round(avg_win, 2) if avg_win is not None else None,
            "avg_loss_usd": round(avg_loss, 2) if avg_loss is not None else None,
            "total_pnl_usd": round(agg["pnl_usd_sum"], 2),
        })

    slices.sort(key=lambda s: (-s["samples"], s["stack"], s["doctrine_version"]))

    return {
        "slices": slices,
        "doctrine_note": (
            "Brain-attribution METADATA only. Promotion / retirement keys on "
            "(lane, seat, doctrine_version) per Patent J — never on the brain "
            "holding the seat. Use /admin/doctrine/scorecard for the "
            "authoritative seat-doctrine view."
        ),
        "endpoint_version": "scorecard_by_brain_v1",
    }

"""Admin endpoint — bracket outcome distribution for the training tile.

Surfaces the live training-signal quality:
  * Total resolved brackets, by tp_hit / sl_hit / timeout band.
  * Confidence-binned distribution — `P(tp_hit | conf ∈ [0.7, 0.8])`
    becomes a single dashboard query.
  * Recent open brackets (top 20) so the operator can spot a
    bracket that's drifting toward an SL hit in real time.

Read-only. No execution side effects.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, Query

from auth import get_current_user
from db import db
from namespaces import WEBULL_BRACKET_INTENTS

router = APIRouter(prefix="/admin/brackets", tags=["brackets"])


# ── confidence binning helper ─────────────────────────────────────


_BINS = [
    (0.0, 0.3, "0.0-0.3"),
    (0.3, 0.5, "0.3-0.5"),
    (0.5, 0.7, "0.5-0.7"),
    (0.7, 0.85, "0.7-0.85"),
    (0.85, 1.01, "0.85-1.0"),
]


def _bin_label(conf: float) -> str:
    for lo, hi, label in _BINS:
        if lo <= conf < hi:
            return label
    return "out_of_range"


# ── endpoints ─────────────────────────────────────────────────────


@router.get("/distribution")
async def bracket_distribution(
    window_hours: int = Query(default=24, ge=1, le=24 * 30),
    _user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    """Aggregate outcome distribution over the last `window_hours`.

    Returns:
        {
          window_hours, total_resolved, total_open,
          by_label:    {tp_hit: int, sl_hit: int, timeout: int},
          by_confidence_bin: {
            "0.0-0.3":  {tp_hit, sl_hit, timeout, total, tp_rate},
            ...
          },
          recent_open: [<top 20 open brackets by opened_at desc>],
        }
    """
    cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=window_hours)
    ).isoformat()

    resolved_cursor = db[WEBULL_BRACKET_INTENTS].find(
        {"status": "resolved", "resolved_at": {"$gte": cutoff}},
        {"_id": 0},
    )
    resolved = await resolved_cursor.to_list(length=5000)

    open_cursor = db[WEBULL_BRACKET_INTENTS].find(
        {"status": "open"}, {"_id": 0},
    ).sort("opened_at", -1)
    open_rows = await open_cursor.to_list(length=20)

    by_label: Counter[str] = Counter()
    by_bin: dict[str, Counter[str]] = defaultdict(Counter)

    for row in resolved:
        label = row.get("outcome_label") or "unknown"
        by_label[label] += 1
        bin_label = _bin_label(float(row.get("confidence") or 0.0))
        by_bin[bin_label][label] += 1
        by_bin[bin_label]["total"] += 1

    # Build the by_confidence_bin response with derived tp_rate.
    by_bin_out: dict[str, dict[str, Any]] = {}
    for _, _, label in _BINS:
        counts = by_bin.get(label, Counter())
        total = counts.get("total", 0)
        tp = counts.get("tp_hit", 0)
        by_bin_out[label] = {
            "tp_hit": tp,
            "sl_hit": counts.get("sl_hit", 0),
            "timeout": counts.get("timeout", 0),
            "total": total,
            "tp_rate": (tp / total) if total > 0 else None,
        }

    # Open brackets — trim to displayable fields for the tile.
    recent_open: list[dict[str, Any]] = []
    for row in open_rows:
        recent_open.append({
            "bracket_id": row.get("bracket_id"),
            "symbol": row.get("symbol"),
            "lane": row.get("lane"),
            "side": row.get("side"),
            "stack": row.get("stack"),
            "confidence": row.get("confidence"),
            "entry_price": row.get("entry_price"),
            "target_price": row.get("target_price"),
            "stop_price": row.get("stop_price"),
            "opened_at": row.get("opened_at"),
            "expires_at": row.get("expires_at"),
        })

    return {
        "window_hours": window_hours,
        "total_resolved": len(resolved),
        "total_open": await db[WEBULL_BRACKET_INTENTS].count_documents(
            {"status": "open"},
        ),
        "by_label": {
            "tp_hit": by_label.get("tp_hit", 0),
            "sl_hit": by_label.get("sl_hit", 0),
            "timeout": by_label.get("timeout", 0),
        },
        "by_confidence_bin": by_bin_out,
        "recent_open": recent_open,
        "doctrine_note": (
            "Training-signal tile: brain-stated target/stop thesis → "
            "categorical outcome labels. Higher confidence bins SHOULD "
            "show higher tp_rate; if the bin curve is flat, confidence "
            "is uncalibrated and the wrapper dampener / scorecard need "
            "tuning."
        ),
    }

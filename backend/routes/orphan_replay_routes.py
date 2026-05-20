"""
Orphan Replay — Doctrine (c) Calibration Report
================================================

Replays every orphan broker fill (UV-classified executions ingested
by the watchdog/ingester) through the current doctrine (c) gate
chain and reports how each WOULD have been handled if MC had owned
the signal.

This is a calibration tool, not a training tool. No memory is
re-classified by this endpoint — that's an explicit operator action
via `/api/admin/memory-kernel/quarantine/{id}/promote-to-so`.

The synthesized snapshot for each fill uses lane-typical values
based on the symbol's universe:

  * Mag-7 / large-cap NYSE-listed names → spread_bps=5, volume_24h_usd huge,
    quality=A, fully tradable session.
  * Lower-cap / less-liquid names → spread_bps=12.

This is sufficient for calibrating the LANE_SPREAD_CAP and
GOVERNOR_DAMPENERS tables — the question is whether *the doctrine*
would route Mag-7 momentum scalps correctly, not whether we know
their exact bid/ask at 13:23:02 UTC on 5/18.

Mount path: `/api/admin/replay/orphan-doctrine-c-report` (under the
`api_router` prefix).
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query

from auth import get_current_user
from db import db
from services.memory_kernel import Provenance


router = APIRouter(prefix="/admin/replay", tags=["replay"])


# Hand-tuned spread proxies for the universe MC actually trades.
# The orphan corpus was Mag-7 + a few BTC; these proxies cover that.
_MAG7 = {"AAPL", "MSFT", "GOOGL", "GOOG", "AMZN", "META", "NVDA", "TSLA"}


def _synthesize_snapshot(symbol: str, lane: str) -> Dict[str, Any]:
    """Lane-typical snapshot for calibration replay."""
    sym = (symbol or "").upper()
    if lane == "crypto":
        spread_bps = 30 if sym.startswith("BTC") else 60
        return {
            "symbol": sym, "spread_bps": spread_bps,
            "volume_24h_usd": 50_000_000_000 if sym.startswith("BTC") else 5_000_000_000,
            "consecutive_losses": 0, "daily_pnl_usd": 0.0,
        }
    spread_bps = 5 if sym in _MAG7 else 12
    return {
        "symbol": sym, "spread_bps": spread_bps,
        "volume_24h_usd": 10_000_000_000,
        "consecutive_losses": 0, "daily_pnl": 0.0,
    }


def _classify_replay_outcome(snapshot: Dict[str, Any], lane: str) -> Dict[str, Any]:
    """Pure replay — no DB writes, no live broker calls.

    Mirrors the live gate chain's logic for the RoadGuard +
    Governor-dampener slice. Anything that requires authority /
    schema / broker state is skipped because the orphans never
    had any of those.
    """
    from shared.crypto.doctrine.crypto_brain_sidecars import (
        GOVERNOR_DAMPENERS,
    )

    spread = float(snapshot.get("spread_bps", 9999))
    cap = 200.0 if lane == "crypto" else 50.0

    if spread > cap:
        return {
            "outcome": "roadguard_kill",
            "reason": f"spread {spread:.1f} > {cap:.0f} bps cap (lane={lane})",
            "risk_multiplier": 0.0,
        }

    # In the live chain, governor dampens on conditions. The synthesized
    # snapshot is intentionally A-quality / no-loss, so we report what
    # the BASE governor-multiplier would be for a Mag-7 scalp:
    # spread ≤ "tight" → no WIDE_SPREAD dampener fires; quality=A → 1.0.
    # We surface the table so the report shows the threshold each fill
    # was checked against.
    dampener_applied = None
    multiplier = 1.0
    # Apply WIDE_SPREAD only if it crosses the brain-side advisor cap
    # (50 bps for equities is the RoadGuard kill; the brain-side "wide"
    # dampener fires earlier, around 20 bps for tight-spread strategies).
    brain_wide_spread = 20.0 if lane == "equity" else 80.0
    if spread > brain_wide_spread:
        multiplier *= GOVERNOR_DAMPENERS["WIDE_SPREAD"]
        dampener_applied = "WIDE_SPREAD"

    return {
        "outcome": "would_allow" if multiplier == 1.0 else "would_dampen",
        "reason": dampener_applied or "clean_setup",
        "risk_multiplier": multiplier,
    }


def _infer_lane(symbol: str) -> str:
    sym = (symbol or "").upper()
    if "/" in sym or sym.endswith("-USD") or sym.startswith(("BTC", "ETH", "DOGE", "SOL", "TON")):
        return "crypto"
    return "equity"


@router.get("/orphan-doctrine-c-report")
async def orphan_doctrine_c_report(
    hours: int = Query(default=720, ge=1, le=8760),
    limit: int = Query(default=2000, ge=1, le=10000),
    _user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Run every orphan fill through doctrine (c) and aggregate.

    Returns:
      * `total` — orphans evaluated
      * `outcomes` — counts of roadguard_kill / would_dampen / would_allow
      * `by_symbol` — per-symbol breakdown (top 25)
      * `by_source_stack` — which "brain" produced each orphan
      * `roadguard_cap_summary` — distribution of spread proxies vs cap
      * `calibration_signal` — narrative hint about whether the caps
        feel right for this corpus

    Read-only. No memory provenance is changed.
    """
    since = datetime.now(timezone.utc) - timedelta(hours=hours)

    # Orphans = UV-class execution memories from the kernel ledger.
    # We don't pull from quarantine because reclassified-to-SO rows
    # have already left UV and shouldn't be re-replayed.
    cursor = (
        db.memory_kernel_ledger
        .find({
            "memory_type": "execution",
            "$or": [
                {"provenance": Provenance.UV.value},
                {"provenance": Provenance.SO.value, "reclassified_from": Provenance.UV.value},
            ],
            "created_at": {"$gte": since},
        })
        .limit(limit)
    )

    outcomes_counter: Counter = Counter()
    by_symbol_counter: Counter = Counter()
    by_source_counter: Counter = Counter()
    spread_buckets: Counter = Counter()
    items: List[Dict[str, Any]] = []
    total = 0

    async for mem in cursor:
        total += 1
        payload = mem.get("payload") or {}
        symbol = payload.get("symbol") or "UNKNOWN"
        lane = _infer_lane(symbol)
        snapshot = _synthesize_snapshot(symbol, lane)
        verdict = _classify_replay_outcome(snapshot, lane)

        outcomes_counter[verdict["outcome"]] += 1
        by_symbol_counter[symbol] += 1
        by_source_counter[mem.get("source_stack") or "unknown"] += 1
        spread = snapshot.get("spread_bps", 0)
        if spread <= 10:
            spread_buckets["≤10 bps"] += 1
        elif spread <= 25:
            spread_buckets["11–25 bps"] += 1
        elif spread <= 100:
            spread_buckets["26–100 bps"] += 1
        else:
            spread_buckets["100+ bps"] += 1

        # Keep first 100 worked-examples for the operator UI.
        if len(items) < 100:
            items.append({
                "memory_id": mem.get("memory_id"),
                "symbol": symbol,
                "lane": lane,
                "side": payload.get("side"),
                "filled_qty": payload.get("filled_qty"),
                "filled_avg_price": payload.get("filled_avg_price"),
                "submitted_at": payload.get("submitted_at"),
                "alpaca_source": payload.get("alpaca_source"),
                "replay_snapshot": snapshot,
                "replay_outcome": verdict,
            })

    # ─── Narrative calibration signal ────────────────────────────────
    allow_pct = (outcomes_counter["would_allow"] / total * 100) if total else 0
    dampen_pct = (outcomes_counter["would_dampen"] / total * 100) if total else 0
    kill_pct = (outcomes_counter["roadguard_kill"] / total * 100) if total else 0

    if total == 0:
        cal_signal = "No orphans in window — corpus is clean."
    elif kill_pct > 50:
        cal_signal = (
            f"⚠ RoadGuard would kill {kill_pct:.1f}% of this corpus. "
            f"If these were legitimate signals (e.g. wide-spread crypto), "
            f"the lane cap may be too tight."
        )
    elif allow_pct > 80:
        cal_signal = (
            f"✓ {allow_pct:.1f}% would have passed cleanly under doctrine (c). "
            f"Caps feel well-calibrated for the orphan corpus. The rogue script "
            f"hit liquid Mag-7 names where the doctrine has no objection."
        )
    else:
        cal_signal = (
            f"{allow_pct:.1f}% allow / {dampen_pct:.1f}% dampen / "
            f"{kill_pct:.1f}% kill. Mixed signal — consider per-symbol drill-down."
        )

    return {
        "ok": True,
        "since": since.isoformat(),
        "total": total,
        "outcomes": dict(outcomes_counter),
        "by_symbol": [
            {"symbol": s, "count": c}
            for s, c in by_symbol_counter.most_common(25)
        ],
        "by_source_stack": [
            {"source_stack": s, "count": c}
            for s, c in by_source_counter.most_common()
        ],
        "spread_buckets": dict(spread_buckets),
        "calibration_signal": cal_signal,
        "doctrine_thresholds": {
            "roadguard_spread_cap_bps": {"equity": 50, "crypto": 200},
            "governor_wide_spread_threshold_bps": {"equity": 20, "crypto": 80},
        },
        "sample_items": items,
    }

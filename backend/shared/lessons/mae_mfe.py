"""MAE/MFE backfill helper.

Computes max adverse / favorable excursion in basis points from the
bar history between `fill_ts` and `exit_ts` (or now if open). Used
by the lesson builder to label intents with their realized risk-vs-
reward shape — not just the final P&L.

Why bps and not %: bps is the canonical unit across the codebase
(spread_enrichment, governor, exposure caps). Keeps lesson rows
comparable across symbol price levels.

Doctrine: pure function — no broker calls, no Mongo writes. Reads
bars via `shared.research.bar_source.load_recent_bars` so the data
priority (broker first, then alts) matches what the brain saw.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from shared.research.bar_source import DEFAULT_TF_BY_LANE, load_recent_bars


def _to_dt(ts_or_iso) -> Optional[datetime]:
    if ts_or_iso is None:
        return None
    if isinstance(ts_or_iso, datetime):
        return ts_or_iso if ts_or_iso.tzinfo else ts_or_iso.replace(tzinfo=timezone.utc)
    try:
        s = str(ts_or_iso).replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except Exception:  # noqa: BLE001
        return None


def _bar_ts(bar: dict) -> Optional[datetime]:
    ts = bar.get("ts") or bar.get("timestamp")
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(float(ts), tz=timezone.utc)
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except Exception:  # noqa: BLE001
            return None
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    return None


async def compute_mae_mfe_bps(
    *,
    symbol: str,
    lane: str,
    side: str,                       # "BUY" or "SELL"
    fill_price: float,
    fill_ts,
    exit_ts=None,
    tf: Optional[str] = None,
    limit: int = 500,
) -> dict:
    """Return {"mae_bps": float|None, "mfe_bps": float|None, "bars_used": int}.

    MAE/MFE are reported as positive bps from `fill_price`. Side is
    used to orient the excursions:
      * BUY  — MFE = max high-water gain;  MAE = max drawdown
      * SELL — MFE = max low-water gain;   MAE = max upmove

    Returns Nones when fewer than 2 bars in the window — caller is
    expected to leave the lesson's MAE/MFE blank rather than zero them
    (which would make a no-data bar look like a perfect scratch).
    """
    if fill_price is None or fill_price <= 0:
        return {"mae_bps": None, "mfe_bps": None, "bars_used": 0}
    side_u = (side or "").upper()
    if side_u not in {"BUY", "SELL"}:
        return {"mae_bps": None, "mfe_bps": None, "bars_used": 0}

    bars, _src = await load_recent_bars(
        symbol,
        tf=(tf or DEFAULT_TF_BY_LANE.get(lane, "1h")),
        limit=limit,
    )
    if not bars:
        return {"mae_bps": None, "mfe_bps": None, "bars_used": 0}

    fill_dt = _to_dt(fill_ts)
    exit_dt = _to_dt(exit_ts) if exit_ts else None

    # Filter to bars whose timestamp is in [fill_ts, exit_ts or now].
    window = []
    for b in bars:
        bts = _bar_ts(b)
        if bts is None:
            continue
        if fill_dt and bts < fill_dt:
            continue
        if exit_dt and bts > exit_dt:
            continue
        window.append(b)

    if len(window) < 2:
        return {"mae_bps": None, "mfe_bps": None, "bars_used": len(window)}

    highest = max(float(b["h"]) for b in window)
    lowest = min(float(b["l"]) for b in window)

    # bps deltas from fill_price (sign-corrected per side below).
    bps_up = (highest - fill_price) / fill_price * 10_000
    bps_down = (fill_price - lowest) / fill_price * 10_000

    if side_u == "BUY":
        mfe = max(0.0, bps_up)
        mae = max(0.0, bps_down)
    else:  # SELL
        mfe = max(0.0, bps_down)
        mae = max(0.0, bps_up)

    return {
        "mae_bps": round(mae, 2),
        "mfe_bps": round(mfe, 2),
        "bars_used": len(window),
    }

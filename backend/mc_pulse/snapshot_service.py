"""SnapshotService — the ONE market-truth builder per pulse.

Reads the latest OHLCV bars off `shared_ohlcv_bars` for each
symbol in the configured universe, wraps them in immutable
`MarketSnapshot` objects, and hands them to the pulse loop.

Doctrine: brains never fetch data. MC does it once per pulse
and hands each brain the same frozen view. See MC_PULSE.md §2.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Iterable, Optional

from db import db
from mc_pulse.snapshot import MarketSnapshot, build_snapshot

logger = logging.getLogger("mc_pulse.snapshot_service")

# Freshness cap: bars older than this get skipped. Same rationale
# as the runner's implicit freshness gate — a 20-minute-old bar
# is not the current market. Audit row #9 captured explicitly.
MAX_BAR_AGE_SECONDS = 300


def _default_universe(lane: str) -> list[str]:
    """Bootstrap universe from env. Refactor to `MC_UNIVERSE_*`
    keys in Phase 2. For now, the runner-side env vars keep
    working so we don't diverge from what Camino was already
    evaluating."""
    if lane == "equity":
        raw = os.environ.get("MC_UNIVERSE_EQUITY") or os.environ.get(
            "SYMBOLS_ALPHA", "NVDA,MSFT,AAPL,TSLA",
        )
    else:
        raw = os.environ.get("MC_UNIVERSE_CRYPTO") or os.environ.get(
            "SYMBOLS_ALPHA_CRYPTO", "BTC/USD,ETH/USD",
        )
    return [s.strip().upper() for s in raw.split(",") if s.strip()]


async def build_all(
    now: Optional[datetime] = None,
    universe: Optional[dict[str, list[str]]] = None,
) -> list[MarketSnapshot]:
    """Build one snapshot per (lane, symbol) in the universe.

    Missing / stale bars are skipped (logged, not raised) — the
    pulse continues with whatever coverage it can get. A brain
    that gets fewer snapshots than expected simply has less to
    say this tick.

    Bounded reads: every Mongo call uses `max_time_ms(1500)` +
    catches exceptions. This is the operator dashboard's hot
    path — a slow Atlas read must not stall the pulse.
    """
    now = now or datetime.now(timezone.utc)
    universe = universe or {
        "equity": _default_universe("equity"),
        "crypto": _default_universe("crypto"),
    }
    # Fetch every open position ONCE per pulse. Audit row #7:
    # brains must never call Mongo themselves — MC materializes
    # the position context and attaches to each snapshot.
    positions_by_symbol = await _fetch_open_positions()

    snapshots: list[MarketSnapshot] = []
    for lane, symbols in universe.items():
        for symbol in symbols:
            snap = await _build_one(
                lane, symbol, now,
                positions_for_symbol=positions_by_symbol.get(symbol, {}),
            )
            if snap is not None:
                snapshots.append(snap)
    return snapshots


async def _fetch_open_positions() -> dict[str, dict[str, dict]]:
    """Return `{symbol: {brain_id: position_dict}}` for every open
    position. One bounded Mongo scan per pulse.

    Position state maps: `state ∈ {open, pending_open, held}` count
    as "held"; `pending_close, closed` do not. Direction / signed
    qty extraction stays defensive — the runner audit noted
    inconsistent field names across historical rows.
    """
    result: dict[str, dict[str, dict]] = {}
    try:
        cursor = db["shared_positions"].find(
            {"state": {"$in": ["open", "pending_open", "held"]}},
            {
                "_id": 0, "symbol": 1, "direction": 1, "state": 1,
                "proposed_by": 1, "runtime": 1, "brain": 1,
                "signed_qty": 1, "qty": 1, "position_id": 1,
                "created_at": 1,
            },
        ).max_time_ms(1500).limit(500)
    except Exception as exc:  # noqa: BLE001
        logger.warning("open-position fetch failed: %s (returning empty)", exc)
        return result
    try:
        async for doc in cursor:
            sym = (doc.get("symbol") or "").upper()
            if not sym:
                continue
            # Attribution priority: `proposed_by` (canonical) →
            # `brain` (short field) → `runtime` (legacy).
            brain_id = (
                doc.get("proposed_by")
                or doc.get("brain")
                or doc.get("runtime")
                or ""
            ).lower()
            if not brain_id:
                continue
            result.setdefault(sym, {})[brain_id] = {
                "position_id": doc.get("position_id"),
                "direction": doc.get("direction"),
                "state": doc.get("state"),
                "signed_qty": doc.get("signed_qty"),
                "qty": doc.get("qty"),
                "created_at": doc.get("created_at"),
            }
    except Exception as exc:  # noqa: BLE001
        logger.warning("open-position iteration failed: %s", exc)
    return result


async def _build_one(
    lane: str, symbol: str, now: datetime,
    *,
    positions_for_symbol: Optional[dict] = None,
) -> Optional[MarketSnapshot]:
    # Try 1m first; fall back to 5m for equity (some feeders don't
    # publish 1m) and 1d for crypto (Kraken daily bars are common
    # in preview). If nothing fresh exists, skip cleanly.
    tf_preference = ["1m", "5m"] if lane == "equity" else ["1m", "5m", "1d"]
    doc = None
    used_tf = None
    for tf in tf_preference:
        try:
            candidate = await db["shared_ohlcv_bars"].find_one(
                {"symbol": symbol, "tf": tf},
                sort=[("ts", -1)],
                max_time_ms=1500,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "snapshot fetch failed lane=%s symbol=%s tf=%s err=%s",
                lane, symbol, tf, exc,
            )
            continue
        if candidate:
            doc = candidate
            used_tf = tf
            break
    if not doc:
        return None

    ts_raw = doc.get("ts")
    try:
        bar_ts = (
            datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            if isinstance(ts_raw, str) else ts_raw
        )
        if bar_ts is None:
            return None
        if bar_ts.tzinfo is None:
            bar_ts = bar_ts.replace(tzinfo=timezone.utc)
    except (AttributeError, ValueError):
        return None

    # Freshness cap scales with the tf. 1m bars go stale in 5 min;
    # 5m in 15 min; 1d in 3 days. Avoids fake liveness on daily
    # bars while still rejecting an hours-old 1m gap.
    max_age = {
        "1m": MAX_BAR_AGE_SECONDS,
        "5m": MAX_BAR_AGE_SECONDS * 3,
        "1d": 3 * 86400,
    }.get(used_tf, MAX_BAR_AGE_SECONDS)
    age = (now - bar_ts).total_seconds()
    if age > max_age:
        return None

    close = doc.get("close") or doc.get("c") or doc.get("open") or doc.get("o")
    if close is None or float(close) <= 0:
        return None

    # Minimal indicator set at v0.1 — the fields the legacy
    # NeutralAdversarialBrain looks at. Feeders currently attach
    # these onto the bar doc; brains that want more must wait
    # until the indicator layer is centralized in Phase 2.
    indicators = {
        k: float(v) for k, v in doc.items()
        if k in {"rvol", "ema20", "ema50", "macd_hist", "atr",
                 "vwap", "spread_bps"}
        and v is not None
    }
    return build_snapshot(
        symbol=symbol,
        lane=lane,
        timestamp=bar_ts,
        price=Decimal(str(close)),
        indicators=indicators,
        market_state=str(doc.get("regime") or "unknown"),
        position_context=positions_for_symbol or {},
    )


async def sample_universe_size() -> dict[str, int]:
    """Diagnostic helper — return current universe sizes. Cheap."""
    return {
        lane: len(_default_universe(lane))
        for lane in ("equity", "crypto")
    }

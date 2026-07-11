"""SnapshotService — the ONE market-truth builder per pulse.

Reads a window of recent bars off `shared_ohlcv_bars` for each
symbol in the configured universe, feeds them through the
canonical Camino feature builder, and wraps the result in an
immutable `MarketSnapshot` carrying:

    * `feature_snapshot`  — the FULL feature dict the legacy
                            NeutralAdversarialBrain core reads
                            (price_change_pct, trend_score, rsi,
                            spread_bps, volatility, ...). Same
                            code path the runner uses via the
                            canonical builder — no independent
                            derivation.
    * `bar_identity`      — authoritative BarIdentity emitted by
                            the source record (open/close/tf).
                            Feeds ParityKey composition; runner
                            and pulse both derive from THIS, not
                            from `datetime.now()`.

Doctrine: brains never fetch data. MC does it once per pulse and
hands each brain the same frozen view. See MC_PULSE.md §2.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from db import db
from mc_pulse.feature_builders.camino import build_camino_features
from mc_pulse.parity_key import bar_identity_from_source
from mc_pulse.snapshot import MarketSnapshot, build_snapshot

logger = logging.getLogger("mc_pulse.snapshot_service")

# Freshness cap: bars older than this get skipped. Same rationale
# as the runner's implicit freshness gate — a 20-minute-old bar
# is not the current market. Audit row #9 captured explicitly.
MAX_BAR_AGE_SECONDS = 300

# How many recent bars to feed the canonical builder. 30 bars is
# the minimum "hot branch" size (the builder needs >= 20 for the
# hot path) with a safety margin for missing bars. Keep the read
# bounded — Atlas `find().sort(...).limit()` gives us a fast index-
# scan when `(symbol, tf, ts)` is indexed.
BAR_WINDOW = 30

# Prior-daily-volume baseline for session_features RVOL. Runner
# pulls this from MC's `/technical` endpoint; we pull it directly
# from `shared_ohlcv_bars` at `tf=1d` (same source that endpoint
# reads from).
DAILY_BASELINE_LOOKBACK = 20


def _default_universe(lane: str) -> list[str]:
    """Bootstrap universe from env. Same env keys the runner reads
    so preview and prod agree on membership."""
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

    Missing / stale bars skip cleanly — the pulse continues with
    whatever coverage it can get. A brain that gets fewer
    snapshots than expected simply has less to say this tick.
    """
    now = now or datetime.now(timezone.utc)
    universe = universe or {
        "equity": _default_universe("equity"),
        "crypto": _default_universe("crypto"),
    }
    # Fetch every open position ONCE per pulse.
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
    position. One bounded Mongo scan per pulse."""
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


async def _fetch_bar_window(symbol: str, lane: str) -> tuple[list[dict], str]:
    """Return `(bars_asc, tf)` — most recent BAR_WINDOW bars in
    chronological order, plus the timeframe that fed them.

    Falls back through tf preferences (1m → 5m for equity; 1m →
    5m → 1d for crypto) until it finds a symbol/tf pair with
    coverage. Empty list means "no coverage at any tf."
    """
    tf_preference = ["1m", "5m"] if lane == "equity" else ["1m", "5m", "1d"]
    for tf in tf_preference:
        try:
            cursor = db["shared_ohlcv_bars"].find(
                {"symbol": symbol, "tf": tf},
                sort=[("ts", -1)],
            ).max_time_ms(1500).limit(BAR_WINDOW)
            docs = await cursor.to_list(BAR_WINDOW)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "snapshot bar fetch failed lane=%s symbol=%s tf=%s err=%s",
                lane, symbol, tf, exc,
            )
            continue
        if docs:
            docs.reverse()   # sorted asc by ts (oldest → newest)
            return docs, tf
    return [], ""


async def _fetch_daily_baseline(symbol: str) -> Optional[list[float]]:
    """Return the last DAILY_BASELINE_LOOKBACK daily volumes
    (`tf=1d`) as the prior-sessions baseline for RVOL. Falls
    back to None (session_features handles that) when the daily
    coverage is thin."""
    try:
        cursor = db["shared_ohlcv_bars"].find(
            {"symbol": symbol, "tf": "1d"},
            {"v": 1, "ts": 1, "_id": 0},
            sort=[("ts", -1)],
        ).max_time_ms(1500).limit(DAILY_BASELINE_LOOKBACK)
        docs = await cursor.to_list(DAILY_BASELINE_LOOKBACK)
    except Exception as exc:  # noqa: BLE001
        logger.warning("daily baseline fetch failed symbol=%s err=%s", symbol, exc)
        return None
    if not docs:
        return None
    return [float(d.get("v") or 0.0) for d in docs]


def _parse_iso(raw) -> Optional[datetime]:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


async def _build_one(
    lane: str, symbol: str, now: datetime,
    *,
    positions_for_symbol: Optional[dict] = None,
) -> Optional[MarketSnapshot]:
    bars, used_tf = await _fetch_bar_window(symbol, lane)
    if not bars:
        return None

    latest = bars[-1]
    latest_ts = _parse_iso(latest.get("ts"))
    if latest_ts is None:
        return None

    # Freshness cap — same logic as before, tf-scaled.
    max_age = {
        "1m": MAX_BAR_AGE_SECONDS,
        "5m": MAX_BAR_AGE_SECONDS * 3,
        "1d": 3 * 86400,
    }.get(used_tf, MAX_BAR_AGE_SECONDS)
    age = (now - latest_ts).total_seconds()
    if age > max_age:
        return None

    # `shared_ohlcv_bars` labels its `ts` field as the bar OPEN
    # (verified: polygon_equity._row_to_bar uses trading-day
    # midnight UTC; intraday feeders follow the same convention).
    bar = bar_identity_from_source(
        timeframe=used_tf,
        bar_timestamp=latest_ts,
        timestamp_semantics="open",
        source="shared_ohlcv_bars",
    )

    # Canonical Camino feature builder — the SAME code the runner
    # will call once we hook it. No independent field derivation
    # in the pulse path.
    prior_daily = None
    if used_tf != "1d":
        # Runner enriches intraday windows with a 20-day daily
        # baseline; do the same here so `relative_volume` is
        # computable on the pulse path too.
        prior_daily = await _fetch_daily_baseline(symbol)
    feature_snapshot, _setup = build_camino_features(
        symbol=symbol,
        lane=lane,
        bars=bars,
        prior_daily_volumes=prior_daily,
        market_regime=None,          # no per-tick regime in pulse v0.1
    )
    fallback_used = not bool(feature_snapshot.get("real_market_data"))

    close = feature_snapshot.get("price") or latest.get("c") or latest.get("close")
    if close is None or float(close) <= 0:
        return None

    # `indicators` — narrow read-only view of the numeric feature
    # subset that lives next to the bar (rvol / ema20 / macd_hist
    # / vwap / atr / spread_bps if a feeder attached them). Kept
    # for non-Camino brains that haven't been migrated yet;
    # Camino reads `feature_snapshot` directly.
    indicators = {
        k: float(v) for k, v in latest.items()
        if k in {"rvol", "ema20", "ema50", "macd_hist", "atr",
                 "vwap", "spread_bps"}
        and v is not None
    }

    return build_snapshot(
        symbol=symbol,
        lane=lane,
        timestamp=latest_ts,
        price=Decimal(str(close)),
        indicators=indicators,
        market_state=str(latest.get("regime") or feature_snapshot.get("market_regime") or "unknown"),
        position_context=positions_for_symbol or {},
        source_tf=used_tf,
        source_bar_count=len(bars),
        bar_identity=bar,
        source_bar_id=str(latest.get("_id") or latest.get("ts") or ""),
        feature_snapshot=feature_snapshot,
        fallback_used=fallback_used,
    )


async def sample_universe_size() -> dict[str, int]:
    """Diagnostic helper — return current universe sizes. Cheap."""
    return {
        lane: len(_default_universe(lane))
        for lane in ("equity", "crypto")
    }

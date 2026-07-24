"""Webull equity feeder — INTRADAY OHLCV via Open API `market_data.get_history_bar`.

Doctrine pin (2026-07-11, operator directive):
    Broker-native data is the source of truth for a trading system —
    it's what you can actually trade against. Vendor feeds (Finnhub,
    Polygon) become BACKUP / continuity, not primary.

    Webull's Open API "Advanced Quotes" entitlement (Nasdaq Basic —
    Non-Display) is authorized free through 2027-06-10 and exposes
    historical minute-bar data via `market_data.get_history_bar(
    symbol, "US_STOCK", "M1"|"M5", count=N)`. That's the SAME
    entitlement `webull_quotes.equity_bars()` already uses for
    doctrine enrichment — we just consume it for OHLCV writes now.

    Feeder writes to `shared_ohlcv_bars` with `source="webull"`,
    `tf="1m"|"5m"`. The Finnhub feeder keeps running in parallel;
    since consumers select via `ORDER BY ts DESC`, whichever source
    is freshest wins automatically. When Webull errors, Finnhub
    naturally becomes the freshest bar for that symbol — no
    coordinator logic required.

Configuration (backend/.env):
    WEBULL_OHLC_FEEDER_ENABLED     "true" to enable (default true)
    WEBULL_OHLC_POLL_INTERVAL_SEC  default 60 (5m bars close every 5min)
    WEBULL_OHLC_BAR_COUNT          default 30 (bars per call)
    WEBULL_OHLC_UNIVERSE           CSV override (else patterns_universe)

Failure modes:
    * Circuit breaker in `webull_quotes` returns None → skip symbol
    * Empty bars response → `feeder_health_audit` row, continue
    * DB write error → `feeder_health_audit` row, continue
    All fail-soft. A wedged Webull must not stop the pulse.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from db import db
from shared.feeders.feeder_health import record_feeder_health
from shared.market_data.webull_quotes import get_quotes_client
from shared.technicals import _persist_bar

logger = logging.getLogger(__name__)

PROVIDER = "webull"
DEFAULT_POLL_INTERVAL_SEC = 60
DEFAULT_BAR_COUNT = 30

# Webull's `get_history_bar` timespan literals. Same for both the
# feeder and the doctrine enricher. Kept as a mapping so extending
# to M15/H1 later is one line.
TF_TO_WEBULL_TIMESPAN = {
    "1m": "M1",
    "5m": "M5",
}


def _env_bool(key: str, default: bool) -> bool:
    v = os.environ.get(key)
    if v is None or v == "":
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key) or default)
    except (TypeError, ValueError):
        return default


async def _discover_universe() -> list[str]:
    """Read the equity universe.

    Doctrine (2026-07-15, iter-30 P4):
        Primary source is `live_universe` (built every 15min by
        `shared/universe/refresher.py` from Webull screener). Falls
        back to `patterns_universe` and then a hard-coded starter
        list so a broker outage or cold-boot never leaves the
        feeder with an empty universe.

        `WEBULL_OHLC_UNIVERSE` env still overrides everything —
        useful for smoke tests.
    """
    override = os.environ.get("WEBULL_OHLC_UNIVERSE", "").strip()
    if override:
        return sorted({s.strip().upper() for s in override.split(",") if s.strip()})

    # ── Primary: live_universe (broker-driven, refreshed every 15min) ──
    try:
        from shared.universe.live_universe import read_universe  # noqa: WPS433
        doc = await read_universe("equity")
        if doc:
            # Preserve RANK order (pins → core → best-scored screener)
            # so the budget slice keeps priority names fresh every tick.
            syms: list[str] = []
            for s in (doc.get("symbols") or []):
                sym = (s.get("canonical_symbol") or "").upper()
                if sym and s.get("tradable", True) and sym not in syms:
                    syms.append(sym)
            if syms:
                return syms
    except Exception as e:  # noqa: BLE001
        logger.warning("webull_ohlc: live_universe discovery failed: %r", e)

    # ── Fallback: legacy patterns_universe ──
    try:
        cursor = db["patterns_universe"].find(
            {"lane": "equity", "active": True},
            {"symbol": 1, "_id": 0},
        ).max_time_ms(2000).limit(200)
        docs = await cursor.to_list(200)
        syms = sorted({(d.get("symbol") or "").upper() for d in docs if d.get("symbol")})
        if syms:
            return syms
    except Exception as e:  # noqa: BLE001
        logger.warning("webull_ohlc: patterns_universe discovery failed: %r", e)
    return ["NVDA", "MSFT", "AAPL", "TSLA"]        # cold-start fallback


def _parse_webull_bar_ts(raw) -> Optional[datetime]:
    """Webull SDK returns bar timestamps variously as epoch ms,
    epoch s, or ISO strings depending on endpoint. Normalize to
    an aware UTC datetime; return None on unparseable input."""
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    try:
        if isinstance(raw, (int, float)):
            # Heuristic: if > 10^12, it's ms since epoch.
            secs = float(raw) / 1000.0 if float(raw) > 1e12 else float(raw)
            return datetime.fromtimestamp(secs, tz=timezone.utc)
        s = str(raw).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError, OSError):
        return None


def _row_to_internal_bar(symbol: str, tf: str, row: dict) -> Optional[dict]:
    """Translate one Webull history-bar row into our internal
    `shared_ohlcv_bars` schema. Returns None if any required
    field is missing / unparseable — same pattern
    `to_internal_bar` uses on the Kraken side.

    Uses safe optional coercion (no bare `float()` calls) so a
    None/NaN price doesn't crash the whole tick — see doctrine
    step 5 in the 2026-07 iter-27 packet."""
    ts_raw = row.get("timestamp") or row.get("tradeTime") or row.get("time")
    ts = _parse_webull_bar_ts(ts_raw)
    if ts is None:
        return None
    def _f(key: str) -> Optional[float]:
        v = row.get(key)
        if v is None:
            return None
        try:
            n = float(v)
        except (TypeError, ValueError):
            return None
        return n if n == n else None      # rejects NaN
    o = _f("open")
    h = _f("high")
    l = _f("low")
    c = _f("close")
    v = _f("volume")
    if None in (o, h, l, c) or v is None:
        return None
    return {
        "symbol": symbol.upper(),
        "tf": tf,
        "ts": ts.isoformat(),
        "o": o, "h": h, "l": l, "c": c, "v": v,
    }


async def _fetch_and_persist_one(symbol: str, tf: str, count: int) -> int:
    """Pull `count` bars for one (symbol, tf) via Webull; persist
    each as source=webull. Fail-soft."""
    timespan = TF_TO_WEBULL_TIMESPAN.get(tf)
    if timespan is None:
        return 0
    client = get_quotes_client()
    if client is None:
        # Client unavailable (missing creds, circuit-open at boot).
        # Not a per-tick failure — silent skip.
        return 0
    # SDK is sync → offload to executor so we don't block the loop.
    loop = asyncio.get_event_loop()
    try:
        rows = await loop.run_in_executor(
            None, client.equity_bars, symbol, timespan, count,
        )
    except Exception as e:  # noqa: BLE001
        await record_feeder_health(
            provider=PROVIDER, endpoint="equity_bars",
            status_code=None, error_type="sdk_error",
            message=f"{type(e).__name__}: {str(e)[:400]}",
            context={"symbol": symbol, "tf": tf},
        )
        return 0
    if not rows:
        # Empty is common outside RTH — record but don't spam.
        return 0

    written = 0
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        bar = _row_to_internal_bar(symbol, tf, row)
        if bar is None:
            continue
        bar["source"] = PROVIDER
        try:
            await _persist_bar(bar)
            written += 1
        except Exception as e:  # noqa: BLE001
            await record_feeder_health(
                provider=PROVIDER, endpoint="_persist_bar",
                status_code=None, error_type="db_error",
                message=f"{type(e).__name__}: {str(e)[:400]}",
                context={"symbol": symbol, "tf": tf, "ts": bar.get("ts")},
            )
    return written


_rotation = 0


def _budget_slice(universe: list[str]) -> list[str]:
    """API-budget guard for large universes (150-cap, 2026-07-24):
    poll at most WEBULL_OHLC_MAX_SYMBOLS_PER_TICK per cycle. The
    ranked head (pins + core liquid names) refreshes EVERY tick; the
    screener tail rotates across ticks so every symbol stays no more
    than a few minutes stale — fine for 5m-bar doctrines."""
    global _rotation  # noqa: PLW0603
    max_n = _env_int("WEBULL_OHLC_MAX_SYMBOLS_PER_TICK", 90)
    if len(universe) <= max_n:
        return universe
    head_n = min(60, max_n // 2)
    head, tail = universe[:head_n], universe[head_n:]
    k = max_n - head_n
    take = [tail[(_rotation + i) % len(tail)] for i in range(min(k, len(tail)))]
    _rotation = (_rotation + k) % len(tail)
    return head + take


async def _tick() -> dict:
    """One poll cycle: for each symbol in the equity universe,
    pull tf=1m AND tf=5m bars. Rate budget is per-key; the
    circuit breaker in `webull_quotes` guards against runaway."""
    # 2026-07-20: credential-less feeders must be VISIBLE. The old
    # behavior silently skipped every symbol when the quotes client
    # couldn't build (missing WEBULL_APP_KEY/SECRET), leaving zero
    # audit trail — prod starved for 13 days with no alarm. Now the
    # absence itself is recorded every tick so pipeline-doctor and
    # the kill map can name it.
    if get_quotes_client() is None:
        await record_feeder_health(
            provider=PROVIDER, endpoint="_tick",
            status_code=None, error_type="no_client",
            message=(
                "quotes client unavailable — WEBULL_APP_KEY/SECRET missing "
                "(connect via the Webull card on the Intents page) or SDK "
                "not importable. Feeder is running but writing nothing."
            ),
        )
        return {"universe_size": 0, "bars_written": 0, "per_symbol": {}}
    count = _env_int("WEBULL_OHLC_BAR_COUNT", DEFAULT_BAR_COUNT)
    universe = _budget_slice(await _discover_universe())
    total = 0
    per_symbol: dict[str, int] = {}
    for sym in universe:
        n = 0
        for tf in TF_TO_WEBULL_TIMESPAN:
            n += await _fetch_and_persist_one(sym, tf, count)
        per_symbol[sym] = n
        total += n
    if total > 0:
        await record_feeder_health(
            provider=PROVIDER, endpoint="_tick",
            status_code=200, error_type=None,
            message=f"tick ok: universe={len(universe)} bars_written={total}",
            context={"per_symbol": per_symbol},
        )
    return {"universe_size": len(universe), "bars_written": total,
            "per_symbol": per_symbol}


_stop_flag = False
_task: Optional[asyncio.Task] = None


async def _worker_loop() -> None:
    global _stop_flag
    interval = _env_int("WEBULL_OHLC_POLL_INTERVAL_SEC", DEFAULT_POLL_INTERVAL_SEC)
    logger.info(
        "webull_ohlc feeder started: interval=%ss tfs=%s",
        interval, list(TF_TO_WEBULL_TIMESPAN),
    )
    while not _stop_flag:
        try:
            result = await _tick()
            if result.get("bars_written", 0) > 0:
                logger.info(
                    "webull_ohlc tick: universe=%s bars_written=%s",
                    result["universe_size"], result["bars_written"],
                )
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("webull_ohlc tick error: %r", e)
            await record_feeder_health(
                provider=PROVIDER, endpoint="_worker_loop",
                status_code=None, error_type="worker_crash",
                message=str(e)[:500],
            )
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            break


def start_worker_if_enabled() -> None:
    """Idempotent starter, mirrors kraken_ohlc.py."""
    global _task, _stop_flag
    if _task is not None and not _task.done():
        return
    if not _env_bool("WEBULL_OHLC_FEEDER_ENABLED", True):
        logger.info("webull_ohlc feeder disabled via WEBULL_OHLC_FEEDER_ENABLED=false")
        return
    _stop_flag = False
    _task = asyncio.create_task(_worker_loop(), name="webull_ohlc_feeder")


async def stop_worker() -> None:
    global _task, _stop_flag
    _stop_flag = True
    if _task is not None and not _task.done():
        _task.cancel()
        try:
            await _task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _task = None


async def run_now() -> dict:
    """Manual one-shot for the admin re-trigger endpoint."""
    return await _tick()

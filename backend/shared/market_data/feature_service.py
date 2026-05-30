"""Feature service — derives `relative_volume` + `has_news` (and a small
set of related operator-facing facts) from MC's already-stored data.

Operator pattern (2026-02-17):
  Brains currently self-compute snapshot facts. When they boot in
  feature-cold-state, they self-veto with `STUCK_FEATURES_NO_DIVERSITY`
  because their internal volume baselines aren't warm yet — even though
  MC's Finnhub feeder has been writing real bars to
  `shared_ohlcv_bars` for weeks. The fix isn't to duplicate the
  bar-warming work in every brain; it's to expose ONE service endpoint
  that returns a doctrine-pinned set of derived facts the brains can
  read.

Doctrine pins:
  - DERIVED EVIDENCE ONLY. This module reads bars + news; never
    decides BUY/SELL/HOLD, never serves broker keys, never affects
    execution authority.
  - `relative_volume` falls back to `None` (not 0.0) when bars are
    insufficient. 0.0 is a real value with a real semantic ("no
    volume right now"); confusing them silently produces false-
    positive `STUCK_FEATURES_NO_DIVERSITY` self-vetoes downstream.
  - `has_news` falls back to `None` on Finnhub failure (or missing
    `FINNHUB_API_KEY`). The labeler treats `None` as informational,
    not penalized. Real `False` only on a successful empty fetch.
  - Hot path: `relative_volume` is pure aggregation over the
    `shared_ohlcv_bars` collection — no external network.
  - News fetch is cached per-symbol for `NEWS_CACHE_TTL_SEC`. Default
    300s. Operator-tunable via env.

Public callable shape (used by both the HTTP route and any future
internal callers):
  - `compute_relative_volume(symbol, tf, source="finnhub_equity",
        lookback_bars=30, db=db) -> {value, basis_bars, current_v,
        avg_v, ok, reason}`
  - `fetch_has_news(symbol, hours=24) -> {has_news, source, ok,
        reason}`
  - `build_market_snapshot(symbol, tf="5m") -> {symbol, tf, price,
        volume_24h_usd, relative_volume, has_news, last_bar_ts,
        source, ...}`
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import httpx

from db import db as _default_db
from namespaces import SHARED_OHLCV_BARS


logger = logging.getLogger("risedual.feature_service")


# ──────────────────────── Doctrine constants ────────────────────────

# How many recent bars (excluding the current one) define the
# `relative_volume` baseline. 30 bars at 5m timeframe ≈ 2.5 hours of
# intraday — long enough to smooth a single anomaly, short enough that
# stale baselines don't dominate fresh action. Operator-tunable but
# pinned by tripwire so silent drift fails loud.
RELATIVE_VOLUME_LOOKBACK_BARS: int = int(
    os.environ.get("MC_RVOL_LOOKBACK_BARS", "30")
)

# `relative_volume` needs at least this many baseline bars to be
# statistically meaningful. Below this, return `None` rather than a
# noisy float.
MIN_BASIS_BARS: int = 5

# Bar timeframe used by `build_market_snapshot` when the caller doesn't
# specify one. 5m matches Finnhub's default ingest cadence.
DEFAULT_TIMEFRAME: str = "5m"

# Bar source used by `build_market_snapshot` when the caller doesn't
# specify one. Finnhub is the primary US-equity OHLCV feed today.
DEFAULT_SOURCE: str = "finnhub_equity"

# News cache TTL — Finnhub is rate-limited and the news set rarely
# changes minute-to-minute. 300s = 5min default.
NEWS_CACHE_TTL_SEC: int = int(os.environ.get("MC_NEWS_CACHE_TTL_SEC", "300"))

# Finnhub news endpoint. Same key the market-data-key proxy already
# serves to brains. Stays in MC's process; never returned in payloads.
FINNHUB_NEWS_URL: str = "https://finnhub.io/api/v1/company-news"

# Network timeout for the news fetch. Bounded so a slow Finnhub never
# stalls operator dashboards or brain-side feature builds.
NEWS_FETCH_TIMEOUT_S: float = 4.0


# ──────────────────────── In-process caches ────────────────────────

# Simple TTL cache for news lookups: symbol -> (epoch_seconds_when_set,
# {has_news, ok, reason, source}). Process-local; on pod restart we
# re-fetch. Adequate because the snapshot endpoint is operator-driven
# (not high-rate) and the brains will cache further on their side.
_NEWS_CACHE: Dict[str, tuple[float, Dict[str, Any]]] = {}


def _cache_get(symbol: str) -> Optional[Dict[str, Any]]:
    entry = _NEWS_CACHE.get(symbol)
    if not entry:
        return None
    set_at, payload = entry
    if time.time() - set_at > NEWS_CACHE_TTL_SEC:
        _NEWS_CACHE.pop(symbol, None)
        return None
    return payload


def _cache_set(symbol: str, payload: Dict[str, Any]) -> None:
    _NEWS_CACHE[symbol] = (time.time(), payload)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ──────────────────────── compute_relative_volume ────────────────────────


async def compute_relative_volume(
    symbol: str,
    tf: str = DEFAULT_TIMEFRAME,
    source: str = DEFAULT_SOURCE,
    lookback_bars: int = RELATIVE_VOLUME_LOOKBACK_BARS,
    db=None,
) -> Dict[str, Any]:
    """`current_bar_volume / mean(last N bars' volume)`.

    Reads from `shared_ohlcv_bars`. Returns a structured payload
    suitable for both the labeler input (consume `value`) and the
    operator dashboard (display `current_v` / `avg_v` / `basis_bars`).

    Doctrine: returns `value=None` when there aren't enough baseline
    bars OR when the baseline mean is zero (no division-by-zero
    silent NaN). Callers must treat `None` as "unknown, do not
    penalize" — distinct from 0.0 which means "current bar saw zero
    volume against a real baseline".

    Args:
        symbol: uppercase ticker (e.g., "NVDA", "BTC/USD").
        tf: bar timeframe — "5m" default to match the Finnhub feeder.
        source: bar source — "finnhub_equity" default. Crypto callers
                pass "kraken_pro".
        lookback_bars: baseline window. Defaults to env-configured 30.
        db: optional Motor db handle (test-injection seam). Falls back
            to the module-level singleton.

    Returns:
        {
          "value": float | None,           # the RVOL multiple
          "current_v": float | None,       # current-bar volume
          "avg_v": float | None,           # baseline mean
          "basis_bars": int,               # how many bars were used
          "last_bar_ts": str | None,       # ISO ts of the latest bar
          "ok": bool,                      # value is trustable
          "reason": str | None,            # why ok=False (audit trail)
        }
    """
    if db is None:
        db = _default_db

    # +1 because we fetch current + baseline in one query.
    query_limit = max(lookback_bars + 1, MIN_BASIS_BARS + 1)
    rows = await db[SHARED_OHLCV_BARS].find(
        {"source": source, "symbol": symbol.upper(), "tf": tf},
        {"_id": 0, "v": 1, "ts": 1},
    ).sort("ts", -1).limit(query_limit).to_list(length=query_limit)

    if not rows:
        return {
            "value": None, "current_v": None, "avg_v": None,
            "basis_bars": 0, "last_bar_ts": None,
            "ok": False, "reason": "no_bars_for_symbol",
        }

    # Index 0 is the most-recent bar (current). Remainder is baseline.
    current = rows[0]
    baseline = rows[1:]
    current_v = float(current.get("v") or 0.0)
    last_bar_ts = current.get("ts")

    if len(baseline) < MIN_BASIS_BARS:
        return {
            "value": None, "current_v": current_v, "avg_v": None,
            "basis_bars": len(baseline), "last_bar_ts": last_bar_ts,
            "ok": False,
            "reason": f"basis_bars_below_minimum_{MIN_BASIS_BARS}",
        }

    baseline_volumes = [float(b.get("v") or 0.0) for b in baseline]
    avg_v = sum(baseline_volumes) / len(baseline_volumes)
    if avg_v <= 0:
        return {
            "value": None, "current_v": current_v, "avg_v": avg_v,
            "basis_bars": len(baseline_volumes),
            "last_bar_ts": last_bar_ts,
            "ok": False, "reason": "baseline_mean_zero",
        }

    rvol = current_v / avg_v
    return {
        "value": round(rvol, 4),
        "current_v": current_v,
        "avg_v": round(avg_v, 2),
        "basis_bars": len(baseline_volumes),
        "last_bar_ts": last_bar_ts,
        "ok": True,
        "reason": None,
    }


# ──────────────────────── fetch_has_news ────────────────────────


async def fetch_has_news(
    symbol: str,
    hours: int = 24,
    *,
    _http_client: Optional[httpx.AsyncClient] = None,
) -> Dict[str, Any]:
    """Did Finnhub publish any company-news headline for `symbol` in
    the last `hours`? Cached per-symbol for NEWS_CACHE_TTL_SEC.

    Doctrine: failure modes — missing API key, network timeout, 4xx/5xx
    from Finnhub — all return `has_news=None, ok=False, reason=<...>`.
    The labeler treats None as informational, never penalizing the
    symbol. Only a successful, empty-result fetch returns
    `has_news=False`.

    Args:
        symbol: uppercase ticker.
        hours: lookback window in hours.
        _http_client: optional injected client for tests.

    Returns:
        {
          "has_news": bool | None,
          "headline_count": int | None,
          "ok": bool,
          "source": "finnhub" | None,
          "reason": str | None,
          "from_cache": bool,
        }
    """
    cached = _cache_get(symbol)
    if cached is not None:
        # Stamp cache hit but don't mutate the stored object.
        return {**cached, "from_cache": True}

    api_key = (os.environ.get("FINNHUB_API_KEY") or "").strip()
    if not api_key:
        result = {
            "has_news": None, "headline_count": None,
            "ok": False, "source": None,
            "reason": "finnhub_api_key_missing",
            "from_cache": False,
        }
        # Cache the negative so we don't hammer env every call.
        _cache_set(symbol, {k: v for k, v in result.items() if k != "from_cache"})
        return result

    now = datetime.now(timezone.utc)
    end_iso = now.date().isoformat()
    start_iso = (now - _timedelta(hours=hours)).date().isoformat()

    client = _http_client or httpx.AsyncClient(timeout=NEWS_FETCH_TIMEOUT_S)
    own_client = _http_client is None
    try:
        resp = await client.get(
            FINNHUB_NEWS_URL,
            params={
                "symbol": symbol.upper(),
                "from": start_iso,
                "to": end_iso,
                "token": api_key,
            },
        )
        if resp.status_code != 200:
            result = {
                "has_news": None, "headline_count": None,
                "ok": False, "source": "finnhub",
                "reason": f"finnhub_http_{resp.status_code}",
                "from_cache": False,
            }
        else:
            headlines = resp.json() if resp.content else []
            if not isinstance(headlines, list):
                # Finnhub sometimes returns {"error": "..."} as a dict
                # on auth failure. Treat as a soft failure.
                result = {
                    "has_news": None, "headline_count": None,
                    "ok": False, "source": "finnhub",
                    "reason": "finnhub_unexpected_payload_shape",
                    "from_cache": False,
                }
            else:
                count = len(headlines)
                result = {
                    "has_news": count > 0,
                    "headline_count": count,
                    "ok": True, "source": "finnhub",
                    "reason": None,
                    "from_cache": False,
                }
    except Exception as exc:  # noqa: BLE001 — bounded network call
        result = {
            "has_news": None, "headline_count": None,
            "ok": False, "source": "finnhub",
            "reason": f"finnhub_fetch_failed:{type(exc).__name__}",
            "from_cache": False,
        }
    finally:
        if own_client:
            await client.aclose()

    # Cache (without the from_cache flag).
    _cache_set(symbol, {k: v for k, v in result.items() if k != "from_cache"})
    return result


# Imported lazily so test injection is straightforward.
def _timedelta(hours: int):
    from datetime import timedelta
    return timedelta(hours=hours)


# ──────────────────────── build_market_snapshot ────────────────────────


async def build_market_snapshot(
    symbol: str,
    tf: str = DEFAULT_TIMEFRAME,
    source: str = DEFAULT_SOURCE,
    *,
    include_news: bool = True,
    db=None,
) -> Dict[str, Any]:
    """One-shot enriched snapshot for `symbol`. Combines the RVOL
    aggregation + news lookup into the dict shape the labeler already
    consumes (`shared.doctrine.base_labels.build_doctrine_labels`).

    Doctrine: this is a CONVENIENCE wrapper. Each field reports its
    own `*_ok` so the caller (operator dashboard or brain feature
    builder) can distinguish "no data yet" from "0.0 right now"
    without parsing wrapper-error semantics.

    Returns:
        {
          symbol, tf, source, computed_at,
          last_bar_ts, current_v, avg_v,
          relative_volume,     # float | None
          relative_volume_ok,  # bool
          relative_volume_reason,  # str | None
          has_news,            # bool | None
          has_news_ok,         # bool
          has_news_reason,     # str | None
          basis_bars,
        }
    """
    rv = await compute_relative_volume(symbol, tf=tf, source=source, db=db)
    snapshot = {
        "symbol": symbol.upper(),
        "tf": tf,
        "source": source,
        "computed_at": _now_iso(),
        "last_bar_ts": rv["last_bar_ts"],
        "current_v": rv["current_v"],
        "avg_v": rv["avg_v"],
        "basis_bars": rv["basis_bars"],
        "relative_volume": rv["value"],
        "relative_volume_ok": rv["ok"],
        "relative_volume_reason": rv["reason"],
    }

    if include_news:
        news = await fetch_has_news(symbol)
        snapshot["has_news"] = news["has_news"]
        snapshot["has_news_ok"] = news["ok"]
        snapshot["has_news_reason"] = news["reason"]
        snapshot["has_news_source"] = news.get("source")
        snapshot["has_news_from_cache"] = news.get("from_cache", False)
    else:
        snapshot["has_news"] = None
        snapshot["has_news_ok"] = False
        snapshot["has_news_reason"] = "skipped_by_caller"

    return snapshot


# Operator escape hatch — clear the news cache. Useful for forcing a
# refresh after rotating the Finnhub key on prod.
def reset_news_cache() -> int:
    n = len(_NEWS_CACHE)
    _NEWS_CACHE.clear()
    return n

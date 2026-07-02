"""Trader configuration. All reads from env, no hardcoded values.

Required env (already in MC's production environment):
    MONGO_URL             — same Atlas/Mongo MC uses (shared truth)
    DB_NAME               — same DB name
    KRAKEN_API_KEY        — Kraken Pro live key
    KRAKEN_API_SECRET     — Kraken Pro live secret
    WEBULL_APP_KEY        — Webull OpenAPI app key
    WEBULL_APP_SECRET     — Webull OpenAPI app secret
    WEBULL_ACCOUNT_ID     — Webull cash account ID

Optional env (sensible defaults):
    TRADER_ENABLED                  — "true" to actually run; default "false" (safe)
    TRADER_INTERVAL_SEC             — cycle interval; default 60
    TRADER_PER_ORDER_USD_CAP        — hard cap per order; default $10
    TRADER_DAILY_USD_CAP            — daily cap across all orders; default $1000
    TRADER_CRYPTO_PAIR              — e.g. "XBTUSD"; default "XBTUSD"
    TRADER_EQUITY_TICKER            — e.g. "TSLA"; default "TSLA"
    TRADER_CONFIDENCE_THRESHOLD     — minimum brain confidence to fire; default 0.55
    TRADER_SQLITE_PATH              — local truth-tape file;
                                      default /app/trader/data/executions.sqlite
    TRADER_JSONL_DIR                — append-only receipt dir;
                                      default /app/trader/data
    TRADER_CACHE_REFRESH_SEC        — Mongo→cache refresh cadence; default 60
"""
from __future__ import annotations

import os


def env_str(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def env_float(key: str, default: float) -> float:
    raw = os.environ.get(key)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def env_int(key: str, default: int) -> int:
    raw = os.environ.get(key)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def env_bool(key: str, default: bool = False) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in ("true", "1", "yes", "on")


# Hard-coded immutable doctrine constants. These are NOT operator-tunable.
LANES = ("equity", "crypto")
ROLES = ("strategist", "governor", "executor", "auditor")
BRAINS = ("camino", "barracuda", "hellcat", "gto")


def trader_enabled() -> bool:
    return env_bool("TRADER_ENABLED", default=False)


def interval_sec() -> int:
    return env_int("TRADER_INTERVAL_SEC", 60)


def per_order_cap_usd() -> float:
    return env_float("TRADER_PER_ORDER_USD_CAP", 10.0)


def daily_cap_usd() -> float:
    return env_float("TRADER_DAILY_USD_CAP", 1000.0)


def crypto_pair() -> str:
    return env_str("TRADER_CRYPTO_PAIR", "XBTUSD")


def crypto_pairs() -> tuple[str, ...]:
    """Multi-pair crypto lane (2026-07-03 narrow-universe doctrine).

    Comma-separated Kraken pair codes, e.g. `TRADER_CRYPTO_PAIRS=BTCUSD,SOLUSD`.
    Backward-compatible: if unset OR the parsed list is empty (e.g. an
    env var containing only whitespace/commas), falls back to
    `(crypto_pair(),)`. This is deliberate — a typo in the env var
    must not silently disable the whole lane.

    Doctrine: depth over breadth. All four brains score the same 2 pairs
    so their dissent/accuracy tracks compare on equal data.
    """
    raw = env_str("TRADER_CRYPTO_PAIRS", "").strip()
    parsed = tuple(
        p.strip().upper() for p in raw.split(",") if p.strip()
    ) if raw else ()
    return parsed or (crypto_pair(),)


def equity_ticker() -> str:
    return env_str("TRADER_EQUITY_TICKER", "TSLA")


def equity_tickers() -> tuple[str, ...]:
    """Multi-ticker equity lane (2026-07-03 narrow-universe doctrine).

    Comma-separated symbols, e.g. `TRADER_EQUITY_TICKERS=NVDA,SPY`.
    Backward-compatible with the singular `TRADER_EQUITY_TICKER` env var.
    If parsed list is empty (typo, only commas, only whitespace), falls
    back to `(equity_ticker(),)` — a broken env must not silently
    disable the whole lane.

    Note: distinct from `equity_spread_tickers()` — that one drives the
    Yahoo/Webull spread poller (can be wider than what's traded). This
    one drives the brain/tick loop (what's actually traded). Keep them
    aligned or the poller wastes cycles on symbols the brains don't touch.
    """
    raw = env_str("TRADER_EQUITY_TICKERS", "").strip()
    parsed = tuple(
        t.strip().upper() for t in raw.split(",") if t.strip()
    ) if raw else ()
    return parsed or (equity_ticker(),)


def confidence_threshold() -> float:
    return env_float("TRADER_CONFIDENCE_THRESHOLD", 0.55)


def sqlite_path() -> str:
    return env_str("TRADER_SQLITE_PATH", "/app/trader/data/executions.sqlite")


def jsonl_dir() -> str:
    return env_str("TRADER_JSONL_DIR", "/app/trader/data")


def cache_refresh_sec() -> int:
    return env_int("TRADER_CACHE_REFRESH_SEC", 60)


# ── Per-brain tunables (2026-07-01) ──────────────────────────────
# Every constant that was previously baked into brains.py is now
# an env-var knob. Operators can tune sensitivity without a
# redeploy — set the env var, restart the pod, brains re-import.

# Camino — trend continuation
def camino_dist_min() -> float:
    return env_float("TRADER_CAMINO_DIST_MIN", 0.0005)


def camino_rsi_buy_min() -> float:
    return env_float("TRADER_CAMINO_RSI_BUY_MIN", 45.0)


def camino_rsi_buy_max() -> float:
    return env_float("TRADER_CAMINO_RSI_BUY_MAX", 75.0)


def camino_rsi_sell_min() -> float:
    return env_float("TRADER_CAMINO_RSI_SELL_MIN", 25.0)


def camino_rsi_sell_max() -> float:
    return env_float("TRADER_CAMINO_RSI_SELL_MAX", 55.0)


# Barracuda — mean reversion
def barracuda_rsi_buy_below() -> float:
    return env_float("TRADER_BARRACUDA_RSI_BUY_BELOW", 45.0)


def barracuda_rsi_sell_above() -> float:
    return env_float("TRADER_BARRACUDA_RSI_SELL_ABOVE", 55.0)


# Hellcat — breakout
def hellcat_hl_proximity() -> float:
    return env_float("TRADER_HELLCAT_HL_PROXIMITY", 0.01)


def hellcat_bb_upper() -> float:
    return env_float("TRADER_HELLCAT_BB_UPPER", 0.65)


def hellcat_bb_lower() -> float:
    return env_float("TRADER_HELLCAT_BB_LOWER", 0.35)


# GTO — momentum
def gto_macd_min_gap() -> float:
    return env_float("TRADER_GTO_MACD_MIN_GAP", 0.0)


# ── Kraken spread poller (2026-07-02) ─────────────────────────────
# Polls Kraken's public Ticker for one or more pairs, computes bid/
# ask spread in basis points, caches the latest tick in memory, and
# persists a rolling window to SQLite. Non-authoritative observability
# by default; can be promoted to a hard risk gate for the crypto lane
# by setting TRADER_SPREAD_GATE_ENABLED=true.
def spread_enabled() -> bool:
    return env_bool("TRADER_SPREAD_ENABLED", default=True)


def spread_pairs() -> tuple[str, ...]:
    """Comma-separated Kraken pair codes; default = the trader's
    active crypto pair. e.g. `TRADER_SPREAD_PAIRS=XBTUSD,ETHUSD`."""
    raw = env_str("TRADER_SPREAD_PAIRS", "").strip()
    if not raw:
        return (crypto_pair(),)
    return tuple(p.strip().upper() for p in raw.split(",") if p.strip())


def spread_poll_sec() -> int:
    return env_int("TRADER_SPREAD_POLL_SEC", 15)


def spread_max_bps() -> float:
    """Wide-spread ceiling in basis points. When gating is enabled,
    a spread above this HOLDs the crypto lane for that cycle."""
    return env_float("TRADER_SPREAD_MAX_BPS", 50.0)


def spread_gate_enabled() -> bool:
    """When true, `risk.check()` refuses crypto orders whose latest
    observed spread exceeds `TRADER_SPREAD_MAX_BPS`. Default off —
    the poller is observability-first."""
    return env_bool("TRADER_SPREAD_GATE_ENABLED", default=False)


def spread_stale_sec() -> int:
    """Age (seconds) beyond which a cached spread reading is treated
    as unreliable. The gate ignores stale readings (fails open) — a
    dead poller must not deadlock trading."""
    return env_int("TRADER_SPREAD_STALE_SEC", 120)


# Equity spread (Yahoo /v7/finance/quote → bid/ask). Same doctrine
# as the crypto poller: observability-first, optional gate. Webull's
# private OpenAPI quote endpoint isn't reachable from preview pods,
# so Yahoo is the free source-of-truth that matches what the equity
# feeds module already uses. The `source` column distinguishes
# `kraken` vs `yahoo` rows in `spread_ticks`.
def equity_spread_enabled() -> bool:
    # 2026-07-02 default ON: switched from Webull's retired public
    # gateway to the authenticated OpenAPI snapshot endpoint. When
    # WEBULL_APP_KEY/SECRET are unset the fetcher short-circuits
    # gracefully — nothing to poll if creds are missing.
    return env_bool("TRADER_EQUITY_SPREAD_ENABLED", default=True)


def equity_spread_tickers() -> tuple[str, ...]:
    raw = env_str("TRADER_EQUITY_SPREAD_TICKERS", "").strip()
    if not raw:
        return (equity_ticker(),)
    return tuple(t.strip().upper() for t in raw.split(",") if t.strip())


def equity_spread_poll_sec() -> int:
    return env_int("TRADER_EQUITY_SPREAD_POLL_SEC", 20)


def equity_spread_max_bps() -> float:
    """Equity spreads are typically wider than crypto majors — the
    default 25bps allows normal TSLA/AAPL retail-quote conditions
    while blocking obvious mid-halt / after-hours widening."""
    return env_float("TRADER_EQUITY_SPREAD_MAX_BPS", 25.0)


def equity_spread_gate_enabled() -> bool:
    return env_bool("TRADER_EQUITY_SPREAD_GATE_ENABLED", default=False)


# Webull MQTT streaming (fluid-machine upgrade over HTTP polling).
# Opens a persistent connection to `data-api.webull.com`, receives
# QUOTE messages tick-by-tick, decodes protobuf, updates the SAME
# in-memory cache the HTTP poller writes into. The HTTP poller stays
# on as a warm safety net — whichever source is fresher wins.
# 2026-07-02: shipped OFF by default. The SDK's gRPC token-exchange
# call couples `Host` metadata to the MQTT host, which produces
# `UNAVAILABLE: tcp handshaker shutdown` on `api.webull.com`-tier
# plans. Awaiting operator confirmation that the OpenAPI plan
# includes the streaming entitlement (it's separate from L1 snapshot).
def equity_stream_enabled() -> bool:
    return env_bool("TRADER_EQUITY_STREAM_ENABLED", default=False)


def equity_stream_symbols() -> tuple[str, ...]:
    """Symbols to subscribe on the MQTT stream. Defaults to the same
    list the HTTP poller uses so they can coexist without divergence."""
    raw = env_str("TRADER_EQUITY_STREAM_SYMBOLS", "").strip()
    if not raw:
        return equity_spread_tickers()
    return tuple(t.strip().upper() for t in raw.split(",") if t.strip())


def equity_stream_region() -> str:
    """Webull region code passed to the streaming client. Options:
    us / hk / cn. Default `us` matches the trade adapter's default."""
    return env_str("TRADER_EQUITY_STREAM_REGION", "us")


def equity_stream_endpoint() -> str:
    """MQTT host override. Default `data-api.webull.com` — the docs
    endpoint for the L1 quote stream. See also
    `TRADER_EQUITY_STREAM_HTTP_HOST` for the paired gRPC host."""
    return env_str("TRADER_EQUITY_STREAM_ENDPOINT", "data-api.webull.com")


def equity_stream_http_host() -> str:
    """gRPC (token exchange) host. MUST be separate from the MQTT
    host — coupling them causes `UNAVAILABLE: tcp handshaker shutdown`
    because the MQTT gateway doesn't speak gRPC on 443."""
    return env_str("TRADER_EQUITY_STREAM_HTTP_HOST", "api.webull.com")


def equity_stream_session_id() -> str:
    """Stable client session id — Webull boots the earlier connection
    if we reuse a live one, so a per-process/per-role tag is best.
    Max 5 concurrent sessions per App Key."""
    return env_str("TRADER_EQUITY_STREAM_SESSION_ID", "mc_paradox_equity_1")


def equity_stream_sub_types() -> tuple[str, ...]:
    """Which of QUOTE / SNAPSHOT / TICK to subscribe. Default QUOTE
    only (best bid/ask) — SNAPSHOT + TICK add trade prints and OHLC
    but at the cost of throughput. Comma-separated string in env."""
    raw = env_str("TRADER_EQUITY_STREAM_SUB_TYPES", "QUOTE").strip()
    return tuple(s.strip().upper() for s in raw.split(",") if s.strip())

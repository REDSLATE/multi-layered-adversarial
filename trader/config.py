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


def equity_ticker() -> str:
    return env_str("TRADER_EQUITY_TICKER", "TSLA")


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

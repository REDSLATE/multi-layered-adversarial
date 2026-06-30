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

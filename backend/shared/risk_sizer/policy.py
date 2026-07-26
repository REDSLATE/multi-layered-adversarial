"""Dynamic risk-sizer policy (2026-07-25 operator spec).

Doctrine: brains produce opinions, not dollar amounts. Position size =
account-risk budget ÷ canonical stop distance, capped by allocation,
spendable cash, and total open portfolio risk. Governor is
reduce-only. Gain Goal may throttle or block, never increase risk.
"""
from __future__ import annotations

import os
from typing import Any

FLAG_ID = "risk_sizer_policy"

DEFAULTS: dict[str, Any] = {
    "crypto": {
        "risk_fraction": 0.005,            # 0.50% account risk per trade
        "max_position_fraction": 0.25,     # 25% allocation cap
        "reserve_fraction": 0.20,          # uncommitted wallet share
        "fee_buffer_fraction": 0.006,      # entry+exit cost estimate
        "slippage_buffer_fraction": 0.001,
        "minimum_order_notional": 5.0,
        "max_open_risk_fraction": 0.02,    # 2% total open risk
        "min_stop_fraction": 0.01,         # brain stop bounds
        "max_stop_fraction": 0.05,
    },
    "equity": {
        "risk_fraction": 0.005,
        "max_position_fraction": 0.25,
        "reserve_fraction": 0.20,
        "fee_buffer_fraction": 0.001,      # Webull commission-free + spread
        "slippage_buffer_fraction": 0.0005,
        "minimum_order_notional": 5.0,     # Webull fractional minimum
        "max_open_risk_fraction": 0.02,
        "min_stop_fraction": 0.01,
        "max_stop_fraction": 0.05,
    },
    "options": {
        "risk_fraction": 0.005,
        "max_position_fraction": 0.10,
        "reserve_fraction": 0.20,
        "fee_buffer_fraction": 0.01,       # per-contract fees vs premium
        "slippage_buffer_fraction": 0.0,   # spread gate covers it
        "minimum_order_notional": 1.0,
        "max_open_risk_fraction": 0.02,
        "premium_stop_fraction": 1.0,      # long options: full premium at risk
        "max_premium_fraction": 0.05,      # max total premium at risk vs equity
        "contract_multiplier": 100,
        "min_dte": 7,
        "max_dte": 60,
        "min_open_interest": 100,
        "max_spread_fraction": 0.10,       # (ask-bid)/mid
        "min_abs_delta": 0.25,
        "max_abs_delta": 0.85,
        "target_abs_delta": 0.50,
        "max_theta_fraction_per_day": 0.03,
    },
    "selection": {
        "min_confidence": 0.55,
        "min_score": 0.50,
        "min_expectancy_sample": 20,       # soft gate before this
    },
    "balance": {
        "live_timeout_s": 3.0,
        "cache_max_age_s": 60.0,
    },
}

_ENV_FLAGS = {
    "crypto": "CRYPTO_DYNAMIC_RISK_SIZER_ENABLED",
    "equity": "EQUITY_DYNAMIC_RISK_SIZER_ENABLED",
    "options": "OPTIONS_ENABLED",
}
_ENV_DEFAULTS = {"crypto": "true", "equity": "false", "options": "false"}


def lane_enabled(lane: str) -> bool:
    raw = os.environ.get(_ENV_FLAGS.get(lane, ""), _ENV_DEFAULTS.get(lane, "false"))
    return raw.strip().lower() in ("1", "true", "yes", "on")


async def get_sizer_policy() -> dict:
    from db import db  # noqa: WPS433
    import time as _t
    now = _t.monotonic()
    if _policy_cache["value"] is not None and now - _policy_cache["at"] < _POLICY_TTL_S:
        return _policy_cache["value"]
    try:
        stored = await db["runtime_flags"].find_one({"_id": FLAG_ID}, {"_id": 0}) or {}
    except Exception:  # noqa: BLE001
        stored = {}
    out: dict = {}
    for section, defaults in DEFAULTS.items():
        merged = dict(defaults)
        for k, v in (stored.get(section) or {}).items():
            if k in merged and v is not None:
                merged[k] = float(v) if isinstance(merged[k], float) else v
        out[section] = merged
    out["enabled"] = {lane: lane_enabled(lane)
                      for lane in ("crypto", "equity", "options")}
    _policy_cache.update(at=now, value=out)
    return out


_POLICY_TTL_S = 15.0
_policy_cache: dict = {"at": 0.0, "value": None}


def invalidate_policy_cache() -> None:
    _policy_cache.update(at=0.0, value=None)

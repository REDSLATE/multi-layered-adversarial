"""Dynamic BUY eligibility (2026-08-03 operator directive).

Replaces the static crypto BUY allowlist with liquidity RULES:
"the list was standing in for rules — replace, never just delete."

Modes (knob `runtime_flags._id=buy_eligibility`):
  static  — defer entirely to the legacy allowlist (rollback path)
  dynamic — rules only
  hybrid  — operator pins (legacy allowlist symbols) always allowed,
            denylist always blocked, everything else judged by rules
            (DEFAULT, operator-approved "Balanced" thresholds)

Rules (all knobs): min 24h dollar volume $1M · spread ≤ 50bps ·
per-trade notional cap $5 (2026-08-03 operator: "keep it low, like $5
per trade") applied to EVERY crypto BUY — pins, static mode, and
rule-admitted symbols alike; rule-admitted symbols additionally capped
at ≤ 0.5% of 24h volume. Adjustable knob, not hardcoded.
SELLs / exits are NEVER gated here (BUY-path only, enforced by caller).
Fail-closed on missing quotes in dynamic paths; fail-open only on
Mongo read errors (a DB hiccup must not decide trades — same doctrine
as the legacy allowlist).
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

logger = logging.getLogger("risedual.buy_eligibility")

FLAG_ID = "buy_eligibility"
DEFAULTS: dict[str, Any] = {
    "mode": "hybrid",
    "min_dollar_vol_24h": 1_000_000.0,
    "max_spread_bps": 50.0,
    "max_notional_usd": 5.0,
    "max_pct_of_24h_vol": 0.5,
    "hard_reject_spread_bps": 300.0,
    "denylist": [],
}
_CACHE_TTL_S = 30.0
_cfg_cache: dict = {"at": 0.0, "doc": None}
_sym_cache: dict[str, tuple[float, dict]] = {}


def reset_for_tests() -> None:
    _cfg_cache.update(at=0.0, doc=None)
    _sym_cache.clear()


async def get_eligibility_config() -> dict:
    now = time.monotonic()
    if _cfg_cache["doc"] is not None and now - _cfg_cache["at"] < _CACHE_TTL_S:
        return _cfg_cache["doc"]
    from db import db  # noqa: WPS433
    doc = await db["runtime_flags"].find_one(
        {"_id": FLAG_ID}, {"_id": 0}, max_time_ms=3000) or {}
    merged = {**DEFAULTS, **doc}
    _cfg_cache.update(at=now, doc=merged)
    return merged


async def _dollar_volume_24h(symbol: str) -> Optional[float]:
    """live_universe row (volume×price, refresher-fresh) → bar-sum fallback."""
    from db import db  # noqa: WPS433
    row = await db["live_universe"].find_one(
        {"lane": "crypto", "symbols.canonical_symbol": symbol},
        {"symbols.$": 1}, max_time_ms=3000)
    if row and row.get("symbols"):
        s = row["symbols"][0]
        vol, price = float(s.get("volume") or 0), float(s.get("price") or 0)
        if vol > 0 and price > 0:
            return vol * price
    from datetime import datetime, timedelta, timezone  # noqa: WPS433
    cut = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    total, n = 0.0, 0
    async for b in db["shared_ohlcv_bars"].find(
            {"symbol": symbol, "ts": {"$gte": cut}},
            {"_id": 0, "v": 1, "c": 1}).max_time_ms(5000):
        total += float(b.get("v") or 0) * float(b.get("c") or 0)
        n += 1
    return total if n >= 6 else None


async def _spread_bps(symbol: str) -> Optional[float]:
    try:
        from shared.market_data.crypto_snapshot_enrichment import (  # noqa: WPS433
            enrich_crypto_snapshot,
        )
        quote, _diag = await enrich_crypto_snapshot({}, symbol=symbol)
        bid, ask = float(quote.get("bid") or 0), float(quote.get("ask") or 0)
        if bid > 0 and ask >= bid:
            mid = (bid + ask) / 2.0
            return (ask - bid) / mid * 10_000.0
    except Exception as exc:  # noqa: BLE001
        logger.warning("buy_eligibility: quote fetch failed %s: %s",
                       symbol, exc)
    return None


async def evaluate_buy_eligibility(symbol: str) -> tuple[bool, dict]:
    """(allowed, receipt). receipt.notional_cap_usd is set on EVERY
    allowed BUY (2026-08-03: $5/trade default, pins included)."""
    from shared.risk_sizer.buy_allowlist import (  # noqa: WPS433
        buy_allowed, get_allowlist, normalize_crypto_symbol,
    )
    sym = normalize_crypto_symbol(symbol)
    cfg = await get_eligibility_config()
    mode = str(cfg.get("mode") or "hybrid").lower()
    cap_all = round(float(cfg.get("max_notional_usd") or
                          DEFAULTS["max_notional_usd"]), 2)
    base: dict = {"mode": mode, "symbol": sym, "notional_cap_usd": None}

    if mode == "static":
        allowed, al = await buy_allowed(sym)
        return allowed, {**base,
                         "notional_cap_usd": cap_all if allowed else None,
                         "reason":
                         "allowlist_static" if allowed else "not_in_buy_allowlist",
                         "allowlist_size": len(al.get("symbols") or [])}

    deny = {normalize_crypto_symbol(d) for d in (cfg.get("denylist") or [])}
    if sym in deny:
        return False, {**base, "reason": "denylisted"}

    if mode == "hybrid":
        try:
            al = await get_allowlist()
            if sym in set(al.get("symbols") or []):
                return True, {**base, "reason": "operator_pin",
                              "notional_cap_usd": cap_all}
        except Exception:  # noqa: BLE001
            pass  # pins unreadable → fall through to rules (fail-open on Mongo)

    now = time.monotonic()
    cached = _sym_cache.get(sym)
    if cached and now - cached[0] < _CACHE_TTL_S:
        r = cached[1]
        return bool(r.get("_allowed")), {**base, **{k: v for k, v in r.items()
                                                    if k != "_allowed"}}

    dvol = await _dollar_volume_24h(sym)
    if dvol is None:
        rec = {"reason": "no_volume_data", "_allowed": False}
        _sym_cache[sym] = (now, rec)
        return False, {**base, "reason": "no_volume_data"}
    if dvol < float(cfg["min_dollar_vol_24h"]):
        rec = {"reason": "below_volume_floor", "dollar_vol_24h": round(dvol),
               "_allowed": False}
        _sym_cache[sym] = (now, rec)
        return False, {**base, **{k: v for k, v in rec.items() if k != "_allowed"}}

    spread = await _spread_bps(sym)
    if spread is None:
        return False, {**base, "reason": "no_quote",
                       "dollar_vol_24h": round(dvol)}  # never cache quote gaps
    # 2026 MC directive "Capture the Move": a wide spread is execution
    # FRICTION, not a rejection — the signal is ADMITTED with a ladder
    # flag so the router hunts a fill instead of returning to HOLD.
    # Only a truly extreme/broken book (hard_reject_spread_bps) still
    # hard-rejects.
    if spread > float(cfg.get("hard_reject_spread_bps")
                      or DEFAULTS["hard_reject_spread_bps"]):
        rec = {"reason": "spread_extreme", "spread_bps": round(spread, 1),
               "dollar_vol_24h": round(dvol), "_allowed": False}
        _sym_cache[sym] = (now, rec)
        return False, {**base, **{k: v for k, v in rec.items() if k != "_allowed"}}

    cap = min(cap_all,
              float(cfg["max_pct_of_24h_vol"]) / 100.0 * dvol)
    if spread > float(cfg["max_spread_bps"]):
        rec = {"reason": "wide_spread_ladder",
               "execution_friction": "spread_too_wide",
               "spread_bps": round(spread, 1),
               "dollar_vol_24h": round(dvol),
               "notional_cap_usd": round(cap, 2), "_allowed": True}
        _sym_cache[sym] = (now, rec)
        return True, {**base, **{k: v for k, v in rec.items() if k != "_allowed"}}

    rec = {"reason": "rules_admitted", "dollar_vol_24h": round(dvol),
           "spread_bps": round(spread, 1),
           "notional_cap_usd": round(cap, 2), "_allowed": True}
    _sym_cache[sym] = (now, rec)
    return True, {**base, **{k: v for k, v in rec.items() if k != "_allowed"}}

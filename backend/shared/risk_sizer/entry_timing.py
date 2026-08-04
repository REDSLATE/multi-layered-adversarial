"""Entry Timing Gate — the one missing hard block (2026-08-01).

Operator doctrine: "Signal says direction. Entry Timing Gate decides
whether the price is still safe to buy. Once RISEDUAL misses the
original entry, it must stop chasing and wait for a new setup."

What already existed (reused here, NOT rebuilt):
  * the emit-time market snapshot is frozen on the intent doc at
    ingest (`intent["snapshot"]`, persisted once, never re-anchored)
    — that IS the confirmation price;
  * `shared.doctrine.universe_classifier.classify_universe` —
    CRYPTO | SMALL_CAP_MOMENTUM | LARGE_CAP | ETF | UNKNOWN;
  * `shared.snapshot_enrich.parabolic_phase.classify_parabolic_phase`
    — velocity_5m, vwap_distance_pct, rvol_acceleration,
    peak_drop_pct + phase label. Until now the phase only nudged the
    ADVISORY score — it never hard-blocked a late entry. That was
    the leak: the system KNEW a move was parabolic and bought anyway.

This module compares the FRESH gate-time price against the FROZEN
confirmation price and blocks the chase, with per-universe-class
thresholds (runtime_flags._id=entry_timing, live-tunable).

Placement: auto_router stage between `_gate_risk` and
`_route_and_submit` — after Seat/doctrine + sizing approval,
immediately before the broker call (the broker adapter re-fetches
its own quote milliseconds later). BUY-only: exits are NEVER gated.

Staleness = price extension since confirmation, NOT wall-clock age.
(The router ticks ~30s; a 20s age wall would reject nearly every
intent. `intent_age_seconds` rides on the receipt for observability.)
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("risedual.entry_timing")

FLAG_ID = "entry_timing"

# Per-universe-class thresholds. A parabolic mover gets STRICTER.
DEFAULT_PROFILES: dict[str, dict] = {
    "large_cap": {
        "max_extension_from_confirmation_pct": 2.0,
        "max_vwap_distance_pct": 3.0,
        "max_velocity_5m_pct": 5.0,
        "blocked_phases": ["topping", "fade"],
    },
    "etf": {
        "max_extension_from_confirmation_pct": 1.5,
        "max_vwap_distance_pct": 2.0,
        "max_velocity_5m_pct": 3.0,
        "blocked_phases": ["topping", "fade"],
    },
    "small_cap_momentum": {
        "max_extension_from_confirmation_pct": 8.0,
        "max_vwap_distance_pct": 12.0,
        "max_velocity_5m_pct": 25.0,
        "blocked_phases": ["parabolic", "topping", "fade"],
    },
    "crypto": {
        "max_extension_from_confirmation_pct": 4.0,
        "max_vwap_distance_pct": 6.0,
        "max_velocity_5m_pct": 10.0,
        "blocked_phases": ["parabolic", "topping", "fade"],
    },
}
# UNKNOWN universe → strictest small-cap treatment (fail-strict, loud)
_CLASS_TO_PROFILE = {
    "CRYPTO": "crypto",
    "SMALL_CAP_MOMENTUM": "small_cap_momentum",
    "LARGE_CAP": "large_cap",
    "ETF": "etf",
    "UNKNOWN": "small_cap_momentum",
}

_CACHE_TTL_S = 30.0
_cache: dict = {"at": 0.0, "doc": None}


def invalidate_cache() -> None:
    _cache.update(at=0.0, doc=None)


async def get_config() -> dict:
    now = time.monotonic()
    if _cache["doc"] is not None and now - _cache["at"] < _CACHE_TTL_S:
        return _cache["doc"]
    from db import db  # noqa: WPS433
    doc = await db["runtime_flags"].find_one(
        {"_id": FLAG_ID}, {"_id": 0}, max_time_ms=3000,
    ) or {}
    profiles = {
        name: {**defaults, **((doc.get("profiles") or {}).get(name) or {})}
        for name, defaults in DEFAULT_PROFILES.items()
    }
    merged = {"enabled": bool(doc.get("enabled", True)), "profiles": profiles}
    _cache.update(at=now, doc=merged)
    return merged


def _f(v) -> Optional[float]:
    try:
        f = float(v)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


async def _load_bars(symbol: str, limit: int = 60) -> list[dict]:
    from db import db  # noqa: WPS433
    from namespaces import SHARED_OHLCV_BARS  # noqa: WPS433
    for tf in ("1m", "5m"):
        rows = await db[SHARED_OHLCV_BARS].find(
            {"symbol": symbol, "tf": tf},
            {"_id": 0, "ts": 1, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1},
        ).sort("ts", -1).max_time_ms(4000).to_list(limit)
        if len(rows) >= 10:
            return list(reversed(rows))
    return []


def evaluate(intent: dict, bars: list[dict], profiles: dict) -> dict:
    """Pure gate verdict — {allowed, reason, decision, receipt}.
    Fail-CLOSED for entries: no usable timing data → no BUY."""
    from shared.doctrine.universe_classifier import classify_universe  # noqa: WPS433
    from shared.snapshot_enrich.parabolic_phase import (  # noqa: WPS433
        classify_parabolic_phase,
    )

    snapshot = intent.get("snapshot") or {}
    confirmation_price = _f(snapshot.get("price")) or _f(intent.get("price"))
    confirmation_source = "snapshot_price" if confirmation_price else None
    if not confirmation_price:
        # Organic intents carry no `price` key — the ingest-time
        # enrichment freezes bid/ask instead. The mid IS the frozen
        # emit-time price (2026-08-01 validation finding).
        bid, ask = _f(snapshot.get("bid")), _f(snapshot.get("ask"))
        if bid and ask and ask >= bid:
            confirmation_price = (bid + ask) / 2.0
            confirmation_source = "snapshot_bid_ask_mid"
    if not confirmation_price and bars:
        # Last resort: close of the bar at/just before confirmation
        # time (equity snapshots may lack quotes entirely).
        ingest = str(intent.get("ingest_ts") or "")
        prior = [b for b in bars if str(b.get("ts") or "") <= ingest]
        if prior:
            confirmation_price = _f(prior[-1].get("c"))
            confirmation_source = "bar_at_confirmation"
    fresh_price = _f(bars[-1].get("c")) if bars else None

    uc = classify_universe({**snapshot, "lane": intent.get("lane"),
                            "symbol": intent.get("symbol")})
    profile_name = _CLASS_TO_PROFILE.get(
        getattr(uc, "name", str(uc)), "small_cap_momentum")
    prof = profiles[profile_name]

    receipt: dict[str, Any] = {
        "universe_class": getattr(uc, "name", str(uc)),
        "profile": profile_name,
        "confirmation_price": confirmation_price,
        "confirmation_source": confirmation_source,
        "confirmation_time": intent.get("ingest_ts"),
        "current_price": fresh_price,
    }
    try:
        ingest = intent.get("ingest_ts")
        if ingest:
            receipt["intent_age_seconds"] = round(
                (datetime.now(timezone.utc)
                 - datetime.fromisoformat(str(ingest))).total_seconds(), 1)
    except (TypeError, ValueError):
        pass

    def _block(reason: str, decision: str = "MISSED_ENTRY") -> dict:
        cp, fp = confirmation_price, fresh_price
        if cp and fp:
            ext = (fp / cp - 1.0) * 100.0
            receipt["message"] = (
                f"MISSED ENTRY — setup confirmed at ${cp:g}. Current "
                f"price ${fp:g} is {ext:+.1f}% above confirmation "
                f"({reason}). Waiting for a new base or pullback."
            )
        else:
            receipt["message"] = f"{reason} — refusing blind entry."
        return {"allowed": False, "reason": reason,
                "decision": decision, "receipt": receipt}

    if not confirmation_price or not fresh_price:
        return _block("NO_TIMING_DATA", decision="REJECT")

    extension = (fresh_price / confirmation_price - 1.0) * 100.0
    receipt["extension_from_confirmation_pct"] = round(extension, 3)

    phase, meas = classify_parabolic_phase(bars, current_price=fresh_price)
    receipt["parabolic_phase"] = phase
    receipt.update({k: meas.get(k) for k in (
        "velocity_5m", "vwap_distance_pct", "rvol_acceleration",
        "peak_drop_pct")})

    if extension > prof["max_extension_from_confirmation_pct"]:
        return _block("MISSED_ENTRY_CHASE_RISK")
    if phase in prof["blocked_phases"]:
        return _block(
            "PARABOLIC_CHASE_RISK" if phase == "parabolic"
            else "LATE_MOMENTUM_ENTRY")
    if meas.get("vwap_distance_pct", 0.0) > prof["max_vwap_distance_pct"]:
        return _block("TOO_FAR_ABOVE_VWAP")
    if meas.get("velocity_5m", 0.0) > prof["max_velocity_5m_pct"]:
        return _block("MOVE_ALREADY_EXTENDED")

    receipt["message"] = (
        f"ENTRY OK — {extension:+.1f}% vs confirmation, phase={phase}")
    return {"allowed": True, "reason": "entry_window_open",
            "decision": "BUY", "receipt": receipt}


async def check_buy_entry(intent: dict) -> dict:
    """Async wrapper: config + fresh bars + pure evaluate.
    Only ever called for BUY intents — exits are never gated."""
    cfg = await get_config()
    if not cfg["enabled"]:
        return {"allowed": True, "reason": "gate_disabled",
                "decision": "BUY", "receipt": {}}
    bars = await _load_bars((intent.get("symbol") or "").upper())
    # Tape Quality Gate (2026-08-04): stale/gappy bars distort the
    # extension math — fail CLOSED, same doctrine as NO_TIMING_DATA.
    # BAD_TAPE_QUALITY is NOT in REARMABLE_REASONS (a data fault must
    # not open a re-arm window). Fail-open on gate errors only.
    tq = None
    if bars:
        try:
            from shared.market_data.tape_quality import assess_with_config  # noqa: WPS433
            tq = await assess_with_config(bars)
        except Exception:  # noqa: BLE001
            tq = None
        if tq is not None and not tq["ok"]:
            return {"allowed": False, "reason": "BAD_TAPE_QUALITY",
                    "decision": "REJECT",
                    "receipt": {"tape_quality": tq,
                                "message": (f"tape {tq['reason']} — "
                                            "refusing blind entry.")}}
    verdict = evaluate(intent, bars, cfg["profiles"])
    if tq is not None:
        verdict.setdefault("receipt", {})["tape_quality"] = {
            "reason": tq["reason"], **(tq.get("fingerprint") or {})}
    return verdict

"""Entry Timing doctrine — "stop buying after the momentum is done".

Operator directive (2026-07-31):
    The parabolic/late-entry signal used to be an ADVISORY size-nudge
    inside `doctrine/base_labels.py` (score −0.10..−0.30). Advisory
    nudges still ship the order — just smaller — so the system kept
    buying tops in miniature. Phase enforcement now lives in a HARD
    gate between Risk approval and broker submission
    (`auto_router_stages._gate_entry_timing`).

This module owns the PURE part of that gate:

    * per-universe-class thresholds, env-configurable (a small-cap
      runner and SPY do not share a "too extended" definition), and
    * `evaluate_entry_timing(...)` — the fail-open decision function.

No I/O, no Mongo, no broker. The stage function does the fetching and
the writing; this module only decides.

Fail-open contract: when `parabolic_phase` is missing/unknown (the
classifier needs ≥10 M1 bars), or no fresh price could be resolved at
submit time, the evaluation returns a non-blocking verdict. Missing
data NEVER hard-blocks a trade.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, Optional

from shared.doctrine.universe_classifier import UniverseClass, classify_universe


# ── Reason codes (stamped as `broker_reason`; bucket `entry_timing`) ──
ENTRY_WINDOW_EXPIRED = "ENTRY_WINDOW_EXPIRED"
STALE_BUY_INTENT = "STALE_BUY_INTENT"
MOVE_ALREADY_EXTENDED = "MOVE_ALREADY_EXTENDED"
MISSED_ENTRY_CHASE_RISK = "MISSED_ENTRY_CHASE_RISK"
TOO_FAR_ABOVE_VWAP = "TOO_FAR_ABOVE_VWAP"
LATE_MOMENTUM_ENTRY = "LATE_MOMENTUM_ENTRY"
PARABOLIC_CHASE_RISK = "PARABOLIC_CHASE_RISK"

REASON_CODES = frozenset({
    ENTRY_WINDOW_EXPIRED, STALE_BUY_INTENT, MOVE_ALREADY_EXTENDED,
    MISSED_ENTRY_CHASE_RISK, TOO_FAR_ABOVE_VWAP, LATE_MOMENTUM_ENTRY,
    PARABOLIC_CHASE_RISK,
})

BLOCKED_BUCKET = "entry_timing"

# Phase → reason code. `accumulation` / `neutral` are entry-eligible.
PHASE_REASONS = {
    "parabolic": PARABOLIC_CHASE_RISK,
    "topping": LATE_MOMENTUM_ENTRY,
    "fade": ENTRY_WINDOW_EXPIRED,
}

# Phases that carry no usable classification (bars too thin).
_UNKNOWN_PHASES = {"", "unknown", "none"}


@dataclass(frozen=True)
class EntryTimingThresholds:
    """Per-universe-class ceilings. Every field is env-overridable."""
    universe_class: str
    max_velocity_5m_pct: float
    max_vwap_distance_pct: float
    max_extension_pct: float


# Defaults per universe class. Small-cap momentum names run hardest
# and reverse hardest, so they get the STRICTEST ceilings; large caps
# get room; ETFs move in far smaller percentages so their ceilings are
# scaled to that reality; crypto has its own (24/7, wider) profile.
_DEFAULTS: Dict[str, tuple[float, float, float]] = {
    # universe_class:      (velocity_5m, vwap_distance, extension)
    UniverseClass.SMALL_CAP_MOMENTUM.value: (4.0, 5.0, 1.0),
    UniverseClass.LARGE_CAP.value:          (8.0, 10.0, 2.0),
    UniverseClass.ETF.value:                (3.0, 4.0, 0.75),
    UniverseClass.CRYPTO.value:             (6.0, 12.0, 2.5),
    # No classification hint — treat like a large cap rather than
    # applying the small-cap ceilings to something we can't identify.
    UniverseClass.UNKNOWN.value:            (8.0, 10.0, 2.0),
}


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


def _env_bool(key: str, default: bool) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def gate_enabled() -> bool:
    """Master flag. DEFAULT OFF (shadow posture): the stage still
    computes and stamps what it WOULD have done so the operator can
    measure hit-rate before arming it."""
    return _env_bool("ENTRY_TIMING_GATE_ENABLED", False)


def stale_age_sec() -> float:
    """Age (seconds since emit) above which an extension breach is
    reported as STALE_BUY_INTENT rather than MOVE_ALREADY_EXTENDED.

    NOT a standalone age gate — the router ticks every ~30s and the
    reconciler skips submits younger than 30s, so a wall-clock age
    veto would reject nearly everything. Price extension since emit is
    the staleness signal; age only picks the reason code.
    """
    return _env_float("ENTRY_TIMING_STALE_AGE_SEC", 120.0)


def thresholds_for(universe_class: Any) -> EntryTimingThresholds:
    """Env-configurable ceilings for one universe class.

    Env keys (per class, e.g. SMALL_CAP_MOMENTUM):
        ENTRY_TIMING_MAX_VELOCITY_5M_PCT_SMALL_CAP_MOMENTUM
        ENTRY_TIMING_MAX_VWAP_DIST_PCT_SMALL_CAP_MOMENTUM
        ENTRY_TIMING_MAX_EXTENSION_PCT_SMALL_CAP_MOMENTUM
    """
    key = getattr(universe_class, "value", str(universe_class or "")).upper()
    if key not in _DEFAULTS:
        key = UniverseClass.UNKNOWN.value
    d_vel, d_vwap, d_ext = _DEFAULTS[key]
    return EntryTimingThresholds(
        universe_class=key,
        max_velocity_5m_pct=_env_float(
            f"ENTRY_TIMING_MAX_VELOCITY_5M_PCT_{key}", d_vel,
        ),
        max_vwap_distance_pct=_env_float(
            f"ENTRY_TIMING_MAX_VWAP_DIST_PCT_{key}", d_vwap,
        ),
        max_extension_pct=_env_float(
            f"ENTRY_TIMING_MAX_EXTENSION_PCT_{key}", d_ext,
        ),
    )


def universe_class_for(snapshot: Dict[str, Any], lane: str, symbol: str):
    """Classify with the lane/symbol the router knows about, which the
    persisted snapshot doesn't always carry."""
    merged = dict(snapshot or {})
    merged.setdefault("lane", lane)
    merged.setdefault("symbol", symbol)
    return classify_universe(merged)


def _f(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def evaluate_entry_timing(
    *,
    snapshot: Dict[str, Any],
    thresholds: EntryTimingThresholds,
    fresh_price: Optional[float],
    emit_price: Optional[float],
    intent_age_sec: Optional[float] = None,
) -> Dict[str, Any]:
    """Decide whether this BUY is arriving after the move is done.

    Returns a verdict dict:
        {
          "block": bool,
          "reason": <reason code> | None,
          "detail": str,
          "fail_open": <why> | None,
          "universe_class": str,
          "parabolic_phase": str | None,
          "extension_pct": float | None,
          "market_price": float | None,
        }
    """
    snap = snapshot if isinstance(snapshot, dict) else {}
    phase = str(snap.get("parabolic_phase") or "").strip().lower()
    velocity_5m = _f(snap.get("velocity_5m"))
    vwap_distance = _f(snap.get("vwap_distance_pct"))
    emit = _f(emit_price)
    fresh = _f(fresh_price)

    out: Dict[str, Any] = {
        "block": False,
        "reason": None,
        "detail": "",
        "fail_open": None,
        "universe_class": thresholds.universe_class,
        "parabolic_phase": phase or None,
        "velocity_5m": velocity_5m,
        "vwap_distance_pct": vwap_distance,
        "extension_pct": None,
        "market_price": fresh,
        "emit_price": emit,
        "thresholds": {
            "max_velocity_5m_pct": thresholds.max_velocity_5m_pct,
            "max_vwap_distance_pct": thresholds.max_vwap_distance_pct,
            "max_extension_pct": thresholds.max_extension_pct,
        },
    }

    # ── Fail-open guards. Missing data never hard-blocks. ──────────
    if phase in _UNKNOWN_PHASES:
        out["fail_open"] = "missing_parabolic_phase"
        return out
    if fresh is None or fresh <= 0:
        out["fail_open"] = "missing_fresh_price"
        return out

    if emit is not None and emit > 0:
        extension_pct = (fresh - emit) / emit * 100.0
        out["extension_pct"] = round(extension_pct, 4)
    else:
        extension_pct = None

    # ── 1. Phase veto (hard — no size nudge) ───────────────────────
    phase_reason = PHASE_REASONS.get(phase)
    if phase_reason:
        out["block"] = True
        out["reason"] = phase_reason
        out["detail"] = (
            f"parabolic_phase={phase} "
            f"velocity_5m={velocity_5m if velocity_5m is not None else 'n/a'} "
            f"vwap_distance_pct="
            f"{vwap_distance if vwap_distance is not None else 'n/a'} "
            f"class={thresholds.universe_class}"
        )
        return out

    # ── 2. Velocity ceiling ────────────────────────────────────────
    if (
        velocity_5m is not None
        and velocity_5m > thresholds.max_velocity_5m_pct
    ):
        out["block"] = True
        out["reason"] = MISSED_ENTRY_CHASE_RISK
        out["detail"] = (
            f"velocity_5m={velocity_5m:.2f}% > "
            f"{thresholds.max_velocity_5m_pct:.2f}% "
            f"({thresholds.universe_class} ceiling); phase={phase}"
        )
        return out

    # ── 3. VWAP-extension ceiling ──────────────────────────────────
    if (
        vwap_distance is not None
        and vwap_distance > thresholds.max_vwap_distance_pct
    ):
        out["block"] = True
        out["reason"] = TOO_FAR_ABOVE_VWAP
        out["detail"] = (
            f"vwap_distance_pct={vwap_distance:.2f}% > "
            f"{thresholds.max_vwap_distance_pct:.2f}% "
            f"({thresholds.universe_class} ceiling); phase={phase}"
        )
        return out

    # ── 4. Stale-intent revalidation against the FRESH price ───────
    if (
        extension_pct is not None
        and extension_pct > thresholds.max_extension_pct
    ):
        age = _f(intent_age_sec)
        stale = age is not None and age >= stale_age_sec()
        out["block"] = True
        out["reason"] = STALE_BUY_INTENT if stale else MOVE_ALREADY_EXTENDED
        out["detail"] = (
            f"price ran {extension_pct:.2f}% since emit "
            f"({emit:.4f} → {fresh:.4f}) > "
            f"{thresholds.max_extension_pct:.2f}% ceiling "
            f"({thresholds.universe_class})"
            + (f"; intent_age={age:.0f}s" if age is not None else "")
        )
        return out

    out["detail"] = (
        f"phase={phase} extension_pct="
        f"{out['extension_pct'] if out['extension_pct'] is not None else 'n/a'}"
    )
    return out

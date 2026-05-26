"""Micro-live sizing gate (Phase 4 Ladder Doctrine).

Doctrine pin (2026-05-26, operator-locked):
    Before any live-money order, MC enforces a hard per-order cap that
    overrides every other sizing input. The default is $5/order — small
    enough that a brain mistake costs lunch, not rent.

    The micro_live cap is INDEPENDENT from `exposure_caps.cap_for_lane`:
        * `cap_for_lane` is the engineering rail — what the system was
          built to tolerate (currently $500 crypto, $100k equity).
        * `micro_live` is the operator rail — what the operator wants
          to risk this week. It's tightenable via env without touching
          the engineering caps. Both rails are evaluated; the SMALLER
          wins. Doctrine: fail-closed to the tighter cap.

    Per-lane configuration so the operator can run, e.g., $5 crypto
    while equity stays at $100 paper. All env-driven.

Provenance: every clamped order carries `sizing_provenance` on its
receipt so the operator can trace exactly which rail bound the size.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

from shared.exposure_caps import cap_for_lane


logger = logging.getLogger("risedual.sizing_gate")


# ─── env-driven configuration ───
def _env_bool(key: str, default: bool = False) -> bool:
    return os.environ.get(key, str(default)).strip().lower() in {
        "true", "1", "yes", "on",
    }


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, str(default)))
    except (TypeError, ValueError):
        return default


# Master toggle. When True, every order is clamped to the micro_live
# cap. When False, only the engineering lane cap applies. Operator
# flips this via env when promoting from paper → first live trades.
MICRO_LIVE_ENABLED: bool = _env_bool("MICRO_LIVE_ENABLED", False)

# Default cap when no lane-specific override.
MICRO_LIVE_DEFAULT_CAP_USD: float = _env_float(
    "MICRO_LIVE_DEFAULT_CAP_USD", 5.0,
)

# Per-lane overrides. None = use default.
MICRO_LIVE_CRYPTO_CAP_USD: float = _env_float(
    "MICRO_LIVE_CRYPTO_CAP_USD", MICRO_LIVE_DEFAULT_CAP_USD,
)
MICRO_LIVE_EQUITY_CAP_USD: float = _env_float(
    "MICRO_LIVE_EQUITY_CAP_USD", MICRO_LIVE_DEFAULT_CAP_USD,
)


@dataclass
class SizingDecision:
    """Result of running the sizing gate."""
    requested_usd: float
    final_usd: float
    was_clamped: bool
    binding_rail: str   # "lane_cap" | "micro_live" | "none"
    micro_live_enabled: bool
    lane_cap_usd: float
    micro_live_cap_usd: Optional[float]
    lane: Optional[str]


def _micro_live_cap_for(lane: Optional[str]) -> float:
    """Resolve per-lane micro_live cap. Falls back to default."""
    if lane == "crypto":
        return MICRO_LIVE_CRYPTO_CAP_USD
    if lane == "equity":
        return MICRO_LIVE_EQUITY_CAP_USD
    return MICRO_LIVE_DEFAULT_CAP_USD


def evaluate_sizing(
    requested_usd: float, lane: Optional[str],
) -> SizingDecision:
    """Run both rails and return the tighter decision.

    Doctrine: never trusts the requested amount. Always evaluates both
    rails. Returns full provenance so receipts carry the audit trail.
    """
    # Fail-closed on garbage input.
    try:
        req = float(requested_usd)
    except (TypeError, ValueError):
        req = 0.0
    if req <= 0:
        return SizingDecision(
            requested_usd=req, final_usd=0.0, was_clamped=True,
            binding_rail="invalid_input", micro_live_enabled=MICRO_LIVE_ENABLED,
            lane_cap_usd=cap_for_lane(lane),
            micro_live_cap_usd=_micro_live_cap_for(lane) if MICRO_LIVE_ENABLED else None,
            lane=lane,
        )

    lane_cap = cap_for_lane(lane)
    candidates = [("lane_cap", lane_cap)]

    if MICRO_LIVE_ENABLED:
        ml_cap = _micro_live_cap_for(lane)
        candidates.append(("micro_live", ml_cap))

    # Find the smallest binding cap.
    binding_rail = "none"
    final = req
    for name, cap in candidates:
        if cap < final:
            final = cap
            binding_rail = name

    was_clamped = (final < req)
    return SizingDecision(
        requested_usd=req,
        final_usd=final,
        was_clamped=was_clamped,
        binding_rail=binding_rail,
        micro_live_enabled=MICRO_LIVE_ENABLED,
        lane_cap_usd=lane_cap,
        micro_live_cap_usd=(_micro_live_cap_for(lane) if MICRO_LIVE_ENABLED else None),
        lane=lane,
    )


def reload_env() -> None:
    """Re-read env vars. Used by tests + the kill-switch reload path
    so tightening micro_live mid-session doesn't require a redeploy."""
    global MICRO_LIVE_ENABLED, MICRO_LIVE_DEFAULT_CAP_USD
    global MICRO_LIVE_CRYPTO_CAP_USD, MICRO_LIVE_EQUITY_CAP_USD
    MICRO_LIVE_ENABLED = _env_bool("MICRO_LIVE_ENABLED", False)
    MICRO_LIVE_DEFAULT_CAP_USD = _env_float("MICRO_LIVE_DEFAULT_CAP_USD", 5.0)
    MICRO_LIVE_CRYPTO_CAP_USD = _env_float(
        "MICRO_LIVE_CRYPTO_CAP_USD", MICRO_LIVE_DEFAULT_CAP_USD,
    )
    MICRO_LIVE_EQUITY_CAP_USD = _env_float(
        "MICRO_LIVE_EQUITY_CAP_USD", MICRO_LIVE_DEFAULT_CAP_USD,
    )

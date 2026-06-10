"""Webull route caps — pre-trade gate for the Webull broker.

Doctrine pin (operator, 2026-06-10):

    Webull goes LIVE on day one. There is no paper/UAT stop. To keep
    the blast radius small while we shake the integration out, EVERY
    Webull order MUST pass through this gate before it can leave the
    backend:

        1. The "armed" gate (`WEBULL_ARMED=true`) must be flipped on
           in `.env` by the operator. Without it, the route is dead.
           Default is FALSE so a stale or accidentally-deployed env
           cannot trade.

        2. The notional MUST satisfy
               WEBULL_MIN_NOTIONAL_USD  ≤  notional  ≤  WEBULL_MAX_NOTIONAL_USD
           Defaults: $3.00 ≤ N ≤ $10.00. Operator can widen via env
           later, but the floor and ceiling exist precisely to keep
           the live-pilot cost-per-mistake bounded.

    These caps are ADDITIVE to the existing $500 exposure cap, the
    in-flight dedupe, and the position-misread detection — they do
    not replace any of them. They apply EXCLUSIVELY to orders that
    route via Webull; Kraken/Public.com orders are untouched.

Reading env vars at call-time (not import-time) so the operator can
flip the armed flag or widen the band without restarting supervisor.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional


# Sentinel values used by the router when the gate refuses an order.
DEFAULT_MIN_NOTIONAL_USD = 3.00
DEFAULT_MAX_NOTIONAL_USD = 10.00


class WebullCapBlocked(Exception):
    """Raised when the Webull pre-trade cap refuses an order. Treated
    as a fail-closed NO_TRADE by the broker router."""


@dataclass(frozen=True)
class WebullCapDecision:
    ok: bool
    reason: str
    min_usd: float
    max_usd: float
    armed: bool

    def raise_if_blocked(self) -> None:
        if not self.ok:
            raise WebullCapBlocked(self.reason)


def _read_float_env(key: str, default: float) -> float:
    """Tolerant float reader — empty or malformed env falls back to
    the doctrine default rather than crashing the route."""
    raw = (os.environ.get(key) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def is_webull_armed() -> bool:
    """Operator-flipped kill switch. Default OFF (fail-closed)."""
    return (os.environ.get("WEBULL_ARMED") or "").strip().lower() in {
        "true", "1", "yes", "on",
    }


def webull_notional_band() -> tuple[float, float]:
    """Return (min_usd, max_usd) for the Webull route.

    The floor floors at 0.01 and the ceiling caps at 100.0 even if the
    operator types something silly in `.env` — these are hardcoded
    sanity bounds, not the operator-tunable defaults.
    """
    lo = _read_float_env("WEBULL_MIN_NOTIONAL_USD", DEFAULT_MIN_NOTIONAL_USD)
    hi = _read_float_env("WEBULL_MAX_NOTIONAL_USD", DEFAULT_MAX_NOTIONAL_USD)
    # Sanity rails — never let the band invert and never let the
    # ceiling exceed the small-pilot ceiling the operator pinned.
    lo = max(0.01, lo)
    hi = max(lo, hi)
    hi = min(hi, 100.0)  # absolute panic ceiling
    return lo, hi


def evaluate_webull_order(
    *,
    notional_usd: Optional[float],
    symbol: str,
) -> WebullCapDecision:
    """The single decision point for whether an order can route via
    Webull. The router calls this BEFORE invoking the adapter.

    Returns a `WebullCapDecision`. Callers that want exception
    semantics can use `decision.raise_if_blocked()`.
    """
    lo, hi = webull_notional_band()
    armed = is_webull_armed()

    if not armed:
        return WebullCapDecision(
            ok=False,
            reason=(
                "WEBULL_NOT_ARMED — set WEBULL_ARMED=true in .env to "
                "enable the Webull route; NO_TRADE"
            ),
            min_usd=lo,
            max_usd=hi,
            armed=False,
        )

    if notional_usd is None:
        return WebullCapDecision(
            ok=False,
            reason="WEBULL_NOTIONAL_MISSING — router must pass notional_usd",
            min_usd=lo, max_usd=hi, armed=True,
        )

    if notional_usd < lo:
        return WebullCapDecision(
            ok=False,
            reason=(
                f"WEBULL_NOTIONAL_BELOW_FLOOR — ${notional_usd:.2f} "
                f"< ${lo:.2f} for {symbol}; NO_TRADE"
            ),
            min_usd=lo, max_usd=hi, armed=True,
        )

    if notional_usd > hi:
        return WebullCapDecision(
            ok=False,
            reason=(
                f"WEBULL_NOTIONAL_ABOVE_CAP — ${notional_usd:.2f} "
                f"> ${hi:.2f} for {symbol}; NO_TRADE"
            ),
            min_usd=lo, max_usd=hi, armed=True,
        )

    return WebullCapDecision(
        ok=True,
        reason="WEBULL_CAP_OK",
        min_usd=lo, max_usd=hi, armed=True,
    )

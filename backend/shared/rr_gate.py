"""Risk/Reward ratio gate (Phase A — equity only, 3:1 floor).

Doctrine pin (2026-05-27, operator-locked):
    Every executable equity entry intent must declare a target_price
    AND stop_price relative to its entry. The R:R floor is 3:1 —
    reward must equal at least 3× the risk. Phase A is fail-SOFT for
    intents missing either field (they pass with a warning reason so
    brain teams have a rollout window). Phase B will flip to
    fail-CLOSED on missing fields. The 3:1 ratio enforcement itself
    is HARD from day one.

Scope:
    * Lane:  equity ONLY (Phase A). Crypto has different liquidity
             dynamics and is evaluated separately later.
    * Verb:  BUY / SHORT only (entry actions). SELL / COVER are exit
             actions and skip the gate. OPEN is normalized to BUY or
             SHORT before this gate runs (via `_normalize_action`).
    * Fields: target_price + stop_price come from the brain's intent
             envelope. entry_price comes from `snapshot.price`
             (canonical) with `evidence.entry_price` as fallback.

Direction math:
    BUY  (long):  reward = target - entry ; risk = entry - stop
    SHORT (short):reward = entry - target ; risk = stop - entry
    Both reward AND risk MUST be > 0. A target on the wrong side of
    entry (or stop on the wrong side) is a HARD REJECT, not a soft
    warning — that's the brain saying something incoherent about
    direction.

Audit reasons (stable strings — pinned by tripwire):
    RR_RATIO_OK                  — pass; ratio >= rr_min
    RR_NOT_APPLICABLE_LANE       — non-equity (e.g., crypto); skipped
    RR_NOT_APPLICABLE_ACTION     — exit verb (SELL/COVER) or HOLD
    RR_MISSING_TARGET_OR_STOP    — Phase A fail-soft; pass with warning
    RR_MISSING_ENTRY_PRICE       — no snapshot.price; pass with warning
    RR_INVALID_PRICES            — direction-incoherent target/stop
    RR_RATIO_BELOW_FLOOR         — hard reject; reward/risk < rr_min
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, str(default)))
    except (TypeError, ValueError):
        return default


# Operator-tunable floor. 3:1 was deliberately chosen over 5:1 — high
# enough to filter weak setups, low enough that brain rollouts hit it
# realistically. Tightening to 4:1 / 5:1 is a one-env-var change.
RR_RATIO_MIN_EQUITY: float = _env_float("RR_RATIO_MIN_EQUITY", 3.0)

# Phase A: missing fields fail-SOFT (pass with warning). When the brain
# teams confirm target/stop are universally shipped, flip this env to
# `true` to make missing fields a hard reject.
RR_REQUIRE_FIELDS_HARD: bool = (
    os.environ.get("RR_REQUIRE_FIELDS_HARD", "false").strip().lower()
    in {"true", "1", "yes", "on"}
)


@dataclass(frozen=True)
class RRDecision:
    """Result of the R:R evaluation. Every field is on the audit row."""
    passed: bool
    reason: str
    rr_ratio: Optional[float]    # None when not computable
    rr_min: float
    reward: Optional[float]
    risk: Optional[float]
    entry_price: Optional[float]
    target_price: Optional[float]
    stop_price: Optional[float]
    direction: str               # "long" | "short" | "n/a"
    lane: Optional[str]
    action: Optional[str]
    phase_a_soft: bool           # True if this would be a hard reject in Phase B


def _f(value, default=None):
    """Coerce to float or return `default`. Catches None/blank/strings."""
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _direction_for_action(action: str) -> str:
    """BUY → long. SHORT → short. Otherwise n/a."""
    a = (action or "").upper()
    if a == "BUY":
        return "long"
    if a == "SHORT":
        return "short"
    return "n/a"


def evaluate_rr(
    intent: dict,
    *,
    rr_min: float = RR_RATIO_MIN_EQUITY,
) -> RRDecision:
    """Evaluate the R:R floor on an intent.

    Pure-function. Reads only from the intent dict; no DB, no env beyond
    the module-level config. Safe to call from gate chain, dry-run
    inspectors, and unit tests.
    """
    lane = (intent.get("lane") or "").lower() or None
    action = (intent.get("action") or "").upper() or None
    target_raw = intent.get("target_price")
    stop_raw = intent.get("stop_price")
    snapshot = intent.get("snapshot") or {}
    evidence = intent.get("evidence") or {}
    entry_raw = snapshot.get("price")
    if entry_raw is None:
        entry_raw = evidence.get("entry_price")

    target = _f(target_raw)
    stop = _f(stop_raw)
    entry = _f(entry_raw)

    direction = _direction_for_action(action or "")

    # Out-of-scope intents pass cleanly with an audit reason. Equity is
    # the only enforced lane in Phase A; everything else is a typed
    # pass-through so the audit log can show "we saw it, we let it
    # through, here's why".
    if lane != "equity":
        return RRDecision(
            passed=True, reason="RR_NOT_APPLICABLE_LANE",
            rr_ratio=None, rr_min=rr_min, reward=None, risk=None,
            entry_price=entry, target_price=target, stop_price=stop,
            direction=direction, lane=lane, action=action,
            phase_a_soft=False,
        )

    if direction == "n/a":
        return RRDecision(
            passed=True, reason="RR_NOT_APPLICABLE_ACTION",
            rr_ratio=None, rr_min=rr_min, reward=None, risk=None,
            entry_price=entry, target_price=target, stop_price=stop,
            direction=direction, lane=lane, action=action,
            phase_a_soft=False,
        )

    # Missing target OR stop — Phase A: fail-soft (warn, pass).
    # Phase B (env-flip) will flip the `passed` to False.
    if target is None or stop is None:
        return RRDecision(
            passed=(not RR_REQUIRE_FIELDS_HARD),
            reason="RR_MISSING_TARGET_OR_STOP",
            rr_ratio=None, rr_min=rr_min, reward=None, risk=None,
            entry_price=entry, target_price=target, stop_price=stop,
            direction=direction, lane=lane, action=action,
            phase_a_soft=True,
        )

    if entry is None:
        # Phase A: also fail-soft if entry price unavailable.
        # Phase B will harden once enrichment guarantees a price.
        return RRDecision(
            passed=(not RR_REQUIRE_FIELDS_HARD),
            reason="RR_MISSING_ENTRY_PRICE",
            rr_ratio=None, rr_min=rr_min, reward=None, risk=None,
            entry_price=entry, target_price=target, stop_price=stop,
            direction=direction, lane=lane, action=action,
            phase_a_soft=True,
        )

    # Direction-coherence check — incoherent prices are a hard reject
    # even in Phase A. The brain told us "long" but put the target
    # below entry: something is broken, not just unconfigured.
    if direction == "long":
        reward = target - entry
        risk = entry - stop
    else:  # short
        reward = entry - target
        risk = stop - entry

    if reward <= 0 or risk <= 0:
        return RRDecision(
            passed=False, reason="RR_INVALID_PRICES",
            rr_ratio=None, rr_min=rr_min, reward=reward, risk=risk,
            entry_price=entry, target_price=target, stop_price=stop,
            direction=direction, lane=lane, action=action,
            phase_a_soft=False,
        )

    ratio = reward / risk
    if ratio < rr_min:
        return RRDecision(
            passed=False, reason="RR_RATIO_BELOW_FLOOR",
            rr_ratio=ratio, rr_min=rr_min, reward=reward, risk=risk,
            entry_price=entry, target_price=target, stop_price=stop,
            direction=direction, lane=lane, action=action,
            phase_a_soft=False,
        )

    return RRDecision(
        passed=True, reason="RR_RATIO_OK",
        rr_ratio=ratio, rr_min=rr_min, reward=reward, risk=risk,
        entry_price=entry, target_price=target, stop_price=stop,
        direction=direction, lane=lane, action=action,
        phase_a_soft=False,
    )


def reload_env() -> None:
    """Re-read env vars. Lets tests + the kill-switch reload tighten
    the floor mid-session without a redeploy."""
    global RR_RATIO_MIN_EQUITY, RR_REQUIRE_FIELDS_HARD
    RR_RATIO_MIN_EQUITY = _env_float("RR_RATIO_MIN_EQUITY", 3.0)
    RR_REQUIRE_FIELDS_HARD = (
        os.environ.get("RR_REQUIRE_FIELDS_HARD", "false").strip().lower()
        in {"true", "1", "yes", "on"}
    )

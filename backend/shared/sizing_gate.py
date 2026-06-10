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

Doctrine pin (2026-02-17, Phase 4 ENGAGED):
    The ladder stage (per brain × lane) is now AUTHORITATIVE for
    sizing/routing. `evaluate_sizing_with_ladder()` reads the stage
    and:
        observation_only  → route="observe"     (no broker; write obs receipt)
        micro_paper       → route="paper"       (paper fire @ MICRO_PAPER_USD)
        micro_live        → route="live_micro"  (live fire @ MICRO_LIVE cap)
        normal_live       → route="live_normal" (full lane-cap sizing)

    The ladder cap participates in the "smallest-wins" comparison
    alongside `lane_cap` and `micro_live`. This means promoting a
    brain to `micro_paper` no longer requires the brain to also stop
    self-zeroing — MC's gate clamps to $10/order regardless of what
    the brain claimed it wanted to risk. The brain becomes a SIGNAL
    SOURCE; MC owns capital deployment.

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

# ─── Phase 4 ladder caps ───
# Per-rung notional defaults. Operator can tighten via env at any time.
LADDER_MICRO_PAPER_USD: float = _env_float("LADDER_MICRO_PAPER_USD", 10.0)
LADDER_MICRO_LIVE_USD: float = _env_float("LADDER_MICRO_LIVE_USD", 5.0)


# Routing tags carried on the receipt so the learning ladder can count
# fills per-stage (see `learning_ladder._paper_progress`).
ROUTE_OBSERVE = "observe"
ROUTE_PAPER = "paper"
ROUTE_LIVE_MICRO = "live_micro"
ROUTE_LIVE_NORMAL = "live_normal"

EXECUTION_MODE_FOR_ROUTE = {
    ROUTE_PAPER: "ladder_paper",
    ROUTE_LIVE_MICRO: "ladder_live_micro",
    ROUTE_LIVE_NORMAL: "live",
}


@dataclass
class SizingDecision:
    """Result of running the sizing gate."""
    requested_usd: float
    final_usd: float
    was_clamped: bool
    binding_rail: str   # "lane_cap" | "micro_live" | "ladder" | "none"
    micro_live_enabled: bool
    lane_cap_usd: float
    micro_live_cap_usd: Optional[float]
    lane: Optional[str]
    # ── Phase 4 ladder fields ──
    stage: Optional[str] = None
    route: Optional[str] = None
    ladder_cap_usd: Optional[float] = None
    execution_mode: Optional[str] = None


def _micro_live_cap_for(lane: Optional[str]) -> float:
    """Resolve per-lane micro_live cap. Falls back to default."""
    if lane == "crypto":
        return MICRO_LIVE_CRYPTO_CAP_USD
    if lane == "equity":
        return MICRO_LIVE_EQUITY_CAP_USD
    return MICRO_LIVE_DEFAULT_CAP_USD


def _ladder_cap_and_route(stage: str) -> tuple[Optional[float], str]:
    """Translate a ladder stage into (notional_cap, route).

    **2026-06-10 — LADDER GATE ELIMINATED (operator directive).**
    The per-brain × per-lane stage no longer gates execution. Every
    stage now resolves to `live_normal` with no ladder cap — sizing
    is bound only by the lane cap, micro_live cap (when enabled),
    and the broker-specific caps (Webull $3-$10, etc.).

    Reasoning: in 22,757 lifetime intents, only 1 was ever executed
    via this path because no formal promotion was ever performed.
    The wrapped brains (Camino/Barracuda/Hellcat/GTO) inherit their
    parents' posture; with the parents never formally promoted, the
    ladder was permanently shadow-locking the system. The audit log,
    `/promote`/`/demote` endpoints, and stage data model remain
    functional for forensic / historical reference, but they are no
    longer authoritative for routing.

    Other safety rails STAY ACTIVE: lane toggle, broker freeze,
    Webull cap, exposure cap, in-flight dedupe, MC receipt seal,
    position-misread detector.
    """
    # All stages → live_normal, no ladder cap. The `stage` argument
    # is preserved on the SizingDecision so dashboards/audits can
    # still display the historical stage even though it no longer
    # gates anything.
    return None, ROUTE_LIVE_NORMAL


def evaluate_sizing(
    requested_usd: float, lane: Optional[str],
) -> SizingDecision:
    """Run both rails and return the tighter decision.

    Doctrine: never trusts the requested amount. Always evaluates both
    rails. Returns full provenance so receipts carry the audit trail.

    LEGACY entry point — used by the manual /execution/submit path
    which doesn't know the calling brain. For ladder-aware sizing the
    auto_router uses `evaluate_sizing_with_ladder()` instead.
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


async def evaluate_sizing_with_ladder(
    requested_usd: float, brain: str, lane: Optional[str],
) -> SizingDecision:
    """Phase 4: ladder-aware sizing.

    Looks up the (brain, lane) ladder stage and folds it into the
    "smallest-wins" cap comparison. Returns SizingDecision with
    `stage`, `route`, `ladder_cap_usd`, `execution_mode` populated.

    Doctrine: the LADDER is authoritative. If the brain is at
    `observation_only`, this returns `route=observe` and the caller
    MUST NOT submit to the broker — write an observation receipt
    instead. At all other stages the ladder cap participates in the
    smallest-wins comparison alongside lane_cap and micro_live.
    """
    # Inline import to avoid a top-level cycle:
    # learning_ladder → namespaces → routes → sizing_gate.
    from shared.learning_ladder import get_stage  # noqa: WPS433

    try:
        req = float(requested_usd)
    except (TypeError, ValueError):
        req = 0.0

    # Default stage if the lookup fails (Mongo unreachable, unknown
    # brain): fail-closed to observation_only so misconfiguration
    # never accidentally fires real money.
    try:
        stage_doc = await get_stage(brain, lane or "")
        stage = stage_doc.get("stage", "observation_only")
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "evaluate_sizing_with_ladder: stage lookup failed for %s/%s: %s — "
            "failing closed to observation_only",
            brain, lane, e,
        )
        stage = "observation_only"

    ladder_cap, route = _ladder_cap_and_route(stage)
    execution_mode = EXECUTION_MODE_FOR_ROUTE.get(route)

    # observation_only short-circuit: don't bother running the rest of
    # the cap comparison; route=observe means the caller skips broker.
    if route == ROUTE_OBSERVE:
        return SizingDecision(
            requested_usd=req,
            final_usd=0.0,
            was_clamped=True,
            binding_rail="ladder_observation",
            micro_live_enabled=MICRO_LIVE_ENABLED,
            lane_cap_usd=cap_for_lane(lane),
            micro_live_cap_usd=(_micro_live_cap_for(lane) if MICRO_LIVE_ENABLED else None),
            lane=lane,
            stage=stage,
            route=route,
            ladder_cap_usd=0.0,
            execution_mode=execution_mode,
        )

    if req <= 0:
        return SizingDecision(
            requested_usd=req, final_usd=0.0, was_clamped=True,
            binding_rail="invalid_input",
            micro_live_enabled=MICRO_LIVE_ENABLED,
            lane_cap_usd=cap_for_lane(lane),
            micro_live_cap_usd=(_micro_live_cap_for(lane) if MICRO_LIVE_ENABLED else None),
            lane=lane,
            stage=stage, route=route, ladder_cap_usd=ladder_cap,
            execution_mode=execution_mode,
        )

    lane_cap = cap_for_lane(lane)
    candidates: list[tuple[str, float]] = [("lane_cap", lane_cap)]
    if MICRO_LIVE_ENABLED:
        candidates.append(("micro_live", _micro_live_cap_for(lane)))
    if ladder_cap is not None:
        candidates.append(("ladder", ladder_cap))

    # Smallest-wins. Note: at micro_paper / micro_live the ladder cap
    # is typically the smallest, so it dominates — exactly the
    # operator's "ladder is authoritative" pin.
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
        stage=stage,
        route=route,
        ladder_cap_usd=ladder_cap,
        execution_mode=execution_mode,
    )


def reload_env() -> None:
    """Re-read env vars. Used by tests + the kill-switch reload path
    so tightening micro_live mid-session doesn't require a redeploy."""
    global MICRO_LIVE_ENABLED, MICRO_LIVE_DEFAULT_CAP_USD
    global MICRO_LIVE_CRYPTO_CAP_USD, MICRO_LIVE_EQUITY_CAP_USD
    global LADDER_MICRO_PAPER_USD, LADDER_MICRO_LIVE_USD
    MICRO_LIVE_ENABLED = _env_bool("MICRO_LIVE_ENABLED", False)
    MICRO_LIVE_DEFAULT_CAP_USD = _env_float("MICRO_LIVE_DEFAULT_CAP_USD", 5.0)
    MICRO_LIVE_CRYPTO_CAP_USD = _env_float(
        "MICRO_LIVE_CRYPTO_CAP_USD", MICRO_LIVE_DEFAULT_CAP_USD,
    )
    MICRO_LIVE_EQUITY_CAP_USD = _env_float(
        "MICRO_LIVE_EQUITY_CAP_USD", MICRO_LIVE_DEFAULT_CAP_USD,
    )
    LADDER_MICRO_PAPER_USD = _env_float("LADDER_MICRO_PAPER_USD", 10.0)
    LADDER_MICRO_LIVE_USD = _env_float("LADDER_MICRO_LIVE_USD", 5.0)

"""Stack Personalities — doctrine surface (2026-02-15).

Each of the four runtimes carries a bounded operating style: a voice,
a bias, a risk posture, and namesake behavior. This file is the
single source of truth. UI / council / intent ingest / any future
consumer reads from here so personality never drifts across surfaces.

Hard rule (do not violate):
    Personality can change interpretation.
    Personality cannot change permissions.

Permissions live in seat_policy.py and are bound to the SEAT, not the
runtime. The fields below labelled `can_*` / `must_*` are mirrors of
the doctrinal authority limits for that runtime's *default* role — they
are advisory and informational. The actual gate enforcement is done
elsewhere (executor_seat.py, seat_policy.py, execution.py council
gates). If you add a new `can_*` field here, you MUST cross-check the
runtime's current seat in `seat_policy.may_*` before acting on it.

Doctrine source: operator specification 2026-02-15.

  Camaro:   decisive, execution-focused, practical
  Alpha:    opportunistic, growth-seeking, momentum-aware
  Chevelle: skeptical, disciplined, risk-governor (must SHAPE not FREEZE)
  REDEYE:   adversarial, bearish, threat-hunting (reports, never executes)
"""
from __future__ import annotations

from typing import Optional


STACK_PERSONALITIES: dict[str, dict] = {
    "camaro": {
        # ── Layer 1: voice / persona ────────────────────────────────
        "voice": "decisive_execution_operator",
        # ── Layer 2: bias profile (what it notices first) ───────────
        "bias": "safe_action_now",
        "bias_question": "Can this be safely acted on now?",
        # ── Layer 3: behavior + authority mirror ────────────────────
        "risk_posture": "controlled_aggressive",
        "default_weight": 1.00,
        "personality_description": (
            "Decisive, execution-focused, practical. Camaro answers "
            "'can this be safely acted on now?'. Translates strong "
            "conviction into shaped orders and never hesitates when "
            "the gate chain clears."
        ),
        # Permission MIRRORS (advisory — actual gating is seat-bound):
        "can_execute": True,
        "can_override_safety": False,   # caps + RoadGuard are absolute
        "never": [
            "override RoadGuard or hard caps",
            "fire without a passed gate chain",
        ],
    },
    "alpha": {
        "voice": "opportunity_hunter",
        "bias": "momentum_and_upside",
        "bias_question": "Where is the asymmetric upside?",
        "risk_posture": "growth_seeking",
        "default_weight": 0.90,
        "personality_description": (
            "Opportunistic, growth-seeking, momentum-aware. Alpha "
            "answers 'where is the asymmetric upside?'. Surfaces "
            "high-convexity setups but defers execution to the "
            "current Executor seat holder."
        ),
        "can_execute": False,
        "can_override_safety": False,
        "never": [
            "force trades through failed consensus or safety gates",
            "act as Executor unless explicitly slotted there",
        ],
    },
    "chevelle": {
        "voice": "risk_governor",
        "bias": "skeptical_validation",
        "bias_question": "What could go wrong and what size is justified?",
        "risk_posture": "capital_preservation",
        "default_weight": 0.65,
        "personality_description": (
            "Skeptical, disciplined, risk-governor. Chevelle answers "
            "'what could go wrong and what size is justified?'. "
            "Returns a SHAPED risk multiplier — never freezes by "
            "default, never hard-vetoes without high-conviction "
            "evidence."
        ),
        "can_execute": False,
        "must_return_multiplier": True,
        "can_hard_veto": False,        # mirrors GOVERNOR_HARD_VETO doctrine
        "never": [
            "freeze everything by default — must shape, not stop",
            "hard-veto without crossing the doctrinal threshold "
            "(GOVERNOR_HARD_VETO_THRESHOLD, see execution.py)",
        ],
    },
    "redeye": {
        "voice": "adversarial_short_hunter",
        "bias": "downside_and_trap_detection",
        "bias_question": "Where is the downside, trap, or short opportunity?",
        "risk_posture": "defensive_adversarial",
        "default_weight": 0.80,
        "personality_description": (
            "Adversarial, bearish, threat-hunting. REDEYE answers "
            "'where is the downside, trap, or short opportunity?'. "
            "Reports threat scores and short theses to whoever holds "
            "the Executor seat; never executes directly."
        ),
        "can_execute": False,
        "reports_to": "executor_seat",  # reports to whoever IS the executor
        "never": [
            "execute orders directly",
            "down-weight beyond MAX_SINGLE_AGENT_INFLUENCE clamp",
        ],
    },
}


# ─────────────────────── helpers ────────────────────────

def personality_of(stack: str) -> Optional[dict]:
    """Return the personality config for `stack`, or None for unknown."""
    return STACK_PERSONALITIES.get((stack or "").lower())


def voice_of(stack: str) -> Optional[str]:
    p = personality_of(stack)
    return p["voice"] if p else None


def bias_of(stack: str) -> Optional[str]:
    p = personality_of(stack)
    return p["bias"] if p else None


def default_weight(stack: str) -> float:
    """Personality-baseline weight for a stack. Caller applies clamps."""
    p = personality_of(stack)
    return float(p["default_weight"]) if p else 1.0


def respects_hard_limits(stack: str, action: str) -> bool:
    """Best-effort check: does the requested `action` (free text or
    code-like) collide with a `never` clause of this stack's personality?

    NOT an authority check — actual permission gating happens in
    seat_policy and the council gates. This is a soft assertion the
    caller can use to enrich an audit row with `hard_limits_respected`.
    """
    p = personality_of(stack)
    if not p:
        return True  # unknown stack: don't pretend to know
    if not action:
        return True
    a = action.lower()
    for forbidden in p.get("never", []):
        # extremely conservative substring check: only flag if action
        # text contains an explicit overlap with the doctrine "never"
        # phrase tokens. This is informational, not gating.
        tokens = [t for t in forbidden.lower().split() if len(t) >= 4]
        if any(t in a for t in tokens):
            return False
    return True


def enrich_response(stack: str, response: dict) -> dict:
    """Stamp a stack response dict with its personality envelope.

    Inserts `stack`, `personality_bias`, `voice` and ensures
    `hard_limits_respected` is present (defaulting to True if the
    caller didn't precompute it). Idempotent — safe to call multiple
    times. Returns the same dict for fluent chaining.
    """
    p = personality_of(stack)
    if not p:
        return response
    response.setdefault("stack", stack)
    response.setdefault("personality_bias", p["bias"])
    response.setdefault("voice", p["voice"])
    response.setdefault("default_weight", p["default_weight"])
    if "hard_limits_respected" not in response:
        response["hard_limits_respected"] = respects_hard_limits(
            stack, response.get("reason") or response.get("action") or ""
        )
    return response

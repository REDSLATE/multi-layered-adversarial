"""RISEDUAL — Doctrine Injection Layer.

Runtime behavioral overlays for lane/regime adaptation.

DOCTRINE:
  - Overlays may influence INTERPRETATION
  - Overlays may NOT bypass safety invariants
  - No overlay may:
        * bypass RoadGuard
        * bypass exposure caps
        * promote HOLD to trade
        * disable audit logging
        * bypass operator gates
        * cross lane boundaries
  - Bounded modulation only — weights clamped to [0.50, 1.25]
  - Forbidden mutations rejected at registration time
  - Every overlay event is audit-logged to mc_shelly

Adapted from the operator-provided spec 2026-02-15. Profiles are
sourced from `stack_personalities.STACK_PERSONALITIES` (single source
of truth) — this layer overlays them, never replaces them.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from shared.mc_shelly import record_async
from shared.stack_personalities import STACK_PERSONALITIES


# ────────────────────────── invariants ──────────────────────────

SAFETY_INVARIANTS: dict[str, bool] = {
    "roadguard_required": True,
    "hold_not_promotable": True,
    "operator_gate_required": True,
    "audit_logging_required": True,
    "lane_isolation_required": True,
    "bounded_modulation_only": True,
}

# Bounded-modulation clamps. An overlay can shape a stack weight within
# these bounds — never below or above. Mirrors execution.py clamps for
# consistency across the two layers.
WEIGHT_MIN = 0.50
WEIGHT_MAX = 1.25

# Keys an overlay's metadata is forbidden from carrying. Any of these
# would constitute an authority escalation, which overlays MAY NOT do.
FORBIDDEN_METADATA_KEYS = frozenset({
    "may_execute",
    "may_override_safety",
    "disable_roadguard",
    "disable_caps",
    "disable_audit",
    "bypass_seat_policy",
    "promote_hold",
})


def _default_profile(stack: str) -> dict:
    """Pull the baseline profile from the doctrine surface. We keep the
    overlay output shape compatible with the operator spec (role_bias,
    risk_posture, weight, may_execute) so consumers don't need to know
    about the bigger personality envelope."""
    p = STACK_PERSONALITIES.get((stack or "").lower(), {})
    return {
        "role_bias":    p.get("bias"),
        "risk_posture": p.get("risk_posture"),
        "weight":       float(p.get("default_weight") or 1.0),
        # `may_execute` mirrors personality but the AUTHORITY check is
        # always done at the seat level — this is descriptive only.
        "may_execute":  bool(p.get("can_execute", False)),
    }


# ────────────────────────── overlay model ──────────────────────────

@dataclass
class DoctrineOverlay:
    """A scoped behavioral overlay. All fields optional; absence = match-any."""
    overlay_id: str

    lane: Optional[str] = None
    regime: Optional[str] = None
    volatility: Optional[str] = None
    event_type: Optional[str] = None

    expires_at: Optional[datetime] = None

    stack_weights: dict[str, float] = field(default_factory=dict)
    governor_policy: dict[str, Any] = field(default_factory=dict)
    personality_bias: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    created_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc),
    )
    enabled: bool = True

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        for k in ("expires_at", "created_at"):
            v = d.get(k)
            if isinstance(v, datetime):
                d[k] = v.isoformat()
        return d


# ────────────────────────── engine ──────────────────────────

class DoctrineInjectionEngine:
    """Runtime overlay manager. Applies temporary operational doctrine
    overlays WITHOUT modifying base intelligence."""

    def __init__(self) -> None:
        self.active_overlays: dict[str, DoctrineOverlay] = {}

    # ── registration ──────────────────────────────────────────
    def register_overlay(self, overlay: DoctrineOverlay) -> None:
        self._validate_overlay(overlay)
        self.active_overlays[overlay.overlay_id] = overlay
        self._audit(
            event="overlay_registered",
            overlay_id=overlay.overlay_id,
            lane=overlay.lane,
            regime=overlay.regime,
            volatility=overlay.volatility,
            event_type=overlay.event_type,
        )

    def remove_overlay(self, overlay_id: str) -> bool:
        if overlay_id not in self.active_overlays:
            return False
        del self.active_overlays[overlay_id]
        self._audit(event="overlay_removed", overlay_id=overlay_id)
        return True

    def list_overlays(self) -> list[dict]:
        return [o.to_dict() for o in self.active_overlays.values()]

    # ── lookup ────────────────────────────────────────────────
    def get_runtime_profile(
        self,
        stack_name: str,
        lane: str,
        regime: Optional[str] = None,
        volatility: Optional[str] = None,
        event_type: Optional[str] = None,
    ) -> dict:
        profile = copy.deepcopy(_default_profile(stack_name))
        applicable = self._matching_overlays(
            lane=lane,
            regime=regime,
            volatility=volatility,
            event_type=event_type,
        )
        applied: list[str] = []

        for overlay in applicable:
            # ── stack weight overrides ────────────────────────
            if stack_name in overlay.stack_weights:
                base = profile["weight"]
                shaped = base * float(overlay.stack_weights[stack_name])
                shaped = max(WEIGHT_MIN, min(WEIGHT_MAX, shaped))
                profile["weight"] = round(shaped, 4)

            # ── personality bias overrides ────────────────────
            if stack_name in overlay.personality_bias:
                profile["temporary_bias"] = overlay.personality_bias[stack_name]

            # ── governor policy ───────────────────────────────
            # Only Chevelle (or whoever holds a governor seat — we key
            # on stack name here per the operator spec). The council's
            # actual gate logic in execution.py is unchanged — this is
            # advisory metadata the council CAN read to soften its
            # damping if it wants. Authority remains seat-bound.
            if stack_name == "chevelle" and overlay.governor_policy:
                profile["governor_policy"] = dict(overlay.governor_policy)

            applied.append(overlay.overlay_id)

        profile["applied_overlays"] = applied
        return profile

    # ── matching ──────────────────────────────────────────────
    def _matching_overlays(
        self,
        lane: str,
        regime: Optional[str],
        volatility: Optional[str],
        event_type: Optional[str],
    ) -> list[DoctrineOverlay]:
        now = datetime.now(timezone.utc)
        matches: list[DoctrineOverlay] = []
        for overlay in self.active_overlays.values():
            if not overlay.enabled:
                continue
            if overlay.expires_at and now > overlay.expires_at:
                continue
            if overlay.lane and overlay.lane != lane:
                continue
            if overlay.regime and overlay.regime != regime:
                continue
            if overlay.volatility and overlay.volatility != volatility:
                continue
            if overlay.event_type and overlay.event_type != event_type:
                continue
            matches.append(overlay)
        return matches

    # ── safety gates ──────────────────────────────────────────
    def _validate_overlay(self, overlay: DoctrineOverlay) -> None:
        # Bounded weights only.
        for stack, weight in overlay.stack_weights.items():
            if weight < WEIGHT_MIN:
                raise ValueError(
                    f"{stack} overlay weight {weight} below minimum {WEIGHT_MIN}"
                )
            if weight > WEIGHT_MAX:
                raise ValueError(
                    f"{stack} overlay weight {weight} above maximum {WEIGHT_MAX}"
                )
        # Forbidden mutations — any attempt to bypass safety is rejected
        # at registration time, before the overlay enters the registry.
        for key in overlay.metadata.keys():
            if key in FORBIDDEN_METADATA_KEYS:
                raise ValueError(f"forbidden overlay mutation: {key}")

    # ── audit ─────────────────────────────────────────────────
    def _audit(self, **payload) -> None:
        """Append-only audit via mc_shelly (the canonical training/audit
        store). All overlay lifecycle events are recorded so the operator
        can trace WHEN an overlay was active and WHAT it changed."""
        event_type = payload.pop("event", "doctrine_overlay")
        rationale = payload.pop("rationale", None)
        record_async(
            event_type=f"doctrine.{event_type}",
            brain=None,
            symbol=None,
            action=None,
            outcome=None,
            rationale=rationale or str(payload),
            ref_id=payload.get("overlay_id"),
            extra=payload,
        )


# ────────────────────────── ready-made overlays ──────────────────────────

def build_crypto_breakout_overlay() -> DoctrineOverlay:
    return DoctrineOverlay(
        overlay_id="crypto_breakout_v1",
        lane="crypto",
        volatility="high",
        stack_weights={
            "redeye":   1.15,
            "alpha":    1.05,
            "chevelle": 0.80,
        },
        governor_policy={
            "downweight_floor": 0.75,
            "lighter_damping": True,
        },
        personality_bias={
            "redeye": "trap_hunter",
            "alpha":  "momentum_expansion",
            "camaro": "execution_precision",
        },
        metadata={"description": "High-volatility crypto breakout doctrine"},
    )


def build_fomc_overlay() -> DoctrineOverlay:
    return DoctrineOverlay(
        overlay_id="fomc_event_guard_v1",
        event_type="fomc",
        stack_weights={
            "chevelle": 1.10,
            "alpha":    0.85,
        },
        governor_policy={
            "max_position_reduction": 0.65,
            "volatility_guard": True,
        },
        personality_bias={
            "chevelle": "macro_risk_focus",
        },
        metadata={"description": "FOMC macro volatility defense doctrine"},
    )


# ────────────────────────── module singleton ──────────────────────────

# One engine per process. State is in-memory — overlays do not survive
# a backend restart by design. If you need persistence across restarts,
# we can later mirror them to Mongo, but the operator spec calls for
# transient overlays (event-driven, regime-driven), so in-memory is
# the safer default.
_ENGINE: Optional[DoctrineInjectionEngine] = None


def get_engine() -> DoctrineInjectionEngine:
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = DoctrineInjectionEngine()
    return _ENGINE

"""Auto-router helper primitives — extracted from `_route_one`.

2026-07-12 (P6b partial): the notional resolution rules and the
`RouteContext` dataclass live here so `_route_one` doesn't have
to declare them inline. Full 5-stage breakout of `_route_one`
into (`_gate_master_switch`, `_gate_seat`, `_gate_risk`,
`_route_and_submit`, `_finalize_gate_state`) is a follow-up
migration — this file lands the primitives it will consume.

No behavior change from the pre-extraction code path.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Optional


AUTO_ROUTER_NOTIONAL_USD = float(os.environ.get("AUTO_ROUTER_NOTIONAL_USD", "10"))


@dataclass
class RouteContext:
    """The state `_route_one` accumulates across its stages.

    Instead of 15+ local variables threaded through nested try/except
    blocks, each stage receives + mutates a single `RouteContext`.

    Populated incrementally:
        - Constructor: intent, action_upper (from the raw intent dict).
        - _resolve_notional: notional_raw, notional_source.
        - _gate_master_switch: (no mutation; may raise/return).
        - _gate_seat: sd (SeatDecision), sd.reason, sd.verdict.
        - _gate_risk: rc (RiskDecision), notional_usd (post-risk).
        - _route_and_submit: broker_response, terminal_state.
        - _finalize_gate_state: (writes gate_state on the intent).

    Kept as a plain dataclass (not frozen) so stage functions can
    mutate specific fields — the semantic is "accumulator" not
    "immutable value object".
    """
    intent: dict
    action_upper: str = ""
    notional_raw: float = 0.0
    notional_source: str = ""
    notional_usd: Optional[float] = None  # post-risk sizing
    sd: Any = None                        # SeatDecision (avoid circular import)
    rc: Any = None                        # RiskDecision
    broker_response: dict = field(default_factory=dict)
    terminal_state: Optional[str] = None
    # Diagnostic accumulator — stage-specific reason codes get appended
    # here for eventual persistence on the intent doc.
    reason_trail: list[str] = field(default_factory=list)


def resolve_notional(intent: dict) -> tuple[float, str]:
    """Notional resolution rules (2026-07-09 operator directive).

    Doctrine:
      Market data → brain BUY/SELL → doctrine scores quality →
      **executor assigns notional (THIS FUNCTION)** → capital ledger
      reserves → broker submits.

    Rule (assign_micro_notional):
      1. If the brain already sized the intent (legacy or v3), USE IT.
         → notional_source ∈ {"brain_legacy", "brain_v3"}
      2. Directional intent (BUY/SELL) with no size AND doctrine
         flagged any failed checks → $1 quality-weak probe.
         → notional_source = "micro_probe_failed_quality"
      3. Directional intent (BUY/SELL) with no size AND doctrine is
         clean (no failed checks) → $5 default probe.
         → notional_source = "micro_default"
      4. Non-directional (HOLD/...) → env default ($10).
         → notional_source = "env_default"

    Pure function — no I/O, no side effects.
    """
    exec_block = intent.get("execution") or {}
    action_upper = str(intent.get("action") or "").upper()
    v3_notional = (
        exec_block.get("notional_usd") if isinstance(exec_block, dict) else None
    )
    legacy_notional = intent.get("requested_notional_usd")

    if legacy_notional not in (None, 0, 0.0):
        return float(legacy_notional), "brain_legacy"
    if v3_notional not in (None, 0, 0.0):
        return float(v3_notional), "brain_v3"
    if action_upper in {"BUY", "SELL"}:
        # Brain made a directional move but didn't size it.
        # Consult the doctrine packet — if ANY quality checks failed,
        # ship a $1 probe; otherwise a $5 default probe.
        try:
            dp = intent.get("doctrine_packet") or {}
            seats_dp = (dp.get("seats") or {}) if isinstance(dp, dict) else {}
            ej = seats_dp.get("execution_judge") or {}
            failed = list(ej.get("failed_checks") or [])
        except Exception:  # noqa: BLE001
            failed = []
        if failed:
            return (
                float(os.environ.get("MICRO_PROBE_FAILED_QUALITY_USD", "1.00")),
                "micro_probe_failed_quality",
            )
        return (
            float(os.environ.get("MICRO_LIVE_DEFAULT_USD", "5.00")),
            "micro_default",
        )
    return AUTO_ROUTER_NOTIONAL_USD, "env_default"

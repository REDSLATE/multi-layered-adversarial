"""Pydantic models for Paradox v2's seven canonical collections.

Each model documents WHICH LAYER owns it and what the layer is allowed
to read/write. Cross-layer reads are explicit and never silent.

Layer ownership map:
    brain_registry         → Brain layer (doctrine, version)
    seat_trusted_brains    → Seat layer (trust list)
    seat_policy_config     → Seat layer (capital, autonomy)
    governor_modifier_rules→ Governor layer (structured modifiers)
    seat_performance       → Verifier layer (P&L, win rate)
    seat_promotion_log     → Verifier layer (autonomy progression audit)
    roadguard_stops        → RoadGuard layer (binary stops)
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, Literal, Any
from pydantic import BaseModel, Field, ConfigDict


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─── Brain layer ──────────────────────────────────────────────────────


BrainDoctrine = Literal["adversarial", "trend", "mean_reversion", "tape_reading"]


class BrainRegistryDoc(BaseModel):
    """Brain layer — a brain's pure identity + doctrine.

    The brain layer NEVER reads from any other layer. It emits an
    opinion (`BrainOpinion` below) and that's it.
    """
    model_config = ConfigDict(extra="forbid")

    brain_id: str = Field(..., description="canonical id: alpha|camaro|chevelle|redeye|…")
    display_name: str = Field(..., description="operator-facing brand: Camino, Barracuda, Hellcat, GTO")
    doctrine: BrainDoctrine
    version: str = Field("1.0.0")
    is_active: bool = Field(True)
    created_at: str = Field(default_factory=_now)
    updated_at: str = Field(default_factory=_now)


class BrainOpinion(BaseModel):
    """Brain layer output — the ONLY thing a brain hands to the seat.

    The brain has NO knowledge of seats, policies, capital, or market
    structure. It speaks pure conviction + evidence.
    """
    model_config = ConfigDict(extra="forbid")

    brain_id: str
    symbol: str
    lane: Literal["equity", "crypto"]
    action: Literal["BUY", "SELL", "HOLD"]
    confidence: float = Field(..., ge=0.0, le=1.0)
    suggested_notional_usd: float = Field(..., ge=0.0)
    evidence: dict[str, Any] = Field(default_factory=dict)
    emitted_at: str = Field(default_factory=_now)


# ─── Seat layer ───────────────────────────────────────────────────────


AutonomyMode = Literal["observe", "shadow", "toehold", "auto_execute"]
InstrumentType = Literal["equity_long", "equity_short", "options", "crypto_spot"]
# Instrument registry (Phase 3 onboarding, 2026-02-19):
#
#   equity_long   — buy-side equities. Default at boot (equity_executor).
#   equity_short  — short-side equities. Pilot seat: spot_short_executor.
#   options       — listed US options. Pilot seat: options_executor.
#   crypto_spot   — spot crypto on Kraken. Default crypto_executor.
#
# instrument_type is METADATA only — it labels what the seat trades for
# operator/UI clarity and verifier grouping. It does NOT gate execution;
# capital + trust + autonomy still own that boundary. New instrument
# types onboard by adding a new seat row here AND a literal value above.
# Autonomy progression doctrine (2026-02-19, locked):
#
#   Seat always decides. Autonomy mode decides whether that decision
#   becomes an order. Observe and shadow modes do not simulate trades;
#   they only log seat decisions and verifier-readable receipts. Live
#   orders begin only at toehold mode.
#
# Canonical flow:
#   Brain opinion → Seat decision → Governor modifier → RoadGuard check
#                 → autonomy_mode gate → receipt or order
#
# Per-mode behaviour:
#   observe       — seat decides but does NOT place an order. The
#                   EvaluationReceipt with decision=BLOCKED is the only
#                   artifact. Verifier reads these to grade
#                   decision-quality before risking a single dollar.
#   shadow        — same no-order behaviour as observe. Distinct only
#                   so the verifier can require BOTH a clean observe
#                   window AND a clean shadow window with stricter
#                   evidence-quality gates before promoting.
#   toehold       — live execution at a heavily-reduced size cap
#                   (operator sets via seat_policy.max_notional_usd
#                   and size_multiplier — typically 5-10% of full).
#   auto_execute  — live execution at full per-policy notional.
#
# There are NO paper trades in this system. The decision-vs-execute
# split lives entirely in the seat's autonomy_mode, not in a separate
# paper broker.


class SeatTrustedBrain(BaseModel):
    """Seat layer — declares which brains a seat trusts.

    The seat does NOT inspect the brain's doctrine. It only checks if
    the brain_id is in its trust list. This is the IP boundary: brain
    doctrine stays in the brain layer; trust is a pure seat opinion.
    """
    model_config = ConfigDict(extra="forbid")

    seat_id: str = Field(..., description="canonical seat: equity_executor|crypto_executor|…")
    brain_id: str
    trust_level: float = Field(1.0, ge=0.0, le=1.0)
    added_at: str = Field(default_factory=_now)
    added_by: str = Field("seed", description="operator email or 'seed'/'verifier'")


class SeatPolicyConfig(BaseModel):
    """Seat layer — capital + autonomy state for one seat.

    The seat decides final notional, max-position-count, daily risk.
    The brain's suggested_notional is a SUGGESTION only; the seat
    can scale it down via `size_multiplier` or refuse it via the
    notional/risk gates.
    """
    model_config = ConfigDict(extra="forbid")

    seat_id: str
    autonomy_mode: AutonomyMode = Field("observe", description="observe → shadow → toehold → auto_execute")
    instrument_type: InstrumentType = Field(
        "equity_long",
        description="What this seat trades. Metadata only — capital + trust + autonomy still gate execution.",
    )
    enabled: bool = Field(True)

    # Capital
    max_notional_usd: float = Field(5_000.0, ge=0.0)
    size_multiplier: float = Field(0.50, ge=0.0, le=2.0)
    daily_risk_budget_usd: float = Field(25_000.0, ge=0.0)
    max_position_count: int = Field(10, ge=0)
    max_concentration_pct: float = Field(25.0, ge=0.0, le=100.0)

    # Brain-quality gates
    # 2026-02-20: default loosened 0.85 → 0.70 to match the operator's
    # preferred per-deploy hand-flip. Seat is the restriction authority
    # in the doctrine, so operators can hand-tune per-seat via the
    # admin endpoint if they want tighter; this is just the floor for
    # fresh deploys / disarm-then-arm cycles.
    confidence_min: float = Field(0.70, ge=0.0, le=1.0)
    market_quality_min: float = Field(0.60, ge=0.0, le=1.0)

    # Governance
    # 2026-02-20: `max_auditor_objections` field deleted — it was
    # declared on the model and seeded across all four seats but
    # never enforced anywhere in the codebase. Auditor objections
    # are advisory only per operator doctrine ("Brain = opinion
    # only"), so the field had no honest place here. If auditor-
    # objection counting ever needs to gate execution, do it via a
    # seat-policy field that's actually wired into the evaluator.
    required_governor_stance: Optional[Literal["RISK_DOWN", "NEUTRAL", "RISK_UP"]] = Field("RISK_DOWN")

    updated_at: str = Field(default_factory=_now)
    updated_by: str = Field("seed")


# ─── Governor layer ───────────────────────────────────────────────────


class GovernorModifierRule(BaseModel):
    """Governor layer — structured risk-down modifiers.

    NEVER blocks. NEVER vetoes. Only outputs a structured payload the
    seat applies to its final size decision. Blocking is RoadGuard's
    job (binary). Veto is the auditor's job (handled in Phase 2 vote
    escalation, not here).
    """
    model_config = ConfigDict(extra="forbid")

    rule_id: str
    trigger_type: str = Field(..., description="wide_spread|low_rvol|earnings_window|halt_risk|…")
    trigger_threshold: float = Field(0.0)
    size_multiplier: float = Field(1.0, ge=0.0, le=1.0)
    vote_required: bool = Field(False)
    increase_scrutiny: bool = Field(False)
    flag_anomaly: bool = Field(False)
    reason_template: str = Field("")
    is_active: bool = Field(True)
    created_at: str = Field(default_factory=_now)


# ─── Verifier layer ───────────────────────────────────────────────────


class SeatPerformanceWindow(BaseModel):
    """Verifier layer — rolling P&L window for one seat.

    The verifier reads this to decide autonomy promotion/demotion.
    The seat itself NEVER reads this — it would couple capital
    decisions to historical performance, which is the verifier's
    job, not the seat's.
    """
    model_config = ConfigDict(extra="forbid")

    seat_id: str
    window_start: str
    window_end: Optional[str] = None
    total_trades: int = Field(0, ge=0)
    winning_trades: int = Field(0, ge=0)
    win_rate: float = Field(0.0, ge=0.0, le=1.0)
    daily_pnl_usd: float = Field(0.0)
    daily_risk_used_usd: float = Field(0.0, ge=0.0)
    sharpe_ratio: Optional[float] = None
    updated_at: str = Field(default_factory=_now)


class SeatPromotionLogEntry(BaseModel):
    """Verifier layer — append-only audit of every autonomy change.

    Written by the verifier and (rarely) the operator. Never edited.
    """
    model_config = ConfigDict(extra="forbid")

    seat_id: str
    from_mode: AutonomyMode
    to_mode: AutonomyMode
    reason: str
    triggered_by: str = Field(..., description="'verifier' or operator email")
    metrics_snapshot: dict[str, Any] = Field(default_factory=dict)
    ts: str = Field(default_factory=_now)


# ─── RoadGuard layer ──────────────────────────────────────────────────


class RoadGuardStop(BaseModel):
    """RoadGuard layer — binary STOP. No gradations, no modifiers.

    A row here means: the seat is blocked. Period. Cleared by writing
    `cleared_at`. Never updated in place.
    """
    model_config = ConfigDict(extra="forbid")

    seat_id: str
    is_active: bool = Field(True)
    reason: str
    triggered_by: str = Field(..., description="'verifier'|'roadguard'|operator email")
    created_at: str = Field(default_factory=_now)
    cleared_at: Optional[str] = None
    cleared_by: Optional[str] = None


# ─── Evaluation receipt ───────────────────────────────────────────────


class EvaluationReceipt(BaseModel):
    """Pipeline output — one row per /api/v2/evaluate call.

    This is the audit trail. Every decision point in the pipeline
    appears here so the operator (and future verifier) can replay
    exactly why an opinion did or didn't execute.
    """
    model_config = ConfigDict(extra="forbid")

    evaluation_id: str
    seat_id: str
    opinion: dict[str, Any]
    decision: Literal["EXECUTED", "REJECTED_SEAT", "REJECTED_ROADGUARD", "BLOCKED", "PENDING_VOTE"]
    reason: str
    final_notional_usd: Optional[float] = None
    pipeline_trace: dict[str, Any] = Field(default_factory=dict)
    ts: str = Field(default_factory=_now)

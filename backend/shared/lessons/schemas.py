"""Lesson schemas — one labeled record per intent.

Pinned shape so the report-card aggregator and setup-memory adjuster
can both reason about the same fields without duplicate parsing.

All floats are Python-native (no numpy) so the row is trivially JSON
serializable for the read-side endpoints.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional


LessonOutcome = Literal[
    "win",        # net favorable move beyond cost, position resolved profitably
    "loss",       # net adverse move, position resolved at a loss
    "scratch",    # flat — exited near entry, no material edge proved
    "missed",     # brain emitted BUY/SELL but gate blocked; market moved favorably
    "avoided",    # brain emitted BUY/SELL but gate blocked; market moved adversely
    "pending",    # position open, outcome not yet labelable
    "unknown",    # data insufficient for a verdict
]


@dataclass(frozen=True)
class Lesson:
    """One labeled lesson — joins intent + research + execution + outcome.

    Fields are grouped by the layer that owns them:

      • brain layer:      stack, action, confidence, rationale
      • research layer:   research_signals[], research_status,
                          research_strongest_direction, research_score
      • market context:   symbol, lane, regime, market_quality_score,
                          spread_bps
      • gate layer:       seat_holder, governor_multiplier, gate_state,
                          blocked_by, dry_run_state, executed
      • execution layer:  fill_price, fill_qty, fill_ts, slippage_bps
      • position layer:   exit_price, exit_ts, holding_period_sec,
                          mae_bps, mfe_bps, pnl_bps, pnl_usd
      • verdict:          setup_id, outcome
    """
    # Identity / brain layer
    intent_id: str
    stack: str
    lane: str
    symbol: str
    action: str
    confidence: float
    rationale: Optional[str] = None
    posted_at: Optional[str] = None    # ISO

    # Research layer
    research_signals: list[dict] = field(default_factory=list)
    research_status: Optional[str] = None
    research_strongest_direction: Optional[str] = None   # BUY|SELL|HOLD or None
    research_score: Optional[float] = None
    research_source: Optional[str] = None
    research_tf: Optional[str] = None

    # Market context
    regime: Optional[str] = None
    market_quality_score: Optional[float] = None
    spread_bps: Optional[float] = None

    # Gate layer
    seat_holder_at_post: Optional[str] = None
    governor_multiplier: Optional[float] = None
    gate_state: Optional[str] = None
    dry_run_state: Optional[str] = None
    blocked_by: list[str] = field(default_factory=list)
    executed: bool = False

    # Execution layer
    fill_price: Optional[float] = None
    fill_qty: Optional[float] = None
    fill_ts: Optional[str] = None
    slippage_bps: Optional[float] = None

    # Position lifecycle
    exit_price: Optional[float] = None
    exit_ts: Optional[str] = None
    holding_period_sec: Optional[float] = None
    mae_bps: Optional[float] = None       # max ADVERSE excursion (>= 0)
    mfe_bps: Optional[float] = None       # max FAVORABLE excursion (>= 0)
    pnl_bps: Optional[float] = None
    pnl_usd: Optional[float] = None

    # Verdict
    setup_id: Optional[str] = None        # e.g. "crypto_breakdown_v1:SELL"
    outcome: LessonOutcome = "unknown"
    label_source: Optional[str] = None    # "bracket_resolver" | "brain_outcomes" | "synthetic" | None

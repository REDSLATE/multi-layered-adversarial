"""Pydantic models for External Signal Intake v1.

Doctrine frame (TRIAL COURT, NOT A VOTING SYSTEM):
    Pine does not boost.
    Pine does not veto.
    Pine does not persuade.
    Pine only enters the holding cell.

    Verifier decides if Pine ever earns weight.
    Shelly judges whether memory still applies.
    Seat executes.

Authority pin: there is no `may_execute` field, on purpose. The
witness cannot ask for execution authority through this envelope.
The Seat decides — and in v1, the Seat sees these signals as
DIMMED, READ-ONLY context. No click-to-execute path from the
witness panel exists.

Four phases of witness lifecycle (Verifier-owned transitions):

    Phase 1 — New source
        verifier_status: UNTRUSTED
        influence_allowed: False
        Governor modifier: 0.0
        Diagnostic display: "Pine saw BUY on NVDA at 9:31."
        Effect on trading: NONE.

    Phase 2 — Proving period
        Verifier compares signals to later outcomes:
          - Did Pine call BUY before a profitable move?
          - Did Pine disagree with Brain and turn out right?
          - Was Pine just echoing the Brain?
          - Did Pine spam bad signals?
        Still no execution influence.

    Phase 3 — Promotion (only after enough clean samples)
        verifier_status: WATCHLIST → TRUSTED
        influence_allowed: True
        Modifier applies ONLY when thesis is orthogonal — not
        agreement, not hype, only proven different information.

    Phase 4 — Demotion (if witness turns bad)
        TRUSTED → WATCHLIST → UNTRUSTED
        Modifier returns to 0.0.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator


ExternalSignalSource = Literal["pine", "polygon", "public", "mtr"]
ExternalSignalSide = Literal["BUY", "SELL", "HOLD"]
VerifierStatus = Literal["UNTRUSTED", "WATCHLIST", "TRUSTED"]


def build_dedup_key(
    source: str,
    symbol: str,
    timeframe: Optional[str],
    event: Optional[str],
    bar_close_ts: str,
) -> str:
    """Stable idempotency key for an external signal.

    Doctrine: deterministic from witness-supplied fields only. No
    server timestamps. Retries from TradingView produce the same key
    and hit the unique index, refusing the duplicate insert.

    `bar_close_ts` is REQUIRED — fallback to `received_at` would
    make retries look like new signals, breaking idempotency.
    """
    return f"{source}:{symbol.upper()}:{timeframe or '-'}:{event or '-'}:{bar_close_ts}"


class PineWebhookPayload(BaseModel):
    """Raw payload contract for TradingView Pine alerts.

    Operator must template Pine alerts with these fields. Witnesses
    that omit `bar_close_ts` are rejected at the route layer with
    HTTP 400 — no server-side fallback (doctrine: fallbacks weaken
    idempotency).

    NOTE: `grade` and `score` are SELF-REPORTED — Pine grading
    itself. RISEDUAL does not load-bear on these. They are persisted
    for diagnostic display and for Verifier to later judge whether
    Pine's self-grading correlates with realized outcomes.
    """
    v: int = Field(default=2, description="schema version")
    event: str = Field(description="entry | exit | alert | …")
    symbol: str = Field(min_length=1)
    tf: Optional[str] = Field(default=None, description="timeframe label, e.g. '15', '1h'")
    dir: Literal["long", "short", "flat"] = Field(description="signed direction from Pine")
    grade: Optional[str] = Field(default=None, description="self-reported A+ / A / B / C / …")
    score: Optional[float] = Field(default=None, description="self-reported raw Pine score")
    price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    reason: Optional[str] = None

    # REQUIRED. Pine alert template must include `"bar_close_ts": "{{time}}"`.
    # Validated at the route layer; here it's optional only so the
    # 400 path can return a clean error message instead of a Pydantic 422.
    bar_close_ts: Optional[str] = Field(
        default=None,
        description="ISO datetime of the bar close — REQUIRED for idempotency",
    )

    @field_validator("symbol")
    @classmethod
    def _upper_symbol(cls, v: str) -> str:
        return v.strip().upper()


class ExternalSignal(BaseModel):
    """Canonical witness alert row, written to the `external_signals`
    holding cell. Built by the route handler from a
    `PineWebhookPayload` plus the self-reported-confidence mapping
    in `scoring.py`.

    Two doctrine fields enforce default-hostile behavior:

      `verifier_status` — defaults to UNTRUSTED. Only Verifier may
        transition through WATCHLIST → TRUSTED (and back).

      `influence_allowed` — defaults to False. Governor short-circuits
        to a 0.0 modifier on every signal where this is False, no
        matter what the self-reported confidence says.

    `self_reported_confidence` is what Pine claims about itself.
    RISEDUAL does NOT act on it. The field is persisted so Verifier
    can later judge whether Pine's self-grading correlates with
    realized P&L — the trial court's evidence, not the verdict.
    """
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    source: ExternalSignalSource
    symbol: str
    side: ExternalSignalSide
    # Pine's self-graded confidence. ADVISORY ONLY. Not load-bearing.
    self_reported_confidence: float = Field(ge=0.0, le=1.0)
    timeframe: Optional[str] = None
    event: Optional[str] = None
    price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    reason: Optional[str] = None
    raw: dict[str, Any]
    bar_close_ts: str
    dedup_key: str

    # Default-hostile doctrine. Verifier owns transitions of these
    # two fields. The webhook MUST write the defaults; only the
    # Verifier promotion pathway (future) may upgrade them.
    verifier_status: VerifierStatus = "UNTRUSTED"
    influence_allowed: bool = False

    # v1 inline lifecycle (kept for the Seat's read-only panel; the
    # Seat does not write these in v1 because witnesses don't
    # influence Seat decisions at all yet).
    processed_by_seat: bool = False
    seat_decision: Optional[str] = None
    applied_modifier: Optional[float] = None
    received_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
    )

    @field_validator("symbol")
    @classmethod
    def _upper_symbol(cls, v: str) -> str:
        return v.strip().upper()


class ExternalSourceCredibility(BaseModel):
    """Verifier-owned ledger row — one document per witness source.

    The trial court's case file. Updated by Verifier after observed
    outcomes. The witness webhook is allowed to `$setOnInsert` a
    fresh UNTRUSTED row on first sight of a new source (so the case
    file exists from witness #1), but the webhook MUST NOT mutate
    any field on subsequent writes — that would let a hostile
    witness silently re-set itself back to UNTRUSTED after Verifier
    promotes it, defeating the ledger.

    Promotion thresholds (operator-tunable later, documented here
    so the design is accountable to itself):

      Phase 1 → 2  (UNTRUSTED → WATCHLIST):
        samples ≥ 50 AND orthogonal_win_rate > 0.50

      Phase 2 → 3  (WATCHLIST → TRUSTED):
        samples ≥ 200 AND verified_alpha > +0.02

      Phase 3 → 2  (TRUSTED → WATCHLIST, demotion):
        rolling 30-day verified_alpha < 0  OR
        10 consecutive losing trades

      Phase 2 → 1  (WATCHLIST → UNTRUSTED, full demotion):
        90-day verified_alpha < -0.02  OR
        manipulation flag raised by RoadGuard
    """
    source: ExternalSignalSource
    status: VerifierStatus = "UNTRUSTED"
    verified_alpha: float = 0.0           # rolling alpha attributed to this witness
    samples: int = 0                       # total observed signals against resolved outcomes
    wins: int = 0
    losses: int = 0
    avg_return_bps: float = 0.0
    max_drawdown_bps: float = 0.0
    agreement_rate: float = 0.0            # fraction where Pine agreed with Brain
    orthogonal_win_rate: float = 0.0       # win rate WHEN thesis was orthogonal
    last_promoted_at: Optional[str] = None
    last_demoted_at: Optional[str] = None
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
    )
    updated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
    )

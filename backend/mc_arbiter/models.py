"""MC Seat Arbiter — domain primitives.

Frozen dataclasses only. No I/O, no side effects, no Mongo. This is
the shape that flows across the module boundary.

Design freeze: `/app/memory/MC_SEAT_ARBITER.md` §3, §4, §9.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Direction(str, Enum):
    """Direction a brain expresses on a seat.

    FLAT is a legitimate opinion (brain looked and passed) — it's
    graded but never wins a seat. Rationale: grading FLATs closes
    the selection-bias loop the DAWE spec identified. A brain that
    correctly passes on a chop day should get credit; one that
    consistently FLATs into strong moves should have its session
    weight drop.
    """
    LONG = "LONG"
    SHORT = "SHORT"
    FLAT = "FLAT"


class OpinionStatus(str, Enum):
    """Why a brain emitted THIS opinion.

    Operator directive (2026-02, parity work): a fail-closed HOLD
    must be distinguishable from a Camino-confidently-chose-HOLD.
    Previously both surfaced as `direction=FLAT, confidence=1.0`
    which made the parity dashboard read "brain 100% agrees on
    HOLD" when the truth was "brain never got the inputs it
    needed and defaulted to HOLD."

    OK — normal evaluation, opinion reflects real analysis.
    INSUFFICIENT_DATA — required feature(s) missing from snapshot;
        the opinion should be treated as no-signal, NOT as a
        confident HOLD. confidence MUST be 0.0.
    BRAIN_ERROR — brain evaluate() raised; containment wrapped it;
        confidence 0.0.
    """
    OK = "OK"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    BRAIN_ERROR = "BRAIN_ERROR"


class RuntimeMode(str, Enum):
    """Only two modes. No PAPER. No SHADOW. See design freeze §9.

    DISARMED — arbitration runs, DAWE grades update, NO intent
               emitted to the trader. Learning still improves.
    LIVE     — same, but intent IS emitted. Kill switch and
               mechanical validators still gate the emission.
    """
    DISARMED = "DISARMED"
    LIVE = "LIVE"


@dataclass(frozen=True)
class ModelOpinion:
    """A single brain's expression for a seat.

    Field constraints are enforced at the route boundary
    (`mc_arbiter/routes.py`) — the dataclass itself is trusting so
    tests can construct edge cases directly. Frozen so an opinion
    is immutable once emitted — the tape stays honest.

    See design freeze §3 for the full contract.
    """
    brain: str
    seat_key: str
    direction: Direction
    edge: float                   # 0.0-1.0 expected R:R normalized
    confidence: float             # 0.0-1.0 brain's certainty
    regime_fit: float             # 0.4-1.0 brain's regime alignment
    urgency: float                # 0.0-1.0 time-decay of the opportunity
    price_at_signal: float
    ts: str                       # ISO-8601 UTC, brain's emission time
    entry_hint: Optional[float] = None
    stop_hint: Optional[float] = None
    rationale: str = ""
    # ── Parity work (2026-02, operator directive) ──
    # A confident HOLD from a brain that got real inputs is very
    # different from a HOLD emitted because required fields were
    # absent. `status` + `reason_codes` make that distinction
    # first-class on the tape so the parity endpoint can strip
    # `INSUFFICIENT_DATA` rows out of the "action match" score.
    status: str = "OK"                # OpinionStatus enum value
    reason_codes: tuple[str, ...] = ()

    @property
    def rank_score(self) -> float:
        """rank = edge × confidence × regime_fit × (0.75 + urgency × 0.25)

        Matches v3 cognition layer exactly (see risedual_v3_live_only.py).
        Urgency contributes a bounded 0.75→1.00 multiplier so a stale
        signal is dampened but never zeroed out — Bruce Lee bound.

        FLAT direction MUST still produce a rank_score (it's used for
        grading), but the arbitration loop in `arbiter.py` never
        picks a FLAT as winner.
        """
        return (
            self.edge
            * self.confidence
            * self.regime_fit
            * (0.75 + self.urgency * 0.25)
        )


@dataclass
class DaweState:
    """Per-(brain, lane) weight state. Mutable — the grader loop
    updates `session_weight` / `recent_weight` in place via EWMA.

    Stored under
    `brain_runtime_metrics.risedual_stack.brains.<brain>.dawe.<lane>`
    — one doc read gives the whole matrix (design freeze §10).

    Cold-start guard: `effective_weight` returns 1.0 (fully neutral)
    when `grades_used_session < 5`. This prevents a brain's arm from
    being moved by thin data. See design freeze §4.
    """
    brain: str
    lane: str
    session_weight: float = 1.0
    recent_weight: float = 1.0
    prior_weight: float = 1.0
    grades_used_session: int = 0
    grades_used_recent: int = 0
    last_updated: str = ""

    def to_mongo(self) -> dict:
        """Serialize for `brain_runtime_metrics.risedual_stack`
        embedding. Keys mirror the top-level DAWE contract in
        design freeze §10."""
        return {
            "session": self.session_weight,
            "recent": self.recent_weight,
            "prior": self.prior_weight,
            "grades_used_session": self.grades_used_session,
            "grades_used_recent": self.grades_used_recent,
            "last_updated": self.last_updated,
        }

    @classmethod
    def from_mongo(cls, brain: str, lane: str, doc: Optional[dict]) -> "DaweState":
        """Materialize from the embedded stack subdoc. Missing
        fields fall back to neutral defaults — a fresh (brain,
        lane) pair has never been graded and correctly reads as
        cold-start."""
        d = doc or {}
        return cls(
            brain=brain,
            lane=lane,
            session_weight=float(d.get("session", 1.0)),
            recent_weight=float(d.get("recent", 1.0)),
            prior_weight=float(d.get("prior", 1.0)),
            grades_used_session=int(d.get("grades_used_session", 0)),
            grades_used_recent=int(d.get("grades_used_recent", 0)),
            last_updated=str(d.get("last_updated", "") or ""),
        )


@dataclass
class SeatDoc:
    """Row in the `mc_seats` collection.

    One doc per seat_key. Holds every opinion submitted to that
    seat plus the arbiter's decision + resulting intent_id. TTL 30d.

    `winner` is None when arbitration produced no directional
    opinion (all FLAT, or DISARMED mode consumed the decision).
    """
    seat_key: str
    lane: str
    symbol: str
    bucket_iso: str
    opinions: list = field(default_factory=list)   # list[ModelOpinion.__dict__]
    winner: Optional[dict] = None                  # {brain, adjusted_rank, size_mult, intent_id}
    arbitrated_at: Optional[str] = None
    runtime_mode: str = RuntimeMode.DISARMED.value

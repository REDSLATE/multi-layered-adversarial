"""OpinionEnvelope — MC's attribution layer around a brain's opinion.

Brains return `ModelOpinion` (raw view). MC wraps it in an envelope
carrying `pulse_id`, `brain_id`, `seat_key`, `snapshot_id`, and
`evaluated_at`. The envelope is what gets persisted to `mc_seats`
— the brain never touches those fields.

Design freeze: `MC_PULSE.md` §3, §5.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from mc_arbiter.models import ModelOpinion


@dataclass(frozen=True, slots=True)
class OpinionEnvelope:
    """Ready-to-persist wrapper. Frozen so the audit trail can't
    be rewritten mid-flight."""
    pulse_id: str
    brain_id: str
    seat_key: str
    snapshot_id: str
    opinion: ModelOpinion
    evaluated_at: datetime

    def to_mongo(self) -> dict:
        """Flatten for `mc_seats` upsert. The idempotency
        contract lives in the composite unique index on
        (pulse_id, brain_id, symbol, lane) — see
        `db.ensure_indexes` (iter-27 entry)."""
        op = self.opinion
        lane, symbol, bucket = _lane_symbol_bucket_from_key(self.seat_key)
        return {
            "pulse_id": self.pulse_id,
            "brain": self.brain_id,
            "seat_key": self.seat_key,
            "snapshot_id": self.snapshot_id,
            "lane": lane,
            "symbol": symbol,
            "bucket_iso": bucket,
            "direction": op.direction.value,
            "edge": op.edge,
            "confidence": op.confidence,
            "regime_fit": op.regime_fit,
            "urgency": op.urgency,
            "rank_score": op.rank_score,
            "price_at_signal": op.price_at_signal,
            "entry_hint": op.entry_hint,
            "stop_hint": op.stop_hint,
            "rationale": op.rationale,
            "ts": op.ts,
            "evaluated_at": self.evaluated_at.isoformat(),
            # Grader fills these after 15m / 60m:
            "grade_15m": None,
            "grade_60m": None,
        }


def _lane_symbol_bucket_from_key(seat_key: str) -> tuple[str, str, str]:
    # Local import to avoid a circular boot dependency between
    # mc_pulse.envelope ↔ mc_arbiter.seat_key. The arbiter is
    # allowed to import from mc_pulse; the reverse chain must
    # stay short.
    from mc_arbiter.seat_key import parse_seat_key
    return parse_seat_key(seat_key)

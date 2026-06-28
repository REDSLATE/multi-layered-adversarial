"""External Signal Intake v1 — Pine / TradeLens / MTR witnesses.

Doctrine layering:
    Pine / MTR / TradeLens = witnesses (raw alerts; no authority)
    Brains                  = advisors (read witnesses; form opinions)
    Memory                  = evidence under Shelly (recall, not command)
    Shelly                  = judge (trustworthy / promote / demote)
    Governor                = risk modifier (sizing only, asymmetric)
    Seat                    = execution authority (binary go / defer)
    RoadGuard               = hard stop (independent of witnesses)

Witnesses never instruct the Seat directly. They land in the
`external_signals` collection, the Governor optionally applies a
small modifier (asymmetric: +0.10 cap on agreement, -0.15 cap on
disagreement), and the Seat reads but does not obey.

v1 inline lifecycle: one signal → one row. Decision fields
(`processed_by_seat`, `seat_decision`, `applied_modifier`) live
on the signal row itself. If a single signal ever needs to be
evaluated against multiple intents, promote to
`external_signal_decisions` per the original spec.
"""
from .models import (
    ExternalSignal,
    ExternalSignalSide,
    ExternalSignalSource,
    PineWebhookPayload,
    build_dedup_key,
)
from .scoring import (
    GRADE_TO_CONFIDENCE,
    SCORE_BONUS_CAP,
    UNKNOWN_GRADE_FLOOR,
    grade_score_to_confidence,
    pine_dir_to_side,
)

__all__ = [
    "ExternalSignal",
    "ExternalSignalSide",
    "ExternalSignalSource",
    "PineWebhookPayload",
    "build_dedup_key",
    "GRADE_TO_CONFIDENCE",
    "SCORE_BONUS_CAP",
    "UNKNOWN_GRADE_FLOOR",
    "grade_score_to_confidence",
    "pine_dir_to_side",
]

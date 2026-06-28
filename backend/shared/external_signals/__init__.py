"""External Signal Intake v1 — Pine / TradeLens / MTR witnesses.

Doctrine frame (TRIAL COURT, NOT A VOTING SYSTEM):
    Pine does not boost. Pine does not veto. Pine does not persuade.
    Pine only enters the holding cell.

    Verifier decides if Pine ever earns weight.
    Shelly judges whether memory still applies.
    Seat executes.

Architecture:
    Pine / MTR / TradeLens = witnesses (default-hostile until Verifier
                                        promotes; advisory only)
    Brains                  = advisors (independent of witnesses)
    Memory                  = evidence under Shelly (recall, not command)
    Shelly                  = judge (trustworthy / promote / demote)
    Governor                = sizing (0.0 modifier for any witness
                                       where influence_allowed=False)
    Seat                    = execution authority (witnesses are
                                                   read-only dimmed
                                                   context, no
                                                   click-to-execute)
    RoadGuard               = hard stop (independent of witnesses;
                                          plus manipulation cluster)
"""
from .models import (
    ExternalSignal,
    ExternalSignalSide,
    ExternalSignalSource,
    ExternalSourceCredibility,
    PineWebhookPayload,
    VerifierStatus,
    build_dedup_key,
)
from .scoring import (
    GRADE_TO_CONFIDENCE,
    SCORE_BONUS_CAP,
    UNKNOWN_GRADE_FLOOR,
    pine_dir_to_side,
    pine_self_reported_confidence,
)

__all__ = [
    "ExternalSignal",
    "ExternalSignalSide",
    "ExternalSignalSource",
    "ExternalSourceCredibility",
    "PineWebhookPayload",
    "VerifierStatus",
    "build_dedup_key",
    "GRADE_TO_CONFIDENCE",
    "SCORE_BONUS_CAP",
    "UNKNOWN_GRADE_FLOOR",
    "pine_dir_to_side",
    "pine_self_reported_confidence",
]

"""External Signal Intake v1 — Pine / Polygon / Public / MTR witnesses.

Doctrine frame (TRIAL COURT, NOT A VOTING SYSTEM):
    Pine does not boost. Pine does not veto. Pine does not persuade.
    No witness does. Each only enters the holding cell.

    Verifier decides if any source ever earns weight.
    Shelly judges whether memory still applies.
    Seat executes.

Architecture:
    Polygon/Massive  = market truth + news/sentiment witness (poller)
    Public           = broker-quote / account / preflight witness
    Pine             = technical-pattern witness (TradingView webhook,
                                                   on hold)
    MTR              = research-only witness (no live trading path)
    Webull           = execution broker (NOT a witness — different layer)

    Brains           = advisors (independent of witnesses)
    Memory           = evidence under Shelly (recall, not command)
    Shelly           = judge (trustworthy / promote / demote)
    Governor         = sizing (0.0 modifier for any witness where
                                influence_allowed=False)
    Seat             = execution authority (witnesses are read-only
                                             dimmed context, no
                                             click-to-execute)
    RoadGuard        = hard stop (independent of witnesses; plus
                                   manipulation cluster)
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

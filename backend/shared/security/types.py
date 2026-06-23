"""
RISEDUAL Shared Intent Types

Defines the canonical structure for an intent object that flows through
the pipeline: Brain → Mythos Defense → Seat → Governor → RoadGuard → Broker.
"""

from typing import Optional, Literal, Any, Dict
from dataclasses import dataclass, field


# Valid trading actions
TradingAction = Literal["BUY", "SELL", "HOLD", "ABSTAIN"]

# Valid pipeline lanes
PipelineLane = Literal["alpha", "beta", "gamma", "omega"]


@dataclass
class MythosIntent:
    """
    The canonical intent object emitted by a Brain/Agent and passed through
    the Mythos Defense Layer before reaching Seat.

    Fields
    ------
    action          : One of BUY, SELL, HOLD, ABSTAIN. No other values are
                      permitted by the Mythos Defense Layer.
    brain_id        : Unique identifier of the emitting Brain. Must be non-empty.
    signed_source   : Runtime signature proving the intent originated from a
                      trusted, registered Brain process.
    lane            : Pipeline lane the intent is routed through.
    symbol          : Ticker or instrument identifier.
    quantity        : Requested quantity. Must be positive for BUY/SELL.
    confidence      : Brain confidence score in [0.0, 1.0].
    research_ts     : ISO-8601 timestamp of the most recent research evidence
                      used to produce this intent. Used for staleness checks.
    memory_write    : Optional payload for memory writes. Inspected for
                      suspicious content by the Mythos Defense Layer.
    broker_directive: MUST always be None. Any non-None value is an
                      unauthorized broker control attempt and will be blocked.
    metadata        : Arbitrary key-value metadata. Inspected for secrets.
    """
    action: TradingAction
    brain_id: str
    signed_source: str
    lane: PipelineLane
    symbol: str
    quantity: float
    confidence: float
    research_ts: Optional[str] = None
    memory_write: Optional[Dict[str, Any]] = None
    broker_directive: None = None          # MUST always be None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action,
            "brain_id": self.brain_id,
            "signed_source": self.signed_source,
            "lane": self.lane,
            "symbol": self.symbol,
            "quantity": self.quantity,
            "confidence": self.confidence,
            "research_ts": self.research_ts,
            "memory_write": self.memory_write,
            "broker_directive": self.broker_directive,
            "metadata": self.metadata,
        }


@dataclass
class MythosReceipt:
    """
    Stamped receipt produced by the Mythos Defense Layer for every intent,
    whether allowed or blocked.

    Fields
    ------
    allowed             : Whether the intent is cleared to proceed to Seat.
    reason              : Machine-readable reason code.
    security_multiplier : 1.0 if cleared; 0.0 if blocked.
    restriction_source  : Always "security" for Mythos-generated receipts.
    security_layer      : Always "mythos_defense".
    broker_called       : Always False — Mythos never calls the broker.
    intent_snapshot     : Sanitised copy of the original intent for audit.
    """
    allowed: bool
    reason: str
    security_multiplier: float
    restriction_source: str = "security"
    security_layer: str = "mythos_defense"
    broker_called: bool = False
    intent_snapshot: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "security_multiplier": self.security_multiplier,
            "restriction_source": self.restriction_source,
            "security_layer": self.security_layer,
            "broker_called": self.broker_called,
            "intent_snapshot": self.intent_snapshot,
        }

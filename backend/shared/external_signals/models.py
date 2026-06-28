"""Pydantic models for External Signal Intake v1.

Witness layer schema. Authority pin: there is no `may_execute`
field, on purpose. The witness cannot ask for execution authority
through this envelope; the Seat decides what to do with the alert
downstream.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator


ExternalSignalSource = Literal["pine", "tradelens", "mtr"]
ExternalSignalSide = Literal["BUY", "SELL", "HOLD"]


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
    """
    v: int = Field(default=2, description="schema version")
    event: str = Field(description="entry | exit | alert | …")
    symbol: str = Field(min_length=1)
    tf: Optional[str] = Field(default=None, description="timeframe label, e.g. '15', '1h'")
    dir: Literal["long", "short", "flat"] = Field(description="signed direction from Pine")
    grade: Optional[str] = Field(default=None, description="A+ / A / B / C / …")
    score: Optional[float] = Field(default=None, description="raw Pine score")
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
    collection. Built by the route handler from a `PineWebhookPayload`
    plus the `scoring.py` confidence map.
    """
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    source: ExternalSignalSource
    symbol: str
    side: ExternalSignalSide
    confidence: float = Field(ge=0.0, le=1.0)
    timeframe: Optional[str] = None
    event: Optional[str] = None
    price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    reason: Optional[str] = None
    raw: dict[str, Any]
    bar_close_ts: str
    dedup_key: str
    # v1 inline lifecycle
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

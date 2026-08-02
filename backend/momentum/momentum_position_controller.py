"""
Momentum entry + fixed bracket exit controller for RISEDUAL.

Doctrine:
- Existing brains, Seat, Governor, RoadGuard, risk, allowlist, sizing, and broker
  gates remain authoritative for NEW entries.
- This controller may emit a BUY intent only when momentum has transitioned into
  a confirmed positive state.
- After the broker confirms a fill, exits are managed from broker average fill:
      take profit = +5%
      stop loss   = -3%
- Exit requests are reduce-only and must not be blocked by entry-only gates.
- Thresholds are triggers, not guaranteed fill prices.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from enum import Enum
from typing import Any, Mapping, Optional, Protocol
import hashlib


D = Decimal


class AssetClass(str, Enum):
    EQUITY = "equity"
    CRYPTO = "crypto"


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class ExitReason(str, Enum):
    TAKE_PROFIT_5_PCT = "take_profit_5_pct"
    STOP_LOSS_3_PCT = "stop_loss_3_pct"


@dataclass(frozen=True)
class MomentumSnapshot:
    symbol: str
    asset_class: AssetClass
    last_price: Decimal
    previous_score: Decimal
    current_score: Decimal
    price_above_vwap: bool
    price_above_ema9: bool
    acceleration_positive: bool
    spread_bps: Decimal
    quote_age_ms: int
    relative_volume: Decimal
    confirmation_price: Decimal
    observed_at: datetime


@dataclass(frozen=True)
class EntryPolicy:
    min_score: Decimal = D("0.60")
    min_score_delta: Decimal = D("0.08")
    max_spread_bps_equity: Decimal = D("75")
    max_spread_bps_crypto: Decimal = D("250")
    max_quote_age_ms: int = 2500
    min_relative_volume: Decimal = D("1.20")
    max_chase_pct: Decimal = D("0.0075")  # 0.75% above confirmation
    require_vwap: bool = True
    require_ema9: bool = True


@dataclass(frozen=True)
class ExitPolicy:
    take_profit_pct: Decimal = D("0.05")
    stop_loss_pct: Decimal = D("0.03")


@dataclass(frozen=True)
class Position:
    position_id: str
    symbol: str
    asset_class: AssetClass
    quantity: Decimal
    average_fill_price: Decimal
    strategy: str
    opened_at: datetime
    is_open: bool = True


@dataclass(frozen=True)
class OrderRequest:
    client_order_id: str
    symbol: str
    side: Side
    quantity: Decimal
    order_type: str
    time_in_force: str
    reduce_only: bool
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EntryDecision:
    allowed: bool
    reason: str
    intent_payload: Optional[dict[str, Any]] = None


@dataclass(frozen=True)
class ExitDecision:
    should_exit: bool
    reason: Optional[ExitReason] = None
    trigger_price: Optional[Decimal] = None
    order: Optional[OrderRequest] = None


class Broker(Protocol):
    async def submit_order(self, order: OrderRequest) -> Mapping[str, Any]:
        ...

    async def get_last_price(
        self, symbol: str, asset_class: AssetClass
    ) -> Decimal:
        ...


class ExitOrderStore(Protocol):
    async def has_active_exit(self, position_id: str) -> bool:
        ...

    async def record_exit_submission(
        self,
        *,
        position_id: str,
        order: OrderRequest,
        broker_response: Mapping[str, Any],
        reason: ExitReason,
        trigger_price: Decimal,
        observed_price: Decimal,
    ) -> None:
        ...


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _pct_change(current: Decimal, reference: Decimal) -> Decimal:
    if reference <= 0:
        raise ValueError("reference price must be positive")
    return (current - reference) / reference


def _stable_id(*parts: str, max_length: int = 36) -> str:
    raw = "|".join(parts).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:max_length]


def round_to_tick(
    price: Decimal,
    tick_size: Decimal,
    *,
    round_up: bool,
) -> Decimal:
    if tick_size <= 0:
        raise ValueError("tick_size must be positive")
    units = price / tick_size
    rounding = ROUND_UP if round_up else ROUND_DOWN
    return units.to_integral_value(rounding=rounding) * tick_size


def valid_momentum_entry(
    snapshot: MomentumSnapshot,
    policy: EntryPolicy,
) -> EntryDecision:
    """
    Advisory/execution decision only. Existing RISEDUAL entry gates must still
    validate the returned intent before any broker order is placed.
    """
    if snapshot.last_price <= 0 or snapshot.confirmation_price <= 0:
        return EntryDecision(False, "invalid_price")

    if snapshot.current_score < policy.min_score:
        return EntryDecision(False, "momentum_score_below_minimum")

    score_delta = snapshot.current_score - snapshot.previous_score
    if score_delta < policy.min_score_delta:
        return EntryDecision(False, "momentum_not_accelerating")

    if not snapshot.acceleration_positive:
        return EntryDecision(False, "price_acceleration_not_positive")

    if policy.require_vwap and not snapshot.price_above_vwap:
        return EntryDecision(False, "price_below_vwap")

    if policy.require_ema9 and not snapshot.price_above_ema9:
        return EntryDecision(False, "price_below_ema9")

    spread_limit = (
        policy.max_spread_bps_equity
        if snapshot.asset_class == AssetClass.EQUITY
        else policy.max_spread_bps_crypto
    )
    if snapshot.spread_bps > spread_limit:
        return EntryDecision(False, "spread_too_wide")

    if snapshot.quote_age_ms > policy.max_quote_age_ms:
        return EntryDecision(False, "quote_stale")

    if snapshot.relative_volume < policy.min_relative_volume:
        return EntryDecision(False, "relative_volume_too_low")

    chase_pct = _pct_change(
        snapshot.last_price,
        snapshot.confirmation_price,
    )
    if chase_pct > policy.max_chase_pct:
        return EntryDecision(False, "maximum_chase_exceeded")

    intent_id = _stable_id(
        "momentum-entry",
        snapshot.symbol,
        snapshot.observed_at.isoformat(),
        str(snapshot.confirmation_price),
    )

    return EntryDecision(
        True,
        "momentum_entry_confirmed",
        {
            "intent_id": intent_id,
            "source": "momentum_entry_controller",
            "symbol": snapshot.symbol,
            "asset_class": snapshot.asset_class.value,
            "action": Side.BUY.value,
            "confirmation_price": str(snapshot.confirmation_price),
            "snapshot": {
                "last_price": str(snapshot.last_price),
                "previous_score": str(snapshot.previous_score),
                "current_score": str(snapshot.current_score),
                "price_above_vwap": snapshot.price_above_vwap,
                "price_above_ema9": snapshot.price_above_ema9,
                "acceleration_positive": snapshot.acceleration_positive,
                "spread_bps": str(snapshot.spread_bps),
                "quote_age_ms": snapshot.quote_age_ms,
                "relative_volume": str(snapshot.relative_volume),
                "observed_at": snapshot.observed_at.isoformat(),
            },
            # The normal router/Seat/risk path must size and approve this.
            "requires_standard_entry_gates": True,
        },
    )


def evaluate_fixed_exit(
    *,
    position: Position,
    current_price: Decimal,
    policy: ExitPolicy = ExitPolicy(),
) -> ExitDecision:
    """
    Evaluate a full close against broker-confirmed average fill.

    Profit and loss thresholds are deliberately calculated from average_fill_price,
    never from signal/confirmation/limit price.
    """
    if not position.is_open or position.quantity <= 0:
        return ExitDecision(False)

    if position.average_fill_price <= 0 or current_price <= 0:
        return ExitDecision(False)

    take_profit = position.average_fill_price * (D("1") + policy.take_profit_pct)
    stop_loss = position.average_fill_price * (D("1") - policy.stop_loss_pct)

    if current_price >= take_profit:
        reason = ExitReason.TAKE_PROFIT_5_PCT
        trigger_price = take_profit
    elif current_price <= stop_loss:
        reason = ExitReason.STOP_LOSS_3_PCT
        trigger_price = stop_loss
    else:
        return ExitDecision(False)

    client_order_id = _stable_id(
        "fixed-exit",
        position.position_id,
        reason.value,
    )

    order = OrderRequest(
        client_order_id=client_order_id,
        symbol=position.symbol,
        side=Side.SELL,
        quantity=position.quantity,
        order_type="MARKET",
        time_in_force="DAY" if position.asset_class == AssetClass.EQUITY else "GTC",
        reduce_only=True,
        metadata={
            "position_id": position.position_id,
            "strategy": position.strategy,
            "exit_reason": reason.value,
            "average_fill_price": str(position.average_fill_price),
            "trigger_price": str(trigger_price),
            "observed_price": str(current_price),
            "entry_restrictions_bypass": [
                "buy_allowlist",
                "gain_goal_new_entry_latch",
                "new_entry_lane_toggle",
                "momentum_entry_gate",
            ],
            "risk_reducing_exit": True,
        },
    )

    return ExitDecision(
        should_exit=True,
        reason=reason,
        trigger_price=trigger_price,
        order=order,
    )


class MomentumPositionController:
    def __init__(
        self,
        *,
        broker: Broker,
        exit_store: ExitOrderStore,
        entry_policy: EntryPolicy = EntryPolicy(),
        exit_policy: ExitPolicy = ExitPolicy(),
    ) -> None:
        self._broker = broker
        self._exit_store = exit_store
        self._entry_policy = entry_policy
        self._exit_policy = exit_policy

    def evaluate_entry(self, snapshot: MomentumSnapshot) -> EntryDecision:
        return valid_momentum_entry(snapshot, self._entry_policy)

    async def monitor_position(self, position: Position) -> ExitDecision:
        """
        Fetch a fresh broker price, evaluate thresholds, and submit at most one
        active close order per position.
        """
        if await self._exit_store.has_active_exit(position.position_id):
            return ExitDecision(False)

        current_price = await self._broker.get_last_price(
            position.symbol,
            position.asset_class,
        )
        decision = evaluate_fixed_exit(
            position=position,
            current_price=current_price,
            policy=self._exit_policy,
        )
        if not decision.should_exit or decision.order is None or decision.reason is None:
            return decision

        broker_response = await self._broker.submit_order(decision.order)

        await self._exit_store.record_exit_submission(
            position_id=position.position_id,
            order=decision.order,
            broker_response=broker_response,
            reason=decision.reason,
            trigger_price=decision.trigger_price or current_price,
            observed_price=current_price,
        )
        return decision

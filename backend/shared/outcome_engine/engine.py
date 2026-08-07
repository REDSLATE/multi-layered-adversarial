"""RISEDUAL Signal Outcome + Triple Barrier Attribution Engine.

Post-signal outcome/attribution layer (2026-08 operator directive,
design by operator). NOT a brain, NOT in the broker hot path. Freezes
the original signal, applies triple-barrier outcomes to the tape the
signal saw, compares theoretical edge vs actual execution, and assigns
responsibility: signal vs execution. Pure logic — no I/O here.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional, Sequence


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class BarrierOutcome(str, Enum):
    PROFIT = "PROFIT"
    STOP = "STOP"
    TIME = "TIME"
    UNKNOWN = "UNKNOWN"


class Attribution(str, Enum):
    GOOD_COMPLETE_TRADE = "GOOD_COMPLETE_TRADE"
    BAD_SIGNAL = "BAD_SIGNAL"
    GOOD_SIGNAL_LATE_ENTRY = "GOOD_SIGNAL_LATE_ENTRY"
    GOOD_SIGNAL_GATE_REJECTED = "GOOD_SIGNAL_GATE_REJECTED"
    GOOD_SIGNAL_NOT_EXECUTED = "GOOD_SIGNAL_NOT_EXECUTED"
    GOOD_ENTRY_BAD_EXIT = "GOOD_ENTRY_BAD_EXIT"
    EXECUTION_SLIPPAGE = "EXECUTION_SLIPPAGE"
    NO_MEANINGFUL_EDGE = "NO_MEANINGFUL_EDGE"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


@dataclass(frozen=True)
class PricePoint:
    timestamp: datetime
    price: float


@dataclass
class SignalSnapshot:
    """Frozen state of the opportunity WHEN THE BRAIN CREATED IT."""
    signal_id: str
    symbol: str
    lane: str
    brain: str
    side: Side
    signal_time: datetime
    signal_price: float
    confidence: float
    profit_target_pct: float
    stop_loss_pct: float
    max_holding_seconds: int
    regime: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None


@dataclass
class ExecutionSnapshot:
    signal_id: str
    executed: bool
    entry_time: Optional[datetime] = None
    entry_price: Optional[float] = None
    exit_time: Optional[datetime] = None
    exit_price: Optional[float] = None
    gate_rejection_reason: Optional[str] = None
    broker_order_id: Optional[str] = None


@dataclass
class TripleBarrierResult:
    outcome: BarrierOutcome
    barrier_time: Optional[datetime]
    barrier_price: Optional[float]
    profit_barrier: float
    stop_barrier: float
    return_pct: Optional[float]
    time_to_barrier_seconds: Optional[float]
    max_favorable_excursion_pct: Optional[float]
    max_adverse_excursion_pct: Optional[float]


def normalize_timestamp(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def signed_return(side: Side, start_price: float, end_price: float) -> float:
    if start_price <= 0:
        raise ValueError("start price must be > 0")
    raw = (end_price - start_price) / start_price
    return raw if side == Side.BUY else -raw


class TripleBarrierEngine:

    @staticmethod
    def barrier_prices(signal: SignalSnapshot,
                       reference_price: Optional[float] = None) -> tuple[float, float]:
        price = reference_price or signal.signal_price
        if signal.side == Side.BUY:
            return (price * (1 + signal.profit_target_pct),
                    price * (1 - signal.stop_loss_pct))
        return (price * (1 - signal.profit_target_pct),
                price * (1 + signal.stop_loss_pct))

    @classmethod
    def evaluate(cls, signal: SignalSnapshot, prices: Sequence[PricePoint],
                 reference_price: Optional[float] = None,
                 reference_time: Optional[datetime] = None) -> TripleBarrierResult:
        start_price = reference_price or signal.signal_price
        start_time = normalize_timestamp(reference_time or signal.signal_time)
        profit_barrier, stop_barrier = cls.barrier_prices(signal, start_price)
        expiry_ts = start_time.timestamp() + signal.max_holding_seconds

        valid: list[PricePoint] = []
        for point in sorted(prices, key=lambda p: p.timestamp):
            ts = normalize_timestamp(point.timestamp)
            if ts < start_time:
                continue
            if ts.timestamp() > expiry_ts:
                break
            valid.append(PricePoint(timestamp=ts, price=float(point.price)))

        if not valid:
            return TripleBarrierResult(
                outcome=BarrierOutcome.UNKNOWN, barrier_time=None,
                barrier_price=None, profit_barrier=profit_barrier,
                stop_barrier=stop_barrier, return_pct=None,
                time_to_barrier_seconds=None,
                max_favorable_excursion_pct=None,
                max_adverse_excursion_pct=None)

        returns = [signed_return(signal.side, start_price, p.price) for p in valid]
        mfe, mae = max(returns), min(returns)

        def _result(outcome: BarrierOutcome, point: PricePoint) -> TripleBarrierResult:
            return TripleBarrierResult(
                outcome=outcome, barrier_time=point.timestamp,
                barrier_price=point.price, profit_barrier=profit_barrier,
                stop_barrier=stop_barrier,
                return_pct=signed_return(signal.side, start_price, point.price),
                time_to_barrier_seconds=(point.timestamp - start_time).total_seconds(),
                max_favorable_excursion_pct=mfe,
                max_adverse_excursion_pct=mae)

        for point in valid:
            if signal.side == Side.BUY:
                hit_profit = point.price >= profit_barrier
                hit_stop = point.price <= stop_barrier
            else:
                hit_profit = point.price <= profit_barrier
                hit_stop = point.price >= stop_barrier
            # stop checked FIRST — conservative when both hit in one bar
            if hit_stop:
                return _result(BarrierOutcome.STOP, point)
            if hit_profit:
                return _result(BarrierOutcome.PROFIT, point)

        return _result(BarrierOutcome.TIME, valid[-1])


class ExecutionAttributionEngine:

    def __init__(self, minimum_edge_pct: float = 0.0025,
                 late_entry_slippage_pct: float = 0.01,
                 good_capture_ratio: float = 0.60):
        self.minimum_edge_pct = minimum_edge_pct
        self.late_entry_slippage_pct = late_entry_slippage_pct
        self.good_capture_ratio = good_capture_ratio

    def calculate_actual_return(self, signal: SignalSnapshot,
                                execution: ExecutionSnapshot) -> Optional[float]:
        if (not execution.executed or execution.entry_price is None
                or execution.exit_price is None):
            return None
        return signed_return(signal.side, execution.entry_price,
                             execution.exit_price)

    def calculate_entry_slippage(self, signal: SignalSnapshot,
                                 execution: ExecutionSnapshot) -> Optional[float]:
        if not execution.executed or execution.entry_price is None:
            return None
        # positive = adverse entry vs signal price (paid up for BUY)
        return signed_return(signal.side, signal.signal_price,
                             execution.entry_price)

    def calculate_entry_delay(self, signal: SignalSnapshot,
                              execution: ExecutionSnapshot) -> Optional[float]:
        if not execution.entry_time:
            return None
        return (normalize_timestamp(execution.entry_time)
                - normalize_timestamp(signal.signal_time)).total_seconds()

    def calculate_edge_capture_ratio(self, theoretical_return: Optional[float],
                                     actual_return: Optional[float]) -> Optional[float]:
        if theoretical_return is None or actual_return is None:
            return None
        if theoretical_return <= 0:
            return None
        return actual_return / theoretical_return

    def classify(self, signal: SignalSnapshot, execution: ExecutionSnapshot,
                 theoretical: TripleBarrierResult) -> Attribution:
        theoretical_return = theoretical.return_pct

        if theoretical.outcome == BarrierOutcome.UNKNOWN:
            return Attribution.INSUFFICIENT_DATA
        if theoretical.outcome == BarrierOutcome.STOP:
            return Attribution.BAD_SIGNAL
        if (theoretical.outcome == BarrierOutcome.TIME
                and (theoretical_return is None
                     or abs(theoretical_return) < self.minimum_edge_pct)):
            return Attribution.NO_MEANINGFUL_EDGE

        if not execution.executed and execution.gate_rejection_reason:
            return Attribution.GOOD_SIGNAL_GATE_REJECTED
        if not execution.executed:
            return Attribution.GOOD_SIGNAL_NOT_EXECUTED

        actual_return = self.calculate_actual_return(signal, execution)
        slippage = self.calculate_entry_slippage(signal, execution)
        capture_ratio = self.calculate_edge_capture_ratio(
            theoretical_return, actual_return)

        if slippage is not None and slippage > self.late_entry_slippage_pct:
            return Attribution.GOOD_SIGNAL_LATE_ENTRY
        if (theoretical_return is not None and theoretical_return > 0
                and actual_return is not None and actual_return <= 0):
            return Attribution.GOOD_ENTRY_BAD_EXIT
        if capture_ratio is not None and capture_ratio < self.good_capture_ratio:
            return Attribution.EXECUTION_SLIPPAGE
        if actual_return is not None and actual_return > 0:
            return Attribution.GOOD_COMPLETE_TRADE
        return Attribution.NO_MEANINGFUL_EDGE

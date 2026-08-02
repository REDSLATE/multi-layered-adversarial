from datetime import datetime, timezone
from decimal import Decimal as D

import pytest

from momentum.momentum_position_controller import (
    AssetClass,
    EntryPolicy,
    ExitReason,
    MomentumPositionController,
    MomentumSnapshot,
    Position,
    evaluate_fixed_exit,
    valid_momentum_entry,
)


def make_snapshot(**overrides):
    data = dict(
        symbol="NVDA",
        asset_class=AssetClass.EQUITY,
        last_price=D("100.50"),
        previous_score=D("0.51"),
        current_score=D("0.65"),
        price_above_vwap=True,
        price_above_ema9=True,
        acceleration_positive=True,
        spread_bps=D("12"),
        quote_age_ms=300,
        relative_volume=D("1.8"),
        confirmation_price=D("100.00"),
        observed_at=datetime.now(timezone.utc),
    )
    data.update(overrides)
    return MomentumSnapshot(**data)


def make_position(**overrides):
    data = dict(
        position_id="pos-1",
        symbol="NVDA",
        asset_class=AssetClass.EQUITY,
        quantity=D("2.5"),
        average_fill_price=D("100"),
        strategy="momentum_entry_controller",
        opened_at=datetime.now(timezone.utc),
        is_open=True,
    )
    data.update(overrides)
    return Position(**data)


def test_valid_entry_emits_standard_pipeline_intent():
    decision = valid_momentum_entry(make_snapshot(), EntryPolicy())
    assert decision.allowed is True
    assert decision.intent_payload["action"] == "BUY"
    assert decision.intent_payload["requires_standard_entry_gates"] is True


def test_entry_blocks_chasing():
    decision = valid_momentum_entry(
        make_snapshot(last_price=D("101.00")),
        EntryPolicy(max_chase_pct=D("0.0075")),
    )
    assert decision.allowed is False
    assert decision.reason == "maximum_chase_exceeded"


def test_entry_requires_accelerating_score():
    decision = valid_momentum_entry(
        make_snapshot(previous_score=D("0.60"), current_score=D("0.65")),
        EntryPolicy(min_score_delta=D("0.08")),
    )
    assert decision.allowed is False
    assert decision.reason == "momentum_not_accelerating"


def test_take_profit_at_exactly_five_percent():
    decision = evaluate_fixed_exit(
        position=make_position(),
        current_price=D("105"),
    )
    assert decision.should_exit is True
    assert decision.reason == ExitReason.TAKE_PROFIT_5_PCT
    assert decision.order.reduce_only is True
    assert decision.order.quantity == D("2.5")


def test_stop_loss_at_exactly_three_percent():
    decision = evaluate_fixed_exit(
        position=make_position(),
        current_price=D("97"),
    )
    assert decision.should_exit is True
    assert decision.reason == ExitReason.STOP_LOSS_3_PCT


def test_no_exit_inside_band():
    decision = evaluate_fixed_exit(
        position=make_position(),
        current_price=D("101.25"),
    )
    assert decision.should_exit is False


def test_threshold_uses_average_fill_not_confirmation_price():
    position = make_position(average_fill_price=D("102"))
    decision = evaluate_fixed_exit(
        position=position,
        current_price=D("107.10"),
    )
    assert decision.should_exit is True
    assert decision.reason == ExitReason.TAKE_PROFIT_5_PCT
    assert decision.trigger_price == D("107.10")


class FakeBroker:
    def __init__(self, price):
        self.price = D(str(price))
        self.orders = []

    async def get_last_price(self, symbol, asset_class):
        return self.price

    async def submit_order(self, order):
        self.orders.append(order)
        return {"status": "accepted", "broker_order_id": "abc"}


class FakeStore:
    def __init__(self):
        self.active = False
        self.records = []

    async def has_active_exit(self, position_id):
        return self.active

    async def record_exit_submission(self, **kwargs):
        self.active = True
        self.records.append(kwargs)


@pytest.mark.asyncio
async def test_monitor_submits_only_one_active_exit():
    broker = FakeBroker("105")
    store = FakeStore()
    controller = MomentumPositionController(
        broker=broker,
        exit_store=store,
    )

    first = await controller.monitor_position(make_position())
    second = await controller.monitor_position(make_position())

    assert first.should_exit is True
    assert second.should_exit is False
    assert len(broker.orders) == 1
    assert len(store.records) == 1

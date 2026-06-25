"""Paradox v3 — Step 2 lesson + report-card lifter adoption.

Pins:
  * `build_lesson()` lifts v2 + v3 intent docs uniformly.
  * `Lesson` carries the plan/execution fields surfaced by the lifter.
  * `build_report_card()` adds a `plan_discipline` axis.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from db import db
from namespaces import SHARED_INTENTS
from shared.lessons.builder import build_lesson
from shared.report_cards import _summarize_plan_discipline, build_report_card
from shared.lessons.schemas import Lesson


pytestmark = pytest.mark.asyncio


async def _seed(intent_doc: dict) -> str:
    iid = intent_doc.setdefault("intent_id", f"t-{uuid.uuid4().hex[:12]}")
    intent_doc.setdefault("ingest_ts", datetime.now(timezone.utc).isoformat())
    intent_doc.setdefault("executed", False)
    await db[SHARED_INTENTS].insert_one(intent_doc.copy())
    return iid


async def _cleanup(iids: list[str]) -> None:
    if iids:
        await db[SHARED_INTENTS].delete_many({"intent_id": {"$in": iids}})


# ── v2 lesson still works ──────────────────────────────────────────
async def test_v2_lesson_lifts_to_v3_shape():
    iid = await _seed({
        "stack": "camino", "lane": "equity", "symbol": "AAPL",
        "action": "BUY", "confidence": 0.7, "rationale": "test",
        "evidence": {}, "snapshot": {},
    })
    try:
        lesson = await build_lesson(iid)
        assert lesson is not None
        assert lesson.action == "BUY"
        # v2 doc → lifter synthesised plan ENTER/BULLISH
        assert lesson.intent_version == "v2"
        assert lesson.plan_intent == "ENTER"
        assert lesson.plan_stance == "BULLISH"
        assert lesson.plan_execution_style == "MARKET_NOW"
        assert lesson.execution_action == "BUY"
        # Legacy v2 emits flagged false per PRD §3.2.
        assert lesson.execution_derived_from_plan is False
    finally:
        await _cleanup([iid])


async def test_hold_v2_lesson_lifts_to_watch_with_null_action():
    iid = await _seed({
        "stack": "camino", "lane": "equity", "symbol": "AAPL",
        "action": "HOLD", "confidence": 0.3, "rationale": "wait",
    })
    try:
        lesson = await build_lesson(iid)
        assert lesson is not None
        assert lesson.action == "HOLD"
        assert lesson.plan_intent == "WATCH"
        assert lesson.plan_stance == "NEUTRAL"
        assert lesson.execution_action is None
    finally:
        await _cleanup([iid])


async def test_v3_lesson_passes_through_full_plan():
    iid = await _seed({
        "stack": "camino", "lane": "equity", "symbol": "NVDA",
        "action": "HOLD", "confidence": 0.81, "rationale": "wait for breakout",
        "intent_version": "v3",
        "plan": {
            "stance": "BULLISH", "setup": "bull_flag",
            "intent": "WAIT_FOR_TRIGGER",
            "execution_style": "TRIGGERED_LIMIT",
            "size_posture": "STANDARD",
            "portfolio_posture": "NEUTRAL",
            "confidence": 0.81,
            "trigger_price": 187.40,
            "invalidation_price": 184.20,
            "target_prices": [189.00, 191.50],
            "horizon": "INTRADAY",
            "thesis": "intraday bull flag",
        },
        "execution": {"action": None, "derived_from_plan": True},
    })
    try:
        lesson = await build_lesson(iid)
        assert lesson is not None
        assert lesson.intent_version == "v3"
        assert lesson.plan_intent == "WAIT_FOR_TRIGGER"
        assert lesson.plan_setup == "bull_flag"
        assert lesson.plan_trigger_price == 187.40
        assert lesson.plan_invalidation_price == 184.20
        assert lesson.plan_target_prices == [189.00, 191.50]
        assert lesson.plan_horizon == "INTRADAY"
        assert lesson.execution_action is None
        assert lesson.execution_derived_from_plan is True
    finally:
        await _cleanup([iid])


async def test_lesson_dataclass_carries_all_v3_fields():
    """Schema contract — guarantees the dataclass has every v3
    surface the report-card aggregator expects."""
    expected_v3 = {
        "intent_version",
        "plan_stance",
        "plan_intent",
        "plan_setup",
        "plan_execution_style",
        "plan_size_posture",
        "plan_portfolio_posture",
        "plan_confidence",
        "plan_horizon",
        "plan_trigger_price",
        "plan_invalidation_price",
        "plan_target_prices",
        "plan_ttl_seconds",
        "plan_setup_custom_tag",
        "plan_hedge_against_symbol",
        "execution_action",
        "execution_derived_from_plan",
    }
    fields_present = {f.name for f in Lesson.__dataclass_fields__.values()}
    missing = expected_v3 - fields_present
    assert not missing, f"Lesson dataclass missing v3 fields: {sorted(missing)}"


# ── plan_discipline axis ──────────────────────────────────────────
def _mk_lesson(**kw) -> Lesson:
    defaults = dict(
        intent_id="x", stack="camino", lane="equity", symbol="AAPL",
        action="BUY", confidence=0.5,
    )
    defaults.update(kw)
    return Lesson(**defaults)


def test_plan_discipline_empty_when_no_v3_lessons():
    lessons = [_mk_lesson(intent_version="v2", plan_intent="ENTER")]
    out = _summarize_plan_discipline(lessons)
    assert out["v3_lesson_count"] == 0
    assert out["v2_legacy_count"] == 1
    assert out["by_plan_intent"] == {}
    assert out["wait_correct_rate"] is None


def test_plan_discipline_counts_v3_lessons():
    lessons = [
        _mk_lesson(intent_version="v3", plan_intent="ENTER", plan_stance="BULLISH", plan_setup="breakout"),
        _mk_lesson(intent_version="v3", plan_intent="ENTER", plan_stance="BULLISH", plan_setup="bull_flag"),
        _mk_lesson(intent_version="v3", plan_intent="WAIT_FOR_TRIGGER", plan_stance="BULLISH", plan_setup="bull_flag"),
        _mk_lesson(intent_version="v3", plan_intent="WAIT_FOR_TRIGGER", plan_stance="BEARISH", plan_setup="breakdown"),
        _mk_lesson(intent_version="v3", plan_intent="WATCH", plan_stance="NEUTRAL", plan_setup="other"),
        _mk_lesson(intent_version="v2", plan_intent="ENTER"),  # legacy, excluded
    ]
    out = _summarize_plan_discipline(lessons)
    assert out["v3_lesson_count"] == 5
    assert out["v2_legacy_count"] == 1
    assert out["by_plan_intent"]["ENTER"] == 2
    assert out["by_plan_intent"]["WAIT_FOR_TRIGGER"] == 2
    assert out["by_plan_intent"]["WATCH"] == 1
    assert out["by_plan_stance"]["BULLISH"] == 3
    assert out["by_plan_setup"]["bull_flag"] == 2
    assert out["wait_plans_observed"] == 2
    # Step 5 fields stay None until trigger_watcher is live.
    assert out["wait_correct_rate"] is None
    assert out["trigger_hit_rate"] is None
    assert out["invalidation_hit_rate"] is None


async def test_build_report_card_includes_plan_discipline():
    """The aggregator surfaces the new axis without breaking legacy
    consumers reading `overall`/`by_setup`/`by_regime`."""
    iid_a = await _seed({
        "stack": "camino-rc-test", "lane": "equity", "symbol": "AAPL",
        "action": "BUY", "confidence": 0.7, "rationale": "test",
    })
    iid_b = await _seed({
        "stack": "camino-rc-test", "lane": "equity", "symbol": "MSFT",
        "action": "HOLD", "confidence": 0.4, "rationale": "test",
        "intent_version": "v3",
        "plan": {
            "stance": "BULLISH", "setup": "breakout",
            "intent": "ENTER", "execution_style": "MARKET_NOW",
            "confidence": 0.4,
        },
    })
    try:
        card = await build_report_card(stack="camino-rc-test", limit=10)
        assert "plan_discipline" in card
        pd = card["plan_discipline"]
        assert pd["v3_lesson_count"] >= 1
        assert pd["v2_legacy_count"] >= 1
        # Legacy keys must still be present so the existing UI doesn't
        # break when this patch deploys.
        assert "overall" in card
        assert "by_setup" in card
        assert "by_regime" in card
    finally:
        await _cleanup([iid_a, iid_b])

"""Paradox v2 — seat-owned execution doctrine tests.

These pin the IP boundaries between brain (doctrine), seat (capital +
trust), governor (modifiers), roadguard (binary), and verifier
(promotion). Any test that crosses a boundary is a doctrine violation
and should be rejected at code review.
"""
from __future__ import annotations

import asyncio
import uuid
import pytest


pytestmark = pytest.mark.asyncio


# ─── seed idempotence ────────────────────────────────────────────────


async def test_seed_is_idempotent():
    from shared.paradox_v2.seed import seed_paradox_v2

    r1 = await seed_paradox_v2()
    r2 = await seed_paradox_v2()
    assert r1["ok"] is True
    assert r2["ok"] is True
    # Re-running must produce zero new upserts.
    for k in ("brain_registry", "seat_policy_config", "seat_trusted_brains",
              "governor_modifier_rules"):
        assert r2["seeded"][k] == 0, f"{k} should be idempotent on re-seed; got {r2['seeded'][k]}"


async def test_seed_creates_four_canonical_brains():
    from db import db
    from namespaces import PARADOX_V2_BRAIN_REGISTRY
    from shared.paradox_v2.seed import seed_paradox_v2

    await seed_paradox_v2()
    brains = await db[PARADOX_V2_BRAIN_REGISTRY].find({}, {"_id": 0}).to_list(50)
    ids = {b["brain_id"] for b in brains}
    assert {"alpha", "camaro", "chevelle", "redeye"} <= ids
    display = {b["brain_id"]: b["display_name"] for b in brains}
    assert display["alpha"] == "Camino"
    assert display["camaro"] == "Barracuda"
    assert display["chevelle"] == "Hellcat"
    assert display["redeye"] == "GTO"


async def test_seed_equity_executor_trusts_alpha_only():
    from db import db
    from namespaces import PARADOX_V2_SEAT_TRUSTED
    from shared.paradox_v2.seed import seed_paradox_v2

    await seed_paradox_v2()
    trusts = await db[PARADOX_V2_SEAT_TRUSTED].find({"seat_id": "equity_executor"}, {"_id": 0}).to_list(50)
    brain_ids = {t["brain_id"] for t in trusts}
    assert brain_ids == {"alpha"}


async def test_seed_crypto_executor_starts_with_no_trust():
    """Paradox v2 doctrine: crypto seat starts vacant of trust.
    Restrictions belong to the seat, never to a hardcoded brain default."""
    from db import db
    from namespaces import PARADOX_V2_SEAT_TRUSTED
    from shared.paradox_v2.seed import seed_paradox_v2

    await seed_paradox_v2()
    trusts = await db[PARADOX_V2_SEAT_TRUSTED].find({"seat_id": "crypto_executor"}, {"_id": 0}).to_list(50)
    assert len(trusts) == 0


async def test_seed_crypto_executor_starts_in_observe_mode():
    from db import db
    from namespaces import PARADOX_V2_SEAT_POLICY
    from shared.paradox_v2.seed import seed_paradox_v2

    await seed_paradox_v2()
    p = await db[PARADOX_V2_SEAT_POLICY].find_one({"seat_id": "crypto_executor"}, {"_id": 0})
    assert p is not None
    assert p["autonomy_mode"] == "observe"


# ─── Stage 1: SEAT POLICY ────────────────────────────────────────────


async def _seed():
    from shared.paradox_v2.seed import seed_paradox_v2
    await seed_paradox_v2()


def _opinion(**over):
    base = {
        "brain_id": "alpha",
        "symbol": "AAPL",
        "lane": "equity",
        "action": "BUY",
        "confidence": 0.90,
        "suggested_notional_usd": 1_000.0,
        "evidence": {},
        "emitted_at": "2026-02-19T00:00:00+00:00",
    }
    base.update(over)
    return base


async def test_seat_rejects_unknown_seat():
    await _seed()
    from shared.paradox_v2.evaluator import evaluate
    r = await evaluate(_opinion(), seat_id="not_a_real_seat")
    assert r["decision"] == "REJECTED_SEAT"
    assert "unknown_seat" in r["reason"]


async def test_seat_rejects_untrusted_brain():
    await _seed()
    from shared.paradox_v2.evaluator import evaluate
    r = await evaluate(_opinion(brain_id="camaro"), seat_id="equity_executor")
    assert r["decision"] == "REJECTED_SEAT"
    assert "brain_not_trusted" in r["reason"]


async def test_seat_rejects_low_confidence():
    await _seed()
    from shared.paradox_v2.evaluator import evaluate
    r = await evaluate(_opinion(confidence=0.50), seat_id="equity_executor")
    assert r["decision"] == "REJECTED_SEAT"
    assert "confidence_below_floor" in r["reason"]


async def test_seat_rejects_notional_over_cap():
    await _seed()
    from shared.paradox_v2.evaluator import evaluate
    # equity_executor has max_notional_usd=5000 in seed.
    r = await evaluate(_opinion(suggested_notional_usd=10_000.0), seat_id="equity_executor")
    assert r["decision"] == "REJECTED_SEAT"
    assert "notional_exceeds_seat_cap" in r["reason"]


# ─── Stage 2: GOVERNOR ───────────────────────────────────────────────


async def test_governor_wide_spread_compounds_size_multiplier():
    await _seed()
    from shared.paradox_v2.evaluator import evaluate
    # alpha trusted on equity_executor, confidence 0.9, notional 1000.
    # seed seat_size_mult = 0.50. wide_spread (>=6.7) governor mult = 0.50.
    # Final = 1000 * 0.50 * 0.50 = 250.
    r = await evaluate(
        _opinion(evidence={"spread_bps": 10.0}),
        seat_id="equity_executor",
    )
    assert r["decision"] == "EXECUTED"
    assert r["final_notional_usd"] == 250.0
    assert any("wide_spread" in rid["rule_id"] for rid in r["pipeline_trace"]["governor"]["applied_rules"])


async def test_governor_earnings_window_flags_vote_required():
    await _seed()
    from shared.paradox_v2.evaluator import evaluate
    r = await evaluate(
        _opinion(evidence={"earnings_within_days": 2}),
        seat_id="equity_executor",
    )
    assert r["decision"] == "PENDING_VOTE"
    rules = r["pipeline_trace"]["governor"]["applied_rules"]
    assert any(rule["trigger_type"] == "earnings_window" for rule in rules)


async def test_governor_unknown_evidence_does_not_fire():
    await _seed()
    from shared.paradox_v2.evaluator import evaluate
    # Evidence keys the governor doesn't understand must NOT trigger
    # any rule and must NOT change the final size.
    r = await evaluate(
        _opinion(evidence={"some_random_signal": True, "another": "thing"}),
        seat_id="equity_executor",
    )
    assert r["decision"] == "EXECUTED"
    # seat_size_mult 0.5 * gov 1.0 * 1000 = 500
    assert r["final_notional_usd"] == 500.0
    assert r["pipeline_trace"]["governor"]["applied_rules"] == []


# ─── Stage 3: ROADGUARD ──────────────────────────────────────────────


async def test_roadguard_active_stop_blocks_execution():
    await _seed()
    from db import db
    from namespaces import PARADOX_V2_ROADGUARD_STOPS
    from shared.paradox_v2.evaluator import evaluate

    sid = str(uuid.uuid4())
    await db[PARADOX_V2_ROADGUARD_STOPS].insert_one({
        "stop_id": sid,
        "seat_id": "equity_executor",
        "is_active": True,
        "reason": "test stop — daily loss limit breached",
        "triggered_by": "test",
        "created_at": "2026-02-19T00:00:00+00:00",
        "cleared_at": None,
        "cleared_by": None,
    })
    try:
        r = await evaluate(_opinion(), seat_id="equity_executor")
        assert r["decision"] == "REJECTED_ROADGUARD"
        assert "daily loss limit" in r["reason"]
    finally:
        await db[PARADOX_V2_ROADGUARD_STOPS].delete_one({"stop_id": sid})


async def test_roadguard_cleared_stop_does_not_block():
    await _seed()
    from db import db
    from namespaces import PARADOX_V2_ROADGUARD_STOPS
    from shared.paradox_v2.evaluator import evaluate

    sid = str(uuid.uuid4())
    await db[PARADOX_V2_ROADGUARD_STOPS].insert_one({
        "stop_id": sid,
        "seat_id": "equity_executor",
        "is_active": False,
        "reason": "test stop",
        "triggered_by": "test",
        "created_at": "2026-02-19T00:00:00+00:00",
        "cleared_at": "2026-02-19T01:00:00+00:00",
        "cleared_by": "test",
    })
    try:
        r = await evaluate(_opinion(), seat_id="equity_executor")
        assert r["decision"] == "EXECUTED"
    finally:
        await db[PARADOX_V2_ROADGUARD_STOPS].delete_one({"stop_id": sid})


# ─── Stage 4: EXEC DECISION ──────────────────────────────────────────


async def test_observe_mode_blocks_even_when_all_gates_pass():
    """Paradox v2: a seat in observe/shadow logs decisions but never places orders.

    There are no paper trades in this system. observe/shadow mode means
    the seat's verdict is recorded as an EvaluationReceipt with
    decision=BLOCKED and no broker side-effect at all. Toehold and
    auto_execute are the only modes that place live orders."""
    await _seed()
    from db import db
    from namespaces import PARADOX_V2_SEAT_POLICY, PARADOX_V2_SEAT_TRUSTED
    from shared.paradox_v2.evaluator import evaluate

    # Flip equity_executor to observe; ensure alpha still trusted.
    await db[PARADOX_V2_SEAT_POLICY].update_one(
        {"seat_id": "equity_executor"},
        {"$set": {"autonomy_mode": "observe"}},
    )
    try:
        r = await evaluate(_opinion(), seat_id="equity_executor")
        assert r["decision"] == "BLOCKED"
        assert "observe_mode" in r["reason"]
        assert "no order placed" in r["reason"]
    finally:
        await db[PARADOX_V2_SEAT_POLICY].update_one(
            {"seat_id": "equity_executor"},
            {"$set": {"autonomy_mode": "auto_execute"}},
        )


async def test_evaluation_persists_full_receipt():
    await _seed()
    from db import db
    from namespaces import PARADOX_V2_EVALUATIONS
    from shared.paradox_v2.evaluator import evaluate

    r = await evaluate(_opinion(), seat_id="equity_executor")
    stored = await db[PARADOX_V2_EVALUATIONS].find_one(
        {"evaluation_id": r["evaluation_id"]}, {"_id": 0},
    )
    assert stored is not None
    assert stored["decision"] == r["decision"]
    assert stored["pipeline_trace"]["seat_policy"]["pass"] is True
    await db[PARADOX_V2_EVALUATIONS].delete_one({"evaluation_id": r["evaluation_id"]})


# ─── IP boundary tests ───────────────────────────────────────────────


async def test_brain_opinion_has_no_seat_knowledge():
    """A BrainOpinion model must NOT carry seat-side fields like
    seat_id, autonomy_mode, or trust_level. The brain doesn't know
    those exist — that's the IP boundary."""
    from shared.paradox_v2.models import BrainOpinion
    op = BrainOpinion(
        brain_id="alpha", symbol="AAPL", lane="equity",
        action="BUY", confidence=0.9, suggested_notional_usd=1000,
        evidence={"spread_bps": 5},
    )
    d = op.model_dump()
    forbidden = {"seat_id", "autonomy_mode", "trust_level", "max_notional_usd",
                 "size_multiplier", "daily_risk_budget_usd"}
    assert forbidden.isdisjoint(set(d.keys())), \
        f"BrainOpinion leaked seat-side fields: {forbidden & set(d.keys())}"

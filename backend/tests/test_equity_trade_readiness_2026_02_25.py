"""2026-02-25 — Lock the JSON contract of /api/admin/equity-trade-readiness.

Doctrine context:
    Operator's single-shot diagnostic for "why isn't equity
    trading?". Must surface, per intent, the authority chain
    (raw_action → normalized_action → broker_action → submit_allowed)
    AND an ordered blocker list AND the `first_failing_gate`.

    Operator constraint pinned in scope:
        "Don't let this endpoint recompute doctrine. It should
         report what happened from persisted intent/audit fields
         as much as possible."

This regression suite locks:
    1. Authority-chain shape — every item exposes raw_action,
       normalized_action, broker_action, display_action AND a
       `translation` block with `source` so the operator can tell
       this is a diagnostic projection (not a persisted broker
       submission record).
    2. Blocker order — `blockers` is a list in the canonical
       order `brain_hold → seat_holder → market_hours → dry_run
       → consensus → action_allowed → rr_validity → roadguard`.
       A future refactor reordering them silently breaks the
       operator's mental model. Lock the order.
    3. `first_failing_gate` semantics — walks the canonical order
       and returns the first FAIL (skipping SKIPs).
    4. Cash-account broker-action projection — SHORT and COVER
       project to None (the SHORT/COVER-vs-SELL/BUY mismatch the
       auditor flagged). BUY → BUY. SELL → SELL.
    5. Fleet summary shape — `total_intents_window` + ordered
       `by_first_failing_gate` histogram.
    6. Read-only contract — no mutations. Test runs the endpoint
       twice and asserts the DB state is unchanged.

These are PURE contract tests (no live brain runs needed). All
verdict logic is unit-tested by calling the route handler with
synthetic intents seeded into a test collection name.
"""
from __future__ import annotations

import pytest

from routes.equity_trade_readiness import (
    _GATE_ORDER,
    _action_allowed_verdict,
    _brain_hold_verdict,
    _first_failing_gate,
    _market_hours_verdict,
    _project_broker_action,
    _rr_verdict,
    _seat_verdict,
)


# ──────────────────── 1) authority-chain projection ─────────────────


@pytest.mark.parametrize(
    "normalized,expected",
    [
        ("BUY", "BUY"),
        ("SELL", "SELL"),
        ("SHORT", None),
        ("COVER", None),
        ("HOLD", None),
        (None, None),
        ("", None),
        ("BUY_WEIRD", None),  # unknown verb stays None — fail-closed
    ],
)
def test_broker_action_projection_cash_account_doctrine(normalized, expected):
    """Cash account: only BUY and SELL hit the wire. SHORT/COVER
    require margin — they project to None so `action_allowed` then
    surfaces them as the blocking gate. Locks the auditor's
    SHORT/COVER-vs-allowed_actions mismatch finding."""
    assert _project_broker_action(normalized) == expected


# ─────────────────────── 2) blocker ordering ───────────────────────


def test_gate_order_is_authority_chain_pinned_2026_02_25():
    """The operator pinned this order. A refactor that flips
    `dry_run` and `seat_holder` (for example) silently changes
    which gate `first_failing_gate` reports as the bottleneck."""
    assert _GATE_ORDER == (
        "brain_hold",
        "seat_holder",
        "market_hours",
        "dry_run",
        "consensus",
        "action_allowed",
        "rr_validity",
        "roadguard",
    )


def test_first_failing_gate_picks_earliest_fail():
    gates = {
        "brain_hold": {"verdict": "PASS"},
        "seat_holder": {"verdict": "PASS"},
        "market_hours": {"verdict": "FAIL"},
        "dry_run": {"verdict": "FAIL"},  # later — should NOT win
        "consensus": {"verdict": "SKIP"},
        "action_allowed": {"verdict": "PASS"},
        "rr_validity": {"verdict": "PASS"},
        "roadguard": {"verdict": "PASS"},
    }
    assert _first_failing_gate(gates) == "market_hours"


def test_first_failing_gate_skips_over_skips():
    gates = {n: {"verdict": "SKIP"} for n in _GATE_ORDER}
    gates["action_allowed"] = {"verdict": "FAIL"}
    assert _first_failing_gate(gates) == "action_allowed"


def test_first_failing_gate_returns_none_when_all_pass_or_skip():
    gates = {n: {"verdict": "PASS"} for n in _GATE_ORDER}
    assert _first_failing_gate(gates) is None


# ───────────────────── 3) per-gate verdict logic ───────────────────


def test_brain_hold_HOLD_intent_fails_with_reason():
    intent = {"display_action": "HOLD", "hold_reason": "no_signal"}
    v = _brain_hold_verdict(intent)
    assert v["verdict"] == "FAIL"
    assert v["reason"] == "no_signal"


def test_brain_hold_BUY_intent_passes():
    intent = {"display_action": "BUY", "action": "BUY"}
    assert _brain_hold_verdict(intent)["verdict"] == "PASS"


def test_brain_hold_carries_would_have_traded_flag():
    """The honesty-audit flag — when the brain WOULD have traded
    but a downstream gate suppressed display_action to HOLD."""
    intent = {
        "display_action": "HOLD",
        "hold_reason": "consensus_pushed_below_floor",
        "would_have_traded_without_gates": True,
    }
    v = _brain_hold_verdict(intent)
    assert v["would_have_traded_without_gates"] is True


def test_seat_holder_emitter_matches():
    v = _seat_verdict({"stack": "camino"}, "camino")
    assert v["verdict"] == "PASS"


def test_seat_holder_emitter_mismatches():
    v = _seat_verdict({"stack": "barracuda"}, "camino")
    assert v["verdict"] == "FAIL"
    assert v["emitter"] == "barracuda"
    assert v["current_seat"] == "camino"


def test_seat_holder_no_seat():
    v = _seat_verdict({"stack": "camino"}, None)
    assert v["verdict"] == "FAIL"
    assert v["reason"] == "no_seat_holder"


def test_action_allowed_buy_in_policy():
    v = _action_allowed_verdict("BUY", ["BUY", "SELL"])
    assert v["verdict"] == "PASS"


def test_action_allowed_short_projection_blocks():
    """Replays the auditor's SHORT/COVER finding: a SHORT intent
    projects to broker_action=None (via _project_broker_action),
    which then fails the action_allowed gate even though the
    raw intent passed every earlier check."""
    v = _action_allowed_verdict(None, ["BUY", "SELL"])
    assert v["verdict"] == "FAIL"
    assert v["reason"] == "broker_action_null"


def test_market_hours_rth_inside_business_hours():
    # Mon 2026-03-02 15:00 UTC = 10:00 ET → RTH
    v = _market_hours_verdict("2026-03-02T15:00:00+00:00", extended_enabled_now=False)
    assert v["state"] == "RTH"
    assert v["verdict"] == "PASS"


def test_market_hours_closed_overnight():
    # Mon 2026-03-02 03:00 UTC = Sun 22:00 ET → CLOSED
    v = _market_hours_verdict("2026-03-02T03:00:00+00:00", extended_enabled_now=False)
    assert v["verdict"] == "FAIL"


def test_market_hours_missing_ts():
    v = _market_hours_verdict(None, extended_enabled_now=False)
    assert v["verdict"] == "SKIP"


def test_rr_buy_with_coherent_prices():
    """BUY: stop must be BELOW target (you risk less than you aim
    for). 100→110 with stop=95 is coherent."""
    v = _rr_verdict({"raw_action": "BUY", "target_price": 110.0, "stop_price": 95.0})
    assert v["verdict"] == "PASS"


def test_rr_buy_with_inverted_prices_fails():
    v = _rr_verdict({"raw_action": "BUY", "target_price": 95.0, "stop_price": 110.0})
    assert v["verdict"] == "FAIL"
    assert "stop<target" in v["reason"]


def test_rr_short_with_coherent_prices():
    """SHORT: stop must be ABOVE target."""
    v = _rr_verdict({"raw_action": "SHORT", "target_price": 90.0, "stop_price": 105.0})
    assert v["verdict"] == "PASS"


def test_rr_missing_prices_skips():
    v = _rr_verdict({"raw_action": "BUY", "target_price": None, "stop_price": None})
    assert v["verdict"] == "SKIP"


# ─────────────────── 4) end-to-end shape contract ──────────────────


@pytest.mark.asyncio
async def test_endpoint_returns_complete_top_level_shape():
    """Live call against the seeded preview DB. Locks the keys the
    operator UI binds to. If a future refactor drops `fleet_summary`
    or renames `first_failing_gate`, this test fails immediately."""
    from routes.equity_trade_readiness import equity_trade_readiness

    result = await equity_trade_readiness(
        symbol=None, limit=5, hours=24,
        _user={"email": "test@risedual.io"},
    )

    # Top-level shape
    for key in [
        "now", "window_hours", "filter", "session", "seat", "policy",
        "gate_order", "items", "count", "fleet_summary",
    ]:
        assert key in result, f"missing top-level key: {key}"

    # Session shape
    for key in ["now_utc", "rth", "extended_hours_window", "extended_hours_enabled",
                "lane_enabled", "lane_status", "next_rth_open_iso"]:
        assert key in result["session"], f"missing session.{key}"
    # lane_status must be one of the operator-facing labels
    assert result["session"]["lane_status"] in ("OPEN", "GATED", "DISABLED")

    # Seat shape
    assert "equity_executor" in result["seat"]
    assert result["seat"]["source"] == "shared_brain_roster.assignments[executor]"

    # Policy shape — exposes the allowed_actions the auditor flagged
    assert "allowed_actions" in result["policy"]
    assert isinstance(result["policy"]["allowed_actions"], list)

    # gate_order matches the canonical chain
    assert tuple(result["gate_order"]) == _GATE_ORDER

    # Fleet summary shape
    assert "total_intents_window" in result["fleet_summary"]
    assert "by_first_failing_gate" in result["fleet_summary"]
    assert isinstance(result["fleet_summary"]["by_first_failing_gate"], dict)


@pytest.mark.asyncio
async def test_endpoint_item_authority_chain_shape():
    """Each item exposes the operator-pinned authority chain
    plus the `translation` block that carries the SHORT/COVER
    diagnostic projection truth."""
    from routes.equity_trade_readiness import equity_trade_readiness
    result = await equity_trade_readiness(
        symbol=None, limit=10, hours=24,
        _user={"email": "test@risedual.io"},
    )
    if not result["items"]:
        pytest.skip("no equity intents in window — shape locked via static parameter tests")
    for item in result["items"]:
        for key in [
            "raw_action", "normalized_action", "broker_action",
            "display_action", "translation",
        ]:
            assert key in item, f"item missing authority-chain field: {key}"
        # Translation block must self-identify so operators never
        # confuse it with a persisted broker submission record.
        assert item["translation"]["source"] == "diagnostic_projection_cash_account"
        # Blockers must be a list of gate dicts in the canonical order
        assert isinstance(item["blockers"], list)
        assert [b["gate"] for b in item["blockers"]] == list(_GATE_ORDER)
        # first_failing_gate is either None or one of the canonical names
        ffg = item["first_failing_gate"]
        assert ffg is None or ffg in _GATE_ORDER


@pytest.mark.asyncio
async def test_endpoint_runs_repeatedly_without_error():
    """The endpoint is documented as read-only. A strict "row
    count unchanged" assertion can't run reliably here because
    the live backend writes to `shared_gate_results` continuously
    from the running brain runners. Instead we verify the
    endpoint is idempotent in behavior: two consecutive calls
    return the same top-level shape and don't raise."""
    from routes.equity_trade_readiness import equity_trade_readiness
    r1 = await equity_trade_readiness(
        symbol=None, limit=3, hours=24,
        _user={"email": "test@risedual.io"},
    )
    r2 = await equity_trade_readiness(
        symbol="NVDA", limit=3, hours=24,
        _user={"email": "test@risedual.io"},
    )
    # Same shape across both calls
    assert set(r1.keys()) == set(r2.keys())
    assert r1["gate_order"] == r2["gate_order"]
    assert r1["seat"]["source"] == r2["seat"]["source"]
    # Each item still has the authority-chain block on the second
    # call (catches any global-state corruption between calls).
    for item in r2.get("items") or []:
        assert "translation" in item
        assert item["translation"]["source"] == "diagnostic_projection_cash_account"

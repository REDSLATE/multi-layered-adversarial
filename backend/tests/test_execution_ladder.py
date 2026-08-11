"""Execution Recovery Ladder + ATR stops (2026 MC directive —
"Capture the Move, Don't Return to HOLD")."""
from __future__ import annotations

import sys

import pytest

sys.path.insert(0, "/app/backend")

from shared import execution_ladder as lad  # noqa: E402

pytestmark = pytest.mark.tripwire


class FakeAdapter:
    """Scripted adapter: `fill_on_stage` names the ladder stage whose
    order fills; everything before it expires unfilled."""

    def __init__(self, fill_on_stage=None, post_only_reject_stages=()):
        self.fill_on_stage = fill_on_stage
        self.post_only_reject_stages = set(post_only_reject_stages)
        self.submits: list[dict] = []
        self.cancels: list[str] = []
        self._stage_idx = 0

    async def submit_limit_order(self, **kw):
        stage = lad._STAGES[len(self.submits)] if len(self.submits) < 4 else "?"
        self.submits.append({**kw, "stage": stage})
        if stage in self.post_only_reject_stages and kw.get("post_only"):
            raise RuntimeError("EOrder:Post only order")
        return {"order_id": f"TX-{stage}", "status": "submitted",
                "volume_base": kw["qty"], "limit_price": kw["limit_price"]}

    async def get_order(self, order_id):
        stage = order_id.replace("TX-", "")
        if stage == self.fill_on_stage:
            return {"status": "FILLED", "filled_qty": 1.0,
                    "filled_avg_price": 5.0, "filled_at": "now",
                    "raw": {"vol_exec": "1.0"}}
        return {"status": "EXPIRED", "filled_qty": None,
                "filled_avg_price": None, "raw": {"vol_exec": "0"}}

    async def cancel_order(self, order_id):
        self.cancels.append(order_id)
        return {"ok": True}


@pytest.fixture(autouse=True)
def _fast_cfg(monkeypatch):
    async def _cfg():
        return {**lad.DEFAULTS, "stage_wait_s": 0.05, "poll_s": 0.01}
    monkeypatch.setattr(lad, "get_ladder_config", _cfg)

    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(lad, "_record_event", _noop)

    from shared.crypto import broker_adapter as ba
    async def _ba(pair):
        return 100.0, 101.0  # 100bps spread
    monkeypatch.setattr(ba, "_ticker_bid_ask", _ba)


INTENT = {"intent_id": "i1", "symbol": "WIDE/USD", "lane": "crypto",
          "price_at_signal": 100.2}


@pytest.mark.asyncio
async def test_fills_at_first_maker_stage():
    a = FakeAdapter(fill_on_stage="maker_bid")
    order = await lad.run_entry_ladder(
        a, intent=INTENT, broker_symbol="WIDE/USD", notional_usd=5.0,
        mc_receipt={"signature": "s", "mc_policy_hash": "h"})
    assert order["ladder_stage"] == "maker_bid"
    assert order["order_style"] == "recovery_ladder"
    assert a.submits[0]["post_only"] is True
    assert a.submits[0]["limit_price"] == 100.0  # at bid


@pytest.mark.asyncio
async def test_escalates_to_adaptive_then_aggressive_capped():
    a = FakeAdapter(fill_on_stage="aggressive_limit")
    order = await lad.run_entry_ladder(
        a, intent=INTENT, broker_symbol="WIDE/USD", notional_usd=5.0,
        mc_receipt={"signature": "s", "mc_policy_hash": "h"})
    assert order["ladder_stage"] == "aggressive_limit"
    stages = [s["stage"] for s in a.submits]
    assert stages == ["maker_bid", "maker_reprice", "adaptive_maker",
                      "aggressive_limit"]
    # adaptive maker: bid + 25% of spread
    assert a.submits[2]["limit_price"] == pytest.approx(100.25)
    assert a.submits[2]["post_only"] is True
    # aggressive: capped at min(ask, signal*(1+100bps)) — NEVER market
    assert a.submits[3]["post_only"] is False
    assert a.submits[3]["limit_price"] == pytest.approx(101.0)


@pytest.mark.asyncio
async def test_exhausted_ladder_raises_qualified_but_unexecuted():
    a = FakeAdapter(fill_on_stage=None)
    with pytest.raises(lad.LadderUnfilled):
        await lad.run_entry_ladder(
            a, intent=INTENT, broker_symbol="WIDE/USD", notional_usd=5.0,
            mc_receipt={"signature": "s", "mc_policy_hash": "h"})
    assert len(a.submits) == 4


@pytest.mark.asyncio
async def test_price_ran_away_abandons_before_aggressive(monkeypatch):
    from shared.crypto import broker_adapter as ba
    async def _ba(pair):
        return 110.0, 111.0  # book gapped way above the chase cap
    monkeypatch.setattr(ba, "_ticker_bid_ask", _ba)
    a = FakeAdapter(fill_on_stage=None)
    with pytest.raises(lad.LadderUnfilled) as e:
        await lad.run_entry_ladder(
            a, intent=INTENT, broker_symbol="WIDE/USD", notional_usd=5.0,
            mc_receipt={"signature": "s", "mc_policy_hash": "h"})
    assert "price_ran_away" in str(e.value)
    assert len(a.submits) == 3  # aggressive rung never submitted


@pytest.mark.asyncio
async def test_post_only_reject_climbs_to_next_rung():
    a = FakeAdapter(fill_on_stage="maker_reprice",
                    post_only_reject_stages={"maker_bid"})
    order = await lad.run_entry_ladder(
        a, intent=INTENT, broker_symbol="WIDE/USD", notional_usd=5.0,
        mc_receipt={"signature": "s", "mc_policy_hash": "h"})
    assert order["ladder_stage"] == "maker_reprice"


# ─────────────────────── ATR volatility stops ───────────────────────

from shared.risk_sizer import sizer  # noqa: E402


def _bars(entry, atr_pct):
    """15 synthetic bars whose true range = atr_pct of entry."""
    rng = entry * atr_pct
    return [{"ts": f"2026-06-01T00:{i:02d}:00", "h": entry + rng / 2,
             "l": entry - rng / 2, "c": entry} for i in range(15)]


@pytest.mark.asyncio
async def test_atr_stop_scales_and_clamps(monkeypatch):
    async def _atr(sym, entry):
        return 0.04  # 4% ATR → 1.5×4% = 6% stop
    monkeypatch.setattr(sizer, "_atr_fraction", _atr)
    res = await sizer.resolve_canonical_stop(
        {"lane": "crypto", "action": "BUY", "price_at_signal": 100.0}, {})
    assert res["source"] == "ATR_VOL"
    assert res["stop_fraction"] == pytest.approx(0.06)
    assert res["stop_price"] == pytest.approx(94.0)


@pytest.mark.asyncio
async def test_atr_stop_clamped_to_floor_and_ceiling(monkeypatch):
    async def _tiny(sym, entry):
        return 0.001  # 0.1% ATR → clamped up to 3%
    monkeypatch.setattr(sizer, "_atr_fraction", _tiny)
    res = await sizer.resolve_canonical_stop(
        {"lane": "crypto", "action": "BUY", "price_at_signal": 100.0}, {})
    assert res["stop_fraction"] == pytest.approx(0.03)

    async def _huge(sym, entry):
        return 0.10  # 10% ATR → 15% raw, clamped to 8%
    monkeypatch.setattr(sizer, "_atr_fraction", _huge)
    res2 = await sizer.resolve_canonical_stop(
        {"lane": "crypto", "action": "BUY", "price_at_signal": 100.0}, {})
    assert res2["stop_fraction"] == pytest.approx(0.08)


@pytest.mark.asyncio
async def test_no_atr_falls_back_to_exit_policy(monkeypatch):
    async def _none(sym, entry):
        return None
    monkeypatch.setattr(sizer, "_atr_fraction", _none)
    from shared.exits import policy as xp
    async def _pol():
        return {"crypto": {"sl_pct": 3.0}}
    monkeypatch.setattr(xp, "get_policy", _pol)
    res = await sizer.resolve_canonical_stop(
        {"lane": "crypto", "action": "BUY", "price_at_signal": 100.0}, {})
    assert res["source"] == "EXIT_POLICY"
    assert res["stop_fraction"] == pytest.approx(0.03)


def test_router_wiring():
    src = open("/app/backend/shared/broker_router.py").read()
    assert "run_entry_ladder" in src
    assert "qualified_but_unexecuted" in src

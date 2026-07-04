"""Seat doctrine correction — 2026-07-04 council-participant routing.

Doctrine pin: brain identity influences WEIGHT (sizing), it does not
BLOCK execution. Pre-fix behavior returned verdict='pass' when the
emitting brain wasn't strategist/executor for the lane, which stamped
`gate_state='advisory_only'` and starved 50% of emitted intent volume
per lane. Post-fix: non-seat brains route as council participants at
50% of the governor's risk_multiplier — verdict='fire' either way,
only the size differs.

Hard blocks remain: missing action, missing lane, vacant executor seat.
Those still return verdict='pass'.
"""
from __future__ import annotations

import sys
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/backend")


@pytest.fixture()
def crypto_seats():
    """Prod-shape crypto seat layout per the operator screenshot:
    strategist=camino, executor=gto, governor=hellcat, auditor=barracuda."""
    return {
        "strategist": "camino",
        "governor":   "hellcat",
        "executor":   "gto",
        "auditor":    "barracuda",
    }


@pytest.fixture()
def equity_seats():
    """Prod-shape equity seat layout per the operator screenshot:
    strategist=gto, executor=camino, governor=hellcat, auditor=barracuda."""
    return {
        "strategist": "gto",
        "governor":   "hellcat",
        "executor":   "camino",
        "auditor":    "barracuda",
    }


async def _run_decide(intent, seats, mult=1.0):
    """Helper: patch seat.get_lane_seats + get_governor_multiplier and
    call seat.decide(). Isolates the doctrine logic from DB access."""
    from shared import seat
    with patch.object(seat, "get_lane_seats", AsyncMock(return_value=seats)), \
         patch.object(seat, "get_governor_multiplier", AsyncMock(return_value=mult)):
        return await seat.decide(intent)


@pytest.mark.asyncio
async def test_strategist_brain_fires_full_size(crypto_seats):
    """CAMINO emits on crypto (holds strategist seat) → fires at full multiplier."""
    result = await _run_decide(
        {"stack": "camino", "lane": "crypto", "action": "BUY", "symbol": "BTC/USD"},
        crypto_seats, mult=1.0,
    )
    assert result.verdict == "fire"
    assert result.risk_multiplier == 1.0
    assert "strategist_proposes" in result.reason or "executor_self_fires" in result.reason


@pytest.mark.asyncio
async def test_executor_brain_fires_full_size(crypto_seats):
    """GTO emits on crypto (holds executor seat) → fires at full multiplier."""
    result = await _run_decide(
        {"stack": "gto", "lane": "crypto", "action": "BUY", "symbol": "ETH/USD"},
        crypto_seats, mult=1.0,
    )
    assert result.verdict == "fire"
    assert result.risk_multiplier == 1.0


@pytest.mark.asyncio
async def test_non_seat_brain_now_fires_at_half_size_barracuda(crypto_seats):
    """BARRACUDA emits on crypto (holds only auditor, not strategist/executor)
    → PRE-FIX: verdict='pass', advisory_only stamped, no trade.
    → POST-FIX: verdict='fire' at 50% of governor's multiplier."""
    result = await _run_decide(
        {"stack": "barracuda", "lane": "crypto", "action": "SELL", "symbol": "BTC/USD"},
        crypto_seats, mult=1.0,
    )
    assert result.verdict == "fire", (
        f"non-seat brain must FIRE (as council participant), not pass. "
        f"Got verdict={result.verdict!r} reason={result.reason!r}"
    )
    assert result.risk_multiplier == 0.5, (
        f"non-seat brain risk_multiplier must be 50% of governor's, "
        f"got {result.risk_multiplier}"
    )
    assert "council" in result.reason.lower() or "non_seat" in result.reason.lower()


@pytest.mark.asyncio
async def test_non_seat_brain_now_fires_at_half_size_hellcat(crypto_seats):
    """HELLCAT emits on crypto (holds only governor, not strategist/executor)
    → fires at 50% multiplier (council participant)."""
    result = await _run_decide(
        {"stack": "hellcat", "lane": "crypto", "action": "BUY", "symbol": "ETH/USD"},
        crypto_seats, mult=1.0,
    )
    assert result.verdict == "fire"
    assert result.risk_multiplier == 0.5


@pytest.mark.asyncio
async def test_council_participant_dampener_composes_with_governor_dampener(crypto_seats):
    """Governor's own risk_multiplier and the council-participant 50%
    dampener MULTIPLY (compose). E.g., governor at 0.8 * council 0.5 = 0.4."""
    result = await _run_decide(
        {"stack": "barracuda", "lane": "crypto", "action": "SELL", "symbol": "BTC/USD"},
        crypto_seats, mult=0.8,
    )
    assert result.verdict == "fire"
    assert result.risk_multiplier == pytest.approx(0.4)


@pytest.mark.asyncio
async def test_equity_lane_uses_same_council_rule(equity_seats):
    """Same doctrine on equity lane — non-seat brains route at 50%."""
    # BARRACUDA on equity holds only auditor — same as crypto layout
    result = await _run_decide(
        {"stack": "barracuda", "lane": "equity", "action": "BUY", "symbol": "NVDA"},
        equity_seats, mult=1.0,
    )
    assert result.verdict == "fire"
    assert result.risk_multiplier == 0.5


@pytest.mark.asyncio
async def test_vacant_executor_still_hard_blocks(crypto_seats):
    """The one seat-related HARD BLOCK that remains: if the executor
    seat itself is vacant, no fire — this is a real doctrine block
    (someone has to actually execute), not a brain-identity block."""
    seats_no_executor = dict(crypto_seats)
    seats_no_executor["executor"] = None
    result = await _run_decide(
        {"stack": "camino", "lane": "crypto", "action": "BUY", "symbol": "BTC/USD"},
        seats_no_executor, mult=1.0,
    )
    assert result.verdict == "pass"
    assert "executor_seat_vacant" in result.reason


@pytest.mark.asyncio
async def test_non_directional_action_still_hard_blocks(crypto_seats):
    """HOLD/WAIT/etc are non-routable regardless of brain identity."""
    result = await _run_decide(
        {"stack": "camino", "lane": "crypto", "action": "HOLD", "symbol": "BTC/USD"},
        crypto_seats, mult=1.0,
    )
    assert result.verdict == "pass"


@pytest.mark.asyncio
async def test_missing_lane_still_hard_blocks(crypto_seats):
    """No lane → can't route. Still a hard block."""
    result = await _run_decide(
        {"stack": "camino", "action": "BUY", "symbol": "BTC/USD"},
        crypto_seats, mult=1.0,
    )
    assert result.verdict == "pass"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

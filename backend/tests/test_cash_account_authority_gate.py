"""Cash-account safety gate — operator pin 2026-02-23.

"I haven't enabled margin. Don't think I will yet. I want it to trade
 successfully, in the black, first."

The brain-level `<BRAIN>_SHORTS_ENABLED=false` defaults are the FIRST
layer. This gate is the SECOND — a central pipeline rejection of any
SHORT or COVER intent regardless of which path emitted it. Defense in
depth: a legacy sidecar bug, a manual `POST /admin/intents`, or a
future brain that forgets to env-gate its shorts STILL cannot route a
margin trade while `ACCOUNT_TYPE=cash` (the default).
"""
from __future__ import annotations

import sys
import uuid

import pytest

sys.path.insert(0, "/app/backend")

from shared.execution import _evaluate_gates  # noqa: E402


def _intent(action: str) -> dict:
    return {
        "intent_id": f"test-{uuid.uuid4().hex[:8]}",
        "stack": "barracuda",
        "stack_canonical": "barracuda",
        "symbol": "AAPL",
        "lane": "equity",
        "action": action,
        "confidence": 0.6,
        "risk_multiplier": 0.0,
        "may_execute": False,
        "requires_gate_pass": True,
        "target_price": 110.0,
        "stop_price": 95.0,
    }


def _find_gate(result: dict, name: str) -> dict | None:
    for g in result.get("gates", []):
        if g.get("name") == name:
            return g
    return None


@pytest.mark.asyncio
async def test_cash_account_blocks_short(monkeypatch):
    monkeypatch.setenv("ACCOUNT_TYPE", "cash")
    result = await _evaluate_gates(_intent("SHORT"), 100.0)
    cash_gate = _find_gate(result, "cash_account_authority")
    assert cash_gate is not None
    assert cash_gate["passed"] is False
    assert "cash account" in cash_gate["reason"].lower()
    assert "margin" in cash_gate["reason"].lower()
    assert result["verdict"] == "would_block"


@pytest.mark.asyncio
async def test_cash_account_blocks_cover(monkeypatch):
    monkeypatch.setenv("ACCOUNT_TYPE", "cash")
    result = await _evaluate_gates(_intent("COVER"), 100.0)
    cash_gate = _find_gate(result, "cash_account_authority")
    assert cash_gate is not None
    assert cash_gate["passed"] is False


@pytest.mark.asyncio
async def test_cash_account_default_is_cash(monkeypatch):
    """Unset env MUST default to cash (operator pin: safe-by-default)."""
    monkeypatch.delenv("ACCOUNT_TYPE", raising=False)
    result = await _evaluate_gates(_intent("SHORT"), 100.0)
    cash_gate = _find_gate(result, "cash_account_authority")
    assert cash_gate is not None
    assert cash_gate["passed"] is False, (
        "ACCOUNT_TYPE unset must default to cash (no shorts)"
    )


@pytest.mark.asyncio
async def test_cash_account_allows_buy(monkeypatch):
    monkeypatch.setenv("ACCOUNT_TYPE", "cash")
    result = await _evaluate_gates(_intent("BUY"), 100.0)
    cash_gate = _find_gate(result, "cash_account_authority")
    assert cash_gate is not None
    assert cash_gate["passed"] is True


@pytest.mark.asyncio
async def test_cash_account_allows_sell(monkeypatch):
    """SELL of an existing long position is allowed on cash accounts —
    it's just closing inventory, not opening a short."""
    monkeypatch.setenv("ACCOUNT_TYPE", "cash")
    result = await _evaluate_gates(_intent("SELL"), 100.0)
    cash_gate = _find_gate(result, "cash_account_authority")
    assert cash_gate is not None
    assert cash_gate["passed"] is True


@pytest.mark.asyncio
async def test_margin_opt_in_allows_short(monkeypatch):
    """When operator explicitly opts in by setting ACCOUNT_TYPE=margin,
    SHORT/COVER actions pass this gate (they may still be blocked by
    other gates — this gate is just the cash-account safety stop)."""
    monkeypatch.setenv("ACCOUNT_TYPE", "margin")
    result = await _evaluate_gates(_intent("SHORT"), 100.0)
    cash_gate = _find_gate(result, "cash_account_authority")
    assert cash_gate is not None
    assert cash_gate["passed"] is True
    assert "margin" in cash_gate["reason"].lower()


@pytest.mark.asyncio
async def test_cash_gate_skipped_for_hold(monkeypatch):
    """HOLD fails action_routable; the cash gate runs only on routable
    actions, so we don't emit a noisy `cash_account_authority` row for
    every HOLD intent."""
    monkeypatch.setenv("ACCOUNT_TYPE", "cash")
    result = await _evaluate_gates(_intent("HOLD"), 100.0)
    assert _find_gate(result, "cash_account_authority") is None

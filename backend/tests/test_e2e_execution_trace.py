"""E2E execution trace integration test.

Runs the full `mc_pulse.e2e_trace.run_e2e_trace(broker_mock=True)`
pipeline against the real preview DB, with the broker layer stubbed
out. Verifies every expected DB write lands. This is the canonical
end-to-end regression net requested in the "controlled execution
trace" review — a synthesised intent traverses every layer and each
step is asserted independently.

If ANY stage breaks in future, this test tells you EXACTLY which
one — `TraceResult.broke_at` names the last successful stage,
`TraceResult.next_expected` names what should have happened next.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest


@pytest.fixture(autouse=True)
def _stub_entry_timing_gate(monkeypatch):
    """2026-08-01 Entry Timing Gate fails CLOSED without snapshot/
    bars, which this synthetic trace intent lacks. Stub to allow —
    the gate has its own test file (test_entry_timing_gate.py)."""
    import shared.risk_sizer.entry_timing  # noqa: F401,WPS433
    monkeypatch.setattr(
        "shared.risk_sizer.entry_timing.check_buy_entry",
        AsyncMock(return_value={"allowed": True, "reason": "test_stub",
                                "decision": "BUY", "receipt": {}}),
    )


@pytest.mark.asyncio
async def test_e2e_trace_broker_mocked_full_stack():
    """End-to-end: pulse → arbiter → intent → router → broker →
    executions. Broker layer stubbed so no real orders are placed."""
    from mc_pulse.e2e_trace import run_e2e_trace

    result = await run_e2e_trace(
        symbol="AAPL",
        lane="equity",
        broker_mock=True,
        cleanup=True,
    )

    # First check: NO stage crashed with an unexpected exception.
    # Each stage MUST report either `ok=True` or a semantic reason.
    crashed_stages = [
        s for s in result.stages
        if not s.ok and (
            s.error is None
            or "KeyError" in (s.error or "")
            or "AttributeError" in (s.error or "")
            or "TypeError" in (s.error or "")
        )
    ]
    assert not crashed_stages, (
        f"E2E trace had unexpected exceptions:\n"
        + "\n".join(f"  {s.name}: {s.error}" for s in crashed_stages)
    )

    # The trace either completes end-to-end OR stops at a well-defined
    # link with a diagnosis. Print the trace on failure so CI logs
    # show WHERE the stack broke, not just that it broke.
    if not result.ok:
        summary = "\n".join([
            f"  [{'ok' if s.ok else 'FAIL'}] {s.name} "
            f"({s.duration_ms:.1f}ms) "
            f"{'error=' + s.error if s.error else ''}"
            for s in result.stages
        ])
        pytest.fail(
            f"E2E trace did not complete end-to-end.\n"
            f"broke_at={result.broke_at}\n"
            f"next_expected={result.next_expected}\n"
            f"stages:\n{summary}"
        )


@pytest.mark.asyncio
async def test_e2e_trace_refuses_live_broker_without_env_guard():
    """Safety guard: `broker_mock=False` MUST refuse to run unless
    the env var is explicitly set. Prevents accidental live orders
    from a CI or shell invocation."""
    import os
    from mc_pulse.e2e_trace import run_e2e_trace

    # Ensure the guard env var is NOT set for this test.
    prev = os.environ.pop("E2E_TRACE_ALLOW_LIVE_BROKER", None)
    try:
        with pytest.raises(RuntimeError, match="broker_mock=False"):
            await run_e2e_trace(broker_mock=False)
    finally:
        if prev is not None:
            os.environ["E2E_TRACE_ALLOW_LIVE_BROKER"] = prev


@pytest.mark.asyncio
async def test_e2e_trace_stage_names_are_stable():
    """Downstream ops rely on stage names to build alert rules.
    Any rename requires bumping this test (and updating ops runbook)."""
    from mc_pulse.e2e_trace import STAGE_NAMES

    assert STAGE_NAMES == [
        "synthesize_snapshot",
        "run_pulse_writes_opinion",
        "arbitrate_writes_decision",
        "emit_intent",
        "route_one",
        "broker_call",
        "executions_record",
    ]

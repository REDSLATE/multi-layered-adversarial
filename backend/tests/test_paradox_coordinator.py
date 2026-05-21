"""PARADOX coordinator — doctrine lock tests.

Locked invariants:
  * Default state: every agent disabled.
  * No global kill switch — each agent's enable flag is independent.
  * Disabled agents are skipped (state.runs does not increment).
  * Failures are captured into state, never raised.
  * Execute agent does NOT bypass MC — its agent function POSTs to a
    URL that includes `/execution/submit` (the real gated path), not
    a direct broker call.
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

from shared.coordinator.agents import AGENT_FUNCS, run_execute
from shared.coordinator.runner import reset_stop_for_tests, run_agent, run_cycle
from shared.coordinator.state import AGENTS, STATE, snapshot


@pytest.fixture(autouse=True)
def _reset_coordinator_state():
    """Reset the in-memory state before every test."""
    from shared.coordinator.state import AgentState, CoordinatorState, STATE as S
    S.agents.clear()
    S.agents.update({a: AgentState() for a in AGENTS})
    S.cycle_seconds = 300
    S.loop_active = False
    reset_stop_for_tests()
    yield


# ───── default-disabled doctrine ──────────────────────────────────────


@pytest.mark.tripwire
def test_default_state_every_agent_disabled():
    """No agent enables itself at boot. Operator must opt in
    per-agent via `/api/admin/coordinator/enable/{agent}`."""
    snap = snapshot()
    for name in AGENTS:
        assert snap["agents"][name]["enabled"] is False, (
            f"{name} must default to disabled — no auto-enable on boot"
        )


@pytest.mark.tripwire
def test_no_global_kill_switch_constant():
    """There is no module-level flag that flips every agent on or
    off. Each agent must have its own enable bit."""
    import shared.coordinator.state as state_mod
    import shared.coordinator.runner as runner_mod
    forbidden = {
        "RISEDUAL_EXECUTION_ENABLED",  # the Celery pattern
        "COORDINATOR_ENABLED",
        "GLOBAL_KILL",
    }
    for mod in (state_mod, runner_mod):
        for name in dir(mod):
            assert name not in forbidden, (
                f"forbidden global flag found in {mod.__name__}.{name}"
            )


@pytest.mark.tripwire
def test_agent_list_locked():
    assert AGENTS == ("scan", "evaluate", "execute", "risk", "retrain")


@pytest.mark.tripwire
def test_execute_agent_uses_gated_submit_path():
    """The execute agent function must hit a URL that goes through the
    full gated submit chain — NOT a direct broker import. The
    `/execution/submit` substring is the canonical anchor in the live
    endpoint and the coordinator's `execute-next` proxy."""
    src = inspect.getsource(run_execute)
    assert (
        "/api/admin/paradox/execute-next" in src
        or "/api/execution/submit" in src
    ), (
        "execute agent must POST to a gated submit endpoint, never "
        "direct-import broker code"
    )
    # And the function MUST NOT contain a bypass import — broker code
    # is never to be touched directly from this layer.
    assert "from execution import" not in src
    assert "execute_trades" not in src


# ───── runner behavior ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_disabled_agent_is_skipped(monkeypatch):
    called = []
    async def fake(): called.append(1); return {"ok": True}
    monkeypatch.setitem(AGENT_FUNCS, "scan", fake)

    out = await run_agent("scan")
    assert out["skipped"] is True
    assert out["reason"] == "disabled"
    assert STATE.agents["scan"].runs == 0
    assert called == []


@pytest.mark.asyncio
async def test_enabled_agent_runs_and_stamps_state(monkeypatch):
    STATE.agents["scan"].enabled = True

    async def fake(): return {"ok": True, "stub": True, "pipeline": {"pending": 0}}
    monkeypatch.setitem(AGENT_FUNCS, "scan", fake)

    out = await run_agent("scan")
    assert out["ok"] is True
    st = STATE.agents["scan"]
    assert st.runs == 1
    assert st.last_ok is True
    assert st.last_error is None
    assert st.last_result_summary == "stub"


@pytest.mark.asyncio
async def test_agent_failure_is_captured_not_raised(monkeypatch):
    STATE.agents["risk"].enabled = True

    async def boom(): raise RuntimeError("upstream down")
    monkeypatch.setitem(AGENT_FUNCS, "risk", boom)

    out = await run_agent("risk")
    assert out["ok"] is False
    assert "upstream down" in out["error"]
    st = STATE.agents["risk"]
    assert st.last_ok is False
    assert st.last_error == "upstream down"
    assert st.failures == 1


@pytest.mark.asyncio
async def test_unknown_agent_skipped_not_raised():
    out = await run_agent("ghost")
    assert out["skipped"] is True
    assert out["reason"] == "unknown_agent"


@pytest.mark.asyncio
async def test_concurrent_runs_blocked(monkeypatch):
    STATE.agents["scan"].enabled = True

    started = asyncio.Event()
    release = asyncio.Event()
    async def slow():
        started.set()
        await release.wait()
        return {"ok": True}
    monkeypatch.setitem(AGENT_FUNCS, "scan", slow)

    first = asyncio.create_task(run_agent("scan"))
    await started.wait()
    second = await run_agent("scan")
    assert second["skipped"] is True
    assert second["reason"] == "already_running"
    release.set()
    await first


@pytest.mark.asyncio
async def test_run_cycle_runs_enabled_agents_in_parallel(monkeypatch):
    """All 5 agents fire in parallel via asyncio.gather."""
    for name in AGENTS:
        STATE.agents[name].enabled = True

    order = []
    started_at = asyncio.Event()
    async def fake_factory(name):
        async def fn():
            order.append(name)
            started_at.set()
            await asyncio.sleep(0.01)
            return {"ok": True}
        return fn

    for name in AGENTS:
        monkeypatch.setitem(AGENT_FUNCS, name, await fake_factory(name))

    results = await run_cycle()
    assert len(results) == len(AGENTS)
    # All 5 names appeared (parallel order can vary)
    assert set(order) == set(AGENTS)
    for r in results:
        assert r["ok"] is True


@pytest.mark.asyncio
async def test_disabled_agents_skipped_inside_cycle(monkeypatch):
    """Only enabled agents actually call their function."""
    STATE.agents["scan"].enabled = True
    STATE.agents["execute"].enabled = False  # explicit off

    calls = {"scan": 0, "execute": 0}
    async def make(name):
        async def fn():
            calls[name] += 1
            return {"ok": True}
        return fn

    monkeypatch.setitem(AGENT_FUNCS, "scan", await make("scan"))
    monkeypatch.setitem(AGENT_FUNCS, "execute", await make("execute"))

    await run_cycle()
    assert calls["scan"] == 1
    assert calls["execute"] == 0  # disabled stays at 0


# ───── HTTP self-call shape ───────────────────────────────────────────


@pytest.mark.tripwire
def test_internal_api_base_points_at_8001_by_default():
    """The backend runs on 0.0.0.0:8001 in this environment.
    The coordinator's default API base MUST match."""
    import os
    # Force a fresh import to read the module-level constant correctly
    # under the current env.
    prev = os.environ.pop("RISEDUAL_INTERNAL_API_BASE", None)
    try:
        import importlib
        import shared.coordinator.agents as ag
        importlib.reload(ag)
        assert ag._API_BASE == "http://127.0.0.1:8001"
    finally:
        if prev is not None:
            os.environ["RISEDUAL_INTERNAL_API_BASE"] = prev

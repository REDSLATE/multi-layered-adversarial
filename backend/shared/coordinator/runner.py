"""Coordinator runner — asyncio loop, per-agent enable, parallel gather."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List

from shared.coordinator.agents import AGENT_FUNCS
from shared.coordinator.state import AGENTS, STATE

log = logging.getLogger("risedual.paradox_coordinator")
_STOP = asyncio.Event()


async def run_agent(name: str) -> Dict[str, Any]:
    """Run one agent. Skipped if disabled or already running.
    Failures are stamped into state and never bubble up."""
    if name not in STATE.agents:
        return {"agent": name, "skipped": True, "reason": "unknown_agent"}
    st = STATE.agents[name]

    if not st.enabled:
        return {"agent": name, "skipped": True, "reason": "disabled"}
    if st.running:
        return {"agent": name, "skipped": True, "reason": "already_running"}

    st.running = True
    st.last_run_at = datetime.now(timezone.utc).isoformat()
    st.runs += 1

    try:
        result = await AGENT_FUNCS[name]()
        st.last_ok = True
        st.last_error = None
        # Compact result summary for status panel
        summary = (
            "ok"
            if not isinstance(result, dict)
            else (
                result.get("status")
                or ("noop" if result.get("noop") else None)
                or ("stub" if result.get("stub") else None)
                or "ok"
            )
        )
        st.last_result_summary = str(summary)
        return {"agent": name, "ok": True, "result": result}
    except Exception as e:  # noqa: BLE001
        st.last_ok = False
        st.last_error = str(e)
        st.failures += 1
        log.warning("paradox_coordinator: agent=%s failed: %s", name, e)
        return {"agent": name, "ok": False, "error": str(e)}
    finally:
        st.running = False


async def run_cycle() -> List[Dict[str, Any]]:
    """Fire every enabled agent in parallel. Execute is included in the
    same group — it has its own enable flag, its own gate chain, and
    its own paradox-record audit, so there is no race / no bypass."""
    results = await asyncio.gather(
        *[run_agent(name) for name in AGENTS],
        return_exceptions=False,
    )
    return results


async def coordinator_loop():
    STATE.loop_active = True
    log.info("paradox_coordinator: loop start (interval=%ds)", STATE.cycle_seconds)
    try:
        while not _STOP.is_set():
            try:
                await run_cycle()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.exception("paradox_coordinator: cycle error: %s", e)
            try:
                await asyncio.wait_for(_STOP.wait(), timeout=STATE.cycle_seconds)
            except asyncio.TimeoutError:
                pass
    finally:
        STATE.loop_active = False
        log.info("paradox_coordinator: loop stopped")


def request_stop() -> None:
    _STOP.set()


def reset_stop_for_tests() -> None:
    """Test-only helper — resets the stop sentinel."""
    global _STOP
    _STOP = asyncio.Event()

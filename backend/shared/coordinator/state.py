"""Coordinator in-memory state.

Authoritative state for the PARADOX coordinator. Held in-process; lost
on restart by design (we want a clean slate every boot — the operator
re-enables the agents they trust). State writes are intentionally
local; only the runtime can flip enabled flags, and only via the
authenticated `/api/admin/coordinator/{enable,disable}` endpoints.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional

AGENTS = ("scan", "evaluate", "execute", "risk", "retrain")


@dataclass
class AgentState:
    enabled: bool = False
    running: bool = False
    last_run_at: Optional[str] = None
    last_ok: Optional[bool] = None
    last_error: Optional[str] = None
    last_result_summary: Optional[str] = None
    runs: int = 0
    failures: int = 0


@dataclass
class CoordinatorState:
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    cycle_seconds: int = 300
    agents: Dict[str, AgentState] = field(
        default_factory=lambda: {a: AgentState() for a in AGENTS}
    )
    loop_active: bool = False


STATE = CoordinatorState()


def snapshot() -> Dict[str, Any]:
    return {
        "started_at": STATE.started_at,
        "cycle_seconds": STATE.cycle_seconds,
        "loop_active": STATE.loop_active,
        "agents": {k: vars(v) for k, v in STATE.agents.items()},
    }

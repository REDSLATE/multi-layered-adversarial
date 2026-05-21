"""Operator-facing routes for the PARADOX coordinator.

Doctrine:
  * Default state — every agent disabled. Operator must explicitly
    enable each one.
  * No global kill switch. Each agent's enable flag is independent.
  * Execute agent's actual order submission still flows through the
    real `/api/execution/submit` (and therefore the full gate chain
    and paradox-record writer). This route layer cannot bypass that.

Mount path (under `api_router` which prefixes `/api`):
    GET  /api/admin/coordinator/status
    POST /api/admin/coordinator/enable/{agent}
    POST /api/admin/coordinator/disable/{agent}
    POST /api/admin/coordinator/run/{agent}
    POST /api/admin/coordinator/run-cycle
    POST /api/admin/coordinator/cycle-seconds/{seconds}
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from auth import get_current_user
from shared.coordinator.runner import run_agent, run_cycle
from shared.coordinator.state import AGENTS, STATE, snapshot


router = APIRouter(prefix="/admin/coordinator", tags=["paradox-coordinator"])


@router.get("/status")
async def status(_user: dict = Depends(get_current_user)):
    return snapshot()


@router.post("/enable/{agent}")
async def enable(agent: str, _user: dict = Depends(get_current_user)):
    if agent not in AGENTS:
        raise HTTPException(404, f"unknown agent: {agent}")
    STATE.agents[agent].enabled = True
    return {"ok": True, "agent": agent, "enabled": True}


@router.post("/disable/{agent}")
async def disable(agent: str, _user: dict = Depends(get_current_user)):
    if agent not in AGENTS:
        raise HTTPException(404, f"unknown agent: {agent}")
    STATE.agents[agent].enabled = False
    return {"ok": True, "agent": agent, "enabled": False}


@router.post("/run/{agent}")
async def run_one(agent: str, _user: dict = Depends(get_current_user)):
    if agent not in AGENTS:
        raise HTTPException(404, f"unknown agent: {agent}")
    return await run_agent(agent)


@router.post("/run-cycle")
async def run_full_cycle(_user: dict = Depends(get_current_user)):
    return {"ok": True, "results": await run_cycle()}


@router.post("/cycle-seconds/{seconds}")
async def set_cycle_seconds(seconds: int, _user: dict = Depends(get_current_user)):
    """Tune the loop interval. Bounded 30s – 3600s."""
    if seconds < 30 or seconds > 3600:
        raise HTTPException(422, "cycle_seconds must be in [30, 3600]")
    STATE.cycle_seconds = seconds
    return {"ok": True, "cycle_seconds": seconds}

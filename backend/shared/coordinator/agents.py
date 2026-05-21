"""Agent task definitions — each agent POSTs to a gated MC endpoint.

Crucially, the execute agent does NOT direct-import any trading code.
It hits `/api/execution/submit` so every order goes through the full
11-gate chain (executor_seat_check, roadguard_spread_floor, governor,
opponent, caps) and produces a paradox_record.

All HTTP failures are caught — the runner stamps them into AgentState
without crashing the loop.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

import httpx
import jwt

JWT_ALGORITHM = "HS256"
_API_BASE = os.environ.get("RISEDUAL_INTERNAL_API_BASE", "http://127.0.0.1:8001")
_COORDINATOR_USER_ID = "paradox-coordinator"
_COORDINATOR_USER_EMAIL = "coordinator@paradox.internal"


def _mint_internal_jwt() -> str:
    """Mint a short-lived access token for self-calls.

    Uses the same JWT_SECRET as the live auth path, so the coordinator
    passes `get_current_user` without any back-door.
    """
    secret = os.environ.get("JWT_SECRET")
    if not secret:
        return ""
    return jwt.encode(
        {
            "sub": _COORDINATOR_USER_ID,
            "email": _COORDINATOR_USER_EMAIL,
            "exp": datetime.now(timezone.utc) + timedelta(minutes=10),
            "type": "access",
            "issuer": "paradox_coordinator",
        },
        secret,
        algorithm=JWT_ALGORITHM,
    )


def _headers() -> Dict[str, str]:
    h: Dict[str, str] = {
        "X-Paradox-Coordinator": "true",
        "Content-Type": "application/json",
    }
    tok = _mint_internal_jwt()
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


async def _post(path: str, payload: Dict[str, Any] | None = None, timeout: float = 60.0) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=timeout) as cli:
        r = await cli.post(f"{_API_BASE}{path}", json=payload or {}, headers=_headers())
        r.raise_for_status()
        return r.json()


# ───── agent functions ──────────────────────────────────────────────


async def run_scan() -> Dict[str, Any]:
    return await _post("/api/admin/paradox/scan", {"source": "paradox_coordinator"})


async def run_evaluate() -> Dict[str, Any]:
    return await _post("/api/admin/paradox/evaluate", {"source": "paradox_coordinator"})


async def run_execute() -> Dict[str, Any]:
    """Pulls one queued intent and routes it through the FULL gate
    chain via `/api/admin/paradox/execute-next`. The stub endpoint
    finds the oldest queued intent that passed dry-run, then submits
    via the real `/api/execution/submit` — so the gate chain and
    paradox_record writer both fire. There is NO direct-import path."""
    return await _post("/api/admin/paradox/execute-next", {"source": "paradox_coordinator"})


async def run_risk() -> Dict[str, Any]:
    return await _post("/api/admin/risk/check", {"source": "paradox_coordinator"})


async def run_retrain() -> Dict[str, Any]:
    return await _post("/api/admin/ml/retrain/check", {"source": "paradox_coordinator"})


AGENT_FUNCS = {
    "scan": run_scan,
    "evaluate": run_evaluate,
    "execute": run_execute,
    "risk": run_risk,
    "retrain": run_retrain,
}

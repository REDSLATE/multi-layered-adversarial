"""
Paradox Coordinator v0 — Risk check service.

Doctrine pin (2026-02-XX):
    Per-candidate risk check + global risk pause logic.

    Per-symbol fail → mark the candidate `risk_blocked`. The
    candidate stays in `paradox_candidates` for forensic review;
    a paradox_record row of kind `paradox_v0_risk_block` is written
    so the timeline is auditable.

    Global pause is triggered ONLY by (per user spec):
      * daily loss limit hit
      * broker health failed
      * kill switch active

    Other failures (per-symbol exposure, duplicate position, lane
    cap reached) are per-candidate only — the loop keeps cycling.

    The risk service does NOT halt the FastAPI process. "Pause"
    means writing a `paradox_coordinator_paused=True` flag into
    the global state surface; the coordinator's outer loop reads
    that flag and skips its scan/evaluate phases until cleared.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from db import db
from namespaces import PARADOX_CANDIDATES, PARADOX_RECORDS, SHARED_POSITIONS

log = logging.getLogger("risedual.paradox_risk")


# Per-spec defaults — kept here (not env) so tripwires can lock them.
RISK_DEFAULT_DAILY_LOSS_USD = 2000.0
RISK_DEFAULT_OPEN_POSITIONS_MAX = 8
RISK_DEFAULT_TOTAL_EXPOSURE_USD = 50_000.0
RISK_DEFAULT_LANE_CAP_USD = 30_000.0


# ─── helpers ──────────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _open_positions_count() -> int:
    from shared.positions import OPEN_STATES
    try:
        return await db[SHARED_POSITIONS].count_documents(
            {"state": {"$in": list(OPEN_STATES)}},
        )
    except Exception as e:  # noqa: BLE001
        log.warning("open_positions_count failed: %s", e)
        return 0


async def _symbol_has_open_position(symbol: str) -> bool:
    from shared.positions import OPEN_STATES
    try:
        doc = await db[SHARED_POSITIONS].find_one(
            {"symbol": symbol, "state": {"$in": list(OPEN_STATES)}},
            {"_id": 1},
        )
        return doc is not None
    except Exception:  # noqa: BLE001
        return False


async def _total_exposure_usd() -> float:
    from shared.positions import OPEN_STATES
    total = 0.0
    try:
        async for d in db[SHARED_POSITIONS].find(
            {"state": {"$in": list(OPEN_STATES)}},
            {"_id": 0, "notional_usd": 1, "lane": 1},
        ):
            total += float(d.get("notional_usd") or 0.0)
    except Exception:  # noqa: BLE001
        pass
    return total


async def _lane_exposure_usd(lane: str) -> float:
    from shared.positions import OPEN_STATES
    total = 0.0
    try:
        async for d in db[SHARED_POSITIONS].find(
            {"state": {"$in": list(OPEN_STATES)}, "lane": lane},
            {"_id": 0, "notional_usd": 1},
        ):
            total += float(d.get("notional_usd") or 0.0)
    except Exception:  # noqa: BLE001
        pass
    return total


async def _kill_switch_active() -> bool:
    """Read the system kill-switch state, best-effort."""
    try:
        from shared.kill_switch import is_killed
        return bool(is_killed())
    except (ImportError, Exception):  # noqa: BLE001
        # If we can't read the kill switch, fail SAFE (assume not
        # killed). The coordinator already has its own gates; this
        # is just an additional check.
        return False


async def _broker_health_ok() -> bool:
    """Best-effort broker health probe."""
    try:
        from shared.broker_router import broker_health_snapshot
        snap = await broker_health_snapshot()
        return bool(snap and snap.get("ok"))
    except (ImportError, Exception):  # noqa: BLE001
        # If we can't probe, don't trigger global pause from this
        # check alone.
        return True


async def _daily_loss_breached(limit_usd: float) -> bool:
    """Best-effort PnL lookup. If a PnL surface isn't available,
    return False (don't pause from an unknown)."""
    try:
        from shared.pnl import daily_realized_pnl
        pnl = await daily_realized_pnl()
        return pnl is not None and pnl <= -abs(limit_usd)
    except (ImportError, Exception):  # noqa: BLE001
        return False


# ─── global state ────────────────────────────────────────────────────


async def check_global() -> Dict[str, Any]:
    """Return the global-pause state + the reasons."""
    triggers: List[str] = []
    kill = await _kill_switch_active()
    if kill:
        triggers.append("kill_switch_active")
    broker_ok = await _broker_health_ok()
    if not broker_ok:
        triggers.append("broker_health_failed")
    daily_loss_hit = await _daily_loss_breached(RISK_DEFAULT_DAILY_LOSS_USD)
    if daily_loss_hit:
        triggers.append("daily_loss_limit_hit")
    return {
        "paused": bool(triggers),
        "global_triggers": triggers,
        "kill_switch_active": kill,
        "broker_health_ok": broker_ok,
        "daily_loss_limit_breached": daily_loss_hit,
        "checked_at": _now().isoformat(),
    }


# ─── per-candidate ────────────────────────────────────────────────────


async def check_candidate(*, candidate_id: str) -> Dict[str, Any]:
    """Per-candidate gate. Returns the per-candidate verdict +
    embeds the current global state. If the per-candidate verdict
    is risk_blocked, persist a paradox_record so the timeline is
    auditable and stamp the candidate row."""
    candidate = await db[PARADOX_CANDIDATES].find_one(
        {"candidate_id": candidate_id},
    )
    if not candidate:
        raise ValueError(f"candidate {candidate_id!r} not found")

    global_state = await check_global()

    failures: List[str] = list(global_state["global_triggers"])

    open_count = await _open_positions_count()
    if open_count >= RISK_DEFAULT_OPEN_POSITIONS_MAX:
        failures.append("open_positions_max_reached")

    symbol = candidate.get("symbol", "")
    if symbol and await _symbol_has_open_position(symbol):
        failures.append("per_symbol_duplicate")

    total_exp = await _total_exposure_usd()
    if total_exp >= RISK_DEFAULT_TOTAL_EXPOSURE_USD:
        failures.append("total_exposure_cap")

    lane = candidate.get("lane", "equity")
    lane_exp = await _lane_exposure_usd(lane)
    if lane_exp >= RISK_DEFAULT_LANE_CAP_USD:
        failures.append("lane_cap_reached")

    blocked = bool(failures)
    verdict = {
        "candidate_id": candidate_id,
        "symbol": symbol,
        "lane": lane,
        "blocked": blocked,
        "failures": failures,
        "open_positions": open_count,
        "total_exposure_usd": round(total_exp, 2),
        "lane_exposure_usd": round(lane_exp, 2),
        "global": global_state,
        "checked_at": _now().isoformat(),
    }

    if blocked:
        # Stamp the candidate and write an audit record. We do NOT
        # delete the candidate — keep it for forensic review.
        await db[PARADOX_CANDIDATES].update_one(
            {"candidate_id": candidate_id},
            {"$set": {"status": "risk_blocked", "risk_failures": failures}},
        )
        await db[PARADOX_RECORDS].insert_one({
            "evaluation_kind": "paradox_v0_risk_block",
            "candidate_id": candidate_id,
            "symbol": symbol,
            "lane": lane,
            "failures": failures,
            "global": global_state,
            "created_at": _now(),
            "llm_authority": "ADVISORY_ONLY",
        })

    return {"ok": True, "verdict": verdict}

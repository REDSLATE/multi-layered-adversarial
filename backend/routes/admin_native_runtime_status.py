"""Native brain runtime status — operator visibility for the
in-process brain migration (2026-02-23).

Endpoint:
    GET /api/admin/native-runtime/status

Reports per-brain:
  * `enabled`        — is `<BRAIN>_NATIVE_RUNTIME_ENABLED` true?
  * `last_tick`      — freshest row from `<brain>_native_runtime_ticks`
  * `tick_age_sec`   — how long ago the last tick fired (None if never)
  * `tick_count_60m` — number of ticks in the last 60 minutes
  * `emitted_60m`    — total intents emitted in the last 60 minutes
  * `errors_60m`     — total per-symbol errors in the last 60 minutes
  * `silent`         — true when `enabled` is on AND no tick in 5+ min

Read-only. No mutations, no execution path side-effects. Pure
diagnostic view for the operator to confirm the silent-worker bug
is gone after flipping each brain's native flag.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Path

from auth import get_current_user
from db import db
from shared.runtime import (
    barracuda_runtime, camino_runtime, gto_runtime, hellcat_runtime,
)


router = APIRouter(tags=["admin"])


BRAINS = (
    ("barracuda", barracuda_runtime, "barracuda_native_runtime_ticks"),
    ("gto",       gto_runtime,       "gto_native_runtime_ticks"),
    ("camino",    camino_runtime,    "camino_native_runtime_ticks"),
    ("hellcat",   hellcat_runtime,   "hellcat_native_runtime_ticks"),
)
BRAIN_BY_ID = {bid: (mod, coll) for bid, mod, coll in BRAINS}

SILENT_THRESHOLD_SEC = 5 * 60


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None


async def _brain_status(brain_id: str, runtime_mod, tick_collection: str) -> dict:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=1)
    cutoff_iso = cutoff.isoformat()

    last_tick = await db[tick_collection].find_one(
        {}, {"_id": 0}, sort=[("started_at", -1)],
    )

    cursor = db[tick_collection].find(
        {"started_at": {"$gte": cutoff_iso}},
        {"_id": 0, "emitted_count": 1, "error_count": 1, "started_at": 1},
    )
    tick_count = 0
    emitted_60m = 0
    errors_60m = 0
    async for row in cursor:
        tick_count += 1
        emitted_60m += int(row.get("emitted_count") or 0)
        errors_60m += int(row.get("error_count") or 0)

    last_ts = _parse_iso((last_tick or {}).get("started_at"))
    tick_age_sec = int((now - last_ts).total_seconds()) if last_ts else None

    enabled = bool(runtime_mod.is_enabled())
    silent = bool(
        enabled and (tick_age_sec is None or tick_age_sec > SILENT_THRESHOLD_SEC)
    )

    return {
        "brain_id": brain_id,
        "enabled": enabled,
        "tick_age_sec": tick_age_sec,
        "silent": silent,
        "tick_count_60m": tick_count,
        "emitted_60m": emitted_60m,
        "errors_60m": errors_60m,
        "last_tick": last_tick,
    }


@router.get("/admin/native-runtime/status")
async def admin_native_runtime_status(
    user: dict = Depends(get_current_user),  # noqa: B008, ARG001
):
    """Per-brain native runtime status. See module docstring."""
    rows = []
    for brain_id, runtime_mod, tick_collection in BRAINS:
        rows.append(
            await _brain_status(brain_id, runtime_mod, tick_collection)
        )
    silent_brains = [r["brain_id"] for r in rows if r["silent"]]
    enabled_brains = [r["brain_id"] for r in rows if r["enabled"]]
    return {
        "ok": True,
        "as_of": datetime.now(timezone.utc).isoformat(),
        "silent_threshold_sec": SILENT_THRESHOLD_SEC,
        "enabled_brains": enabled_brains,
        "silent_brains": silent_brains,
        "brains": rows,
    }


@router.post("/admin/native-runtime/{brain_id}/tick-once")
async def admin_native_runtime_tick_once(
    brain_id: str = Path(..., description="canonical brain id"),
    user: dict = Depends(get_current_user),  # noqa: B008, ARG001
):
    """Fire a single tick for one brain RIGHT NOW.

    Doctrine (Friday post-redeploy validation):
        After flipping `<BRAIN>_NATIVE_RUNTIME_ENABLED=true` and
        deploying, the operator can call this endpoint instead of
        waiting 60s for the scheduled tick. The returned summary
        carries the SAME shape as a scheduled tick row — including
        the list of `emitted` intents with their `intent_id` and
        `gate_state`, so the operator can immediately confirm:
          * the brain is running in-process
          * it actually emitted BUY/SHORT (not HOLD)
          * each intent cleared the gate chain (`gate_state` ≠
            `dry_run_blocked`)

    NO env-flag check: this endpoint runs the tick directly, so
    the operator can also dry-test the runtime BEFORE flipping
    the production flag. Useful for sanity-checking my native
    runtime against current market data without enabling the
    background loop.
    """
    bid = (brain_id or "").lower().strip()
    if bid not in BRAIN_BY_ID:
        raise HTTPException(
            status_code=404,
            detail=f"unknown brain_id {brain_id!r}; expected one of {sorted(BRAIN_BY_ID)}",
        )
    # Late import — each brain's runner.tick_once is the same shape.
    import importlib
    runner_mod = importlib.import_module(f"shared.brains.{bid}.runner")
    summary = await runner_mod.tick_once(db)
    return {"ok": True, "summary": summary}

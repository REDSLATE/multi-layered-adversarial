"""Post-deploy runtime validation suite.

Doctrine pin (2026-02-26, advisor-recommended after a day of deploy
failures): Static analyzers can verify the codebase is structurally
sound, but they CANNOT tell us:

  * Whether the auto-router actually ticks.
  * Whether required Mongo indexes exist in the LIVE database.
  * Whether the pod has actually become Ready.
  * Whether sample queries respond within a reasonable budget.
  * Whether background workers are alive.

This endpoint runs read-only runtime checks against the live pod and
returns a structured pass/warn/fail per check. Curl this once after
every deploy. If everything is "pass", the system is genuinely
healthy. If anything is "fail", the report shows EXACTLY what's
broken — no log grepping needed.

`GET /api/admin/healthcheck/full` is the only endpoint here. All
checks are read-only, time-bounded, and intentionally NEVER call any
broker mutation endpoint.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends

from auth import get_current_user
from db import db


logger = logging.getLogger("risedual.healthcheck_full")
router = APIRouter(prefix="/admin/healthcheck", tags=["healthcheck"])


# Per-check budget (seconds). Each check is wrapped in `asyncio.wait_for`
# at this deadline so a single slow query can never make this endpoint
# take more than (count * budget).
_PER_CHECK_BUDGET_S = 4.0


# Indexes the runtime DEPENDS on. If any of these are missing on the
# live database, downstream queries will time out and trigger 520s.
# Source of truth for "are required indexes in place?".
_REQUIRED_INDEXES: list[tuple[str, str]] = [
    ("shared_intents", "shared_intents_action_created_idx"),
    ("shared_intents", "shared_intents_ingest_ts_idx"),
    ("shared_intents", "shared_intents_conviction_idx"),
    ("shared_gate_results", "shared_gate_results_kind_ts_idx"),
    ("shared_gate_results", "shared_gate_results_intent_ts_idx"),
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _bounded(coro, *, default):
    """Run a coroutine with the per-check budget. On timeout return
    `default` so one slow check can't poison the whole report."""
    try:
        return await asyncio.wait_for(coro, timeout=_PER_CHECK_BUDGET_S)
    except asyncio.TimeoutError:
        return default


async def _check_mongo_connected() -> dict:
    started = time.monotonic()
    try:
        result = await _bounded(
            db.command("ping"),
            default={"ok": 0, "timed_out": True},
        )
        elapsed_ms = int((time.monotonic() - started) * 1000)
        ok = bool(result and result.get("ok") == 1)
        return {
            "status": "pass" if ok else "fail",
            "elapsed_ms": elapsed_ms,
            "detail": "Mongo ping ok" if ok else f"Mongo ping failed: {result}",
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "fail",
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "detail": f"{type(exc).__name__}: {str(exc)[:160]}",
        }


async def _check_required_indexes() -> dict:
    """Verify the runtime-critical indexes EXIST in the live database.
    This is the check that would have caught the auto-router stall."""
    started = time.monotonic()
    missing: list[str] = []
    present: list[str] = []
    errors: list[str] = []
    for coll_name, idx_name in _REQUIRED_INDEXES:
        try:
            cursor = db[coll_name].list_indexes()
            names = await _bounded(
                cursor.to_list(length=200),
                default=None,
            )
            if names is None:
                errors.append(f"{coll_name}.list_indexes timed out")
                continue
            existing = {i.get("name") for i in names}
            if idx_name in existing:
                present.append(f"{coll_name}.{idx_name}")
            else:
                missing.append(f"{coll_name}.{idx_name}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{coll_name}: {type(exc).__name__}")
    elapsed_ms = int((time.monotonic() - started) * 1000)
    if errors:
        status = "warn"
    elif missing:
        status = "fail"
    else:
        status = "pass"
    return {
        "status": status,
        "elapsed_ms": elapsed_ms,
        "present": present,
        "missing": missing,
        "errors": errors,
        "detail": (
            f"{len(present)}/{len(_REQUIRED_INDEXES)} required indexes present"
            + (f"; MISSING: {missing}" if missing else "")
            + (f"; ERRORS: {errors}" if errors else "")
        ),
    }


async def _check_auto_router_ticking() -> dict:
    started = time.monotonic()
    try:
        from shared.auto_router import get_status  # noqa: WPS433
        s = get_status()
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "fail",
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "detail": f"get_status raised: {type(exc).__name__}: {exc}",
        }
    task_alive = bool(s.get("task_alive"))
    tick_count = int(s.get("tick_count") or 0)
    last_tick_ts = s.get("last_tick_ts")
    last_tick_error = s.get("last_tick_error")
    interval = float(s.get("interval_sec") or 60)

    if not task_alive:
        status = "fail"
        detail = "auto_router task is NOT alive — no autonomous trades possible"
    elif tick_count == 0:
        status = "fail"
        detail = f"task alive but tick_count=0 (last_error={last_tick_error or 'none'})"
    elif last_tick_ts is None:
        status = "warn"
        detail = "task alive, tick_count>0, but no last_tick_ts set"
    else:
        # Check liveness: last_tick_ts should be within ~2× interval.
        try:
            last_dt = datetime.fromisoformat(last_tick_ts.replace("Z", "+00:00"))
            age_s = (datetime.now(timezone.utc) - last_dt).total_seconds()
        except (TypeError, ValueError):
            age_s = None
        if age_s is None:
            status = "warn"
            detail = f"could not parse last_tick_ts={last_tick_ts!r}"
        elif age_s > 2.5 * interval:
            status = "warn"
            detail = (
                f"last_tick_ts is {age_s:.0f}s old "
                f"(interval={interval}s) — tick may be stuck"
            )
        else:
            status = "pass"
            detail = (
                f"ticking healthily: tick_count={tick_count}, "
                f"last_tick {age_s:.0f}s ago"
            )

    return {
        "status": status,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "task_alive": task_alive,
        "tick_count": tick_count,
        "last_tick_ts": last_tick_ts,
        "last_tick_error": last_tick_error,
        "interval_sec": interval,
        "detail": detail,
    }


async def _check_sample_intent_query() -> dict:
    """The query the auto-router runs every tick. If THIS times out,
    the auto-router will too. Mirrors `shared/auto_router.py::_tick`
    EXACTLY — same filter shape, same sort, same lookback — so the
    health signal accurately tracks the real bottleneck."""
    started = time.monotonic()
    try:
        lookback_min = int(__import__("os").environ.get(
            "AUTO_ROUTER_LOOKBACK_MIN", "60",
        ))
    except (TypeError, ValueError):
        lookback_min = 60
    cutoff = (
        datetime.now(timezone.utc) - timedelta(minutes=lookback_min)
    ).isoformat()
    q = {
        "ingest_ts": {"$gte": cutoff},
        "executed": {"$ne": True},
        "action": {"$in": ["BUY", "SELL", "SHORT", "COVER"]},
        "symbol": {"$ne": None},
        "gate_state": {"$nin": ["blocked", "no_trade", "advisory_only"]},
    }
    try:
        rows = await _bounded(
            db.shared_intents.find(q, {"_id": 0, "intent_id": 1})
            .sort("ingest_ts", -1).max_time_ms(3500).to_list(5),
            default=None,
        )
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if rows is None:
            return {
                "status": "fail",
                "elapsed_ms": elapsed_ms,
                "detail": (
                    f"auto-router-shaped query timed out at {_PER_CHECK_BUDGET_S}s "
                    f"— check `shared_intents_ingest_ts_idx` exists and "
                    f"AUTO_ROUTER_LOOKBACK_MIN is bounded"
                ),
            }
        status = "pass" if elapsed_ms < 1000 else "warn"
        return {
            "status": status,
            "elapsed_ms": elapsed_ms,
            "rows_returned": len(rows),
            "lookback_min": lookback_min,
            "detail": (
                f"auto-router-shaped query OK in {elapsed_ms}ms "
                f"({len(rows)} candidates in last {lookback_min}min)"
            ),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "fail",
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "detail": f"{type(exc).__name__}: {str(exc)[:200]}",
        }


async def _check_direct_execute_state() -> dict:
    started = time.monotonic()
    try:
        from shared.direct_execute import is_direct_execute_enabled  # noqa: WPS433
        enabled = await _bounded(is_direct_execute_enabled(), default=None)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if enabled is None:
            return {
                "status": "warn", "elapsed_ms": elapsed_ms,
                "detail": "direct-execute state lookup timed out",
            }
        return {
            "status": "pass",
            "elapsed_ms": elapsed_ms,
            "enabled": bool(enabled),
            "detail": (
                f"direct_execute_mode={'ON' if enabled else 'OFF'} "
                f"({'gates bypassed' if enabled else 'normal pipeline'})"
            ),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "warn",
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "detail": f"{type(exc).__name__}: {str(exc)[:200]}",
        }


async def _check_recent_intents() -> dict:
    """Confirm new intents are being ingested. Empty pipe = upstream
    (brain runners) is the bottleneck, not the router."""
    started = time.monotonic()
    one_hour_ago = (datetime.now(timezone.utc).timestamp() - 3600)
    cutoff_iso = datetime.fromtimestamp(one_hour_ago, tz=timezone.utc).isoformat()
    try:
        n = await _bounded(
            db.shared_intents.count_documents(
                {"created_at": {"$gte": cutoff_iso}},
                maxTimeMS=3000,
            ),
            default=None,
        )
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if n is None:
            return {
                "status": "warn",
                "elapsed_ms": elapsed_ms,
                "detail": "count timed out",
            }
        if n == 0:
            status = "warn"
            detail = "0 intents in last hour — brain runners may be silent"
        elif n < 10:
            status = "warn"
            detail = f"only {n} intents in last hour — low signal"
        else:
            status = "pass"
            detail = f"{n} intents ingested in last hour"
        return {
            "status": status, "elapsed_ms": elapsed_ms,
            "intents_last_hour": n, "detail": detail,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "fail",
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "detail": f"{type(exc).__name__}: {str(exc)[:200]}",
        }


@router.get("/full")
async def healthcheck_full(_user: dict = Depends(get_current_user)):  # noqa: B008
    """Post-deploy runtime validation. Read-only, ~30s budget total.

    Returns one entry per check with `status: pass | warn | fail` and
    a human-readable `detail`. Top-level `overall` is the worst-case
    status — `pass` only if every check passes.
    """
    started = time.monotonic()
    checks: dict[str, Any] = {}
    checks["mongo_connected"] = await _check_mongo_connected()
    checks["required_indexes"] = await _check_required_indexes()
    checks["sample_intent_query"] = await _check_sample_intent_query()
    checks["auto_router_ticking"] = await _check_auto_router_ticking()
    checks["recent_intents"] = await _check_recent_intents()
    checks["direct_execute_state"] = await _check_direct_execute_state()

    # Roll-up. Order matters: fail > warn > pass.
    rank = {"pass": 0, "warn": 1, "fail": 2}
    worst = max((rank.get(c.get("status"), 2) for c in checks.values()), default=0)
    overall = {0: "pass", 1: "warn", 2: "fail"}[worst]

    failures = [k for k, c in checks.items() if c.get("status") == "fail"]
    warnings = [k for k, c in checks.items() if c.get("status") == "warn"]

    return {
        "overall": overall,
        "failures": failures,
        "warnings": warnings,
        "total_elapsed_ms": int((time.monotonic() - started) * 1000),
        "checked_at": _now_iso(),
        "checks": checks,
        "doctrine_note": (
            "Hit this after every deploy. If overall=pass, the system "
            "is genuinely healthy. If overall=fail, the failing check's "
            "`detail` field tells you exactly what to fix. Read-only — "
            "safe to poll."
        ),
    }

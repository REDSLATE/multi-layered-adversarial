"""Gain Goal worker — periodic evaluation, breach latch, snapshot
publication.

The drawdown hard stop is a BINDING risk rule: once a lane's window
drawdown breaches its limit, new entries for that lane are blocked
until operator acknowledgement or the next window reset. It never
auto-resumes because one later trade improved P&L — the latch pins
the breach even if max-drawdown math would soften.

Publication path honors the hot-path doctrine: block + throttle land
in the ExecutionPolicySnapshot (memory + SQLite) so the risk gate and
sizing stage never read Atlas synchronously. Atlas gets the state doc
(`runtime_flags._id=gain_goal_state`) for audit + restart recovery.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("risedual.gain_goal")

EVAL_INTERVAL_SEC = float(os.environ.get("GAIN_GOAL_EVAL_INTERVAL_SEC", "60"))

_state: dict[str, Any] = {"running": False, "task": None, "started_at": None,
                          "last_error": None}
_last_eval: Optional[dict] = None


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def last_evaluation() -> Optional[dict]:
    return _last_eval


async def _load_latches() -> dict:
    from db import db  # noqa: WPS433
    from shared.goals.gain_goal import STATE_FLAG_ID  # noqa: WPS433
    doc = await db["runtime_flags"].find_one({"_id": STATE_FLAG_ID}, {"_id": 0}) or {}
    return doc.get("latches") or {}


async def evaluate_and_publish(now=None) -> dict:
    """One full evaluation cycle: measure, reconcile the breach latch,
    publish block/throttle to the policy snapshot, mirror to Atlas."""
    global _last_eval  # noqa: PLW0603
    from db import db  # noqa: WPS433
    from shared.goals import gain_goal  # noqa: WPS433
    from shared.hotpath import policy_snapshot  # noqa: WPS433

    result = await gain_goal.evaluate_all(now)
    latches = await _load_latches()
    throttle_cfg = (result["config"].get("ahead_of_pace_throttle") or {})
    block: dict[str, bool] = {}
    throttle: dict[str, Optional[float]] = {}

    for lane, ev in result["lanes"].items():
        latch = latches.get(lane) or {}
        # Window rollover = the configured reset — clears the latch.
        # Rolling windows have a moving start (no rollover); their only
        # reset is operator acknowledgement.
        if (
            latch.get("window_start")
            and ev["window_type"] != "rolling_days"
            and latch["window_start"] != ev["window_start"]
        ):
            latch = {}
        if ev["drawdown_breached"] and not latch.get("breached"):
            latch = {
                "breached": True,
                "window_start": ev["window_start"],
                "breached_at": _iso(),
                # Exact breach reason + calculation, stamped (spec #3).
                "reason": (
                    f"window max drawdown ${ev['max_window_drawdown_usd']:.2f} "
                    f">= limit ${ev['drawdown_limit_usd']:.2f} "
                    f"(peak-to-trough of cumulative net realized P&L, "
                    f"{ev['resolved_trades']} resolved trades, window "
                    f"{ev['window_start'][:10]} → {ev['window_end'][:10]})"
                ),
                "acked_by": None,
                "acked_at": None,
            }
            logger.warning("GAIN GOAL drawdown BREACH lane=%s: %s", lane, latch["reason"])
        latches[lane] = latch
        active = bool(latch.get("breached") and not latch.get("acked_at"))
        block[lane] = active
        if active:
            ev["status"] = "DRAWDOWN_BREACHED"
        ev["breach_latch"] = latch or None
        ev["entries_blocked"] = active

        # Ahead-of-pace throttle: reduce-only, requires a set target.
        mult = None
        if (
            throttle_cfg.get("enabled")
            and ev.get("effective_target_usd")
            and ev.get("goal_progress_pct") is not None
            and ev["goal_progress_pct"] >= float(throttle_cfg.get("activation_pct_of_goal") or 100.0)
        ):
            m = float(throttle_cfg.get("notional_multiplier") or 0.5)
            mult = min(1.0, max(0.0, m))
            if mult >= 1.0:
                mult = None
        throttle[lane] = mult
        ev["throttle_active_multiplier"] = mult

    # Hot-path publication FIRST (memory + SQLite) — the gate reads this.
    try:
        policy_snapshot.apply_local(gain_goal={"block": block, "throttle": throttle})
    except Exception as exc:  # noqa: BLE001
        logger.warning("gain_goal snapshot publish failed: %s", exc)

    # Atlas mirror for audit + restart recovery (cold path).
    try:
        from shared.goals.gain_goal import STATE_FLAG_ID  # noqa: WPS433
        await db["runtime_flags"].update_one(
            {"_id": STATE_FLAG_ID},
            {"$set": {"latches": latches, "block": block, "throttle": throttle,
                      "evaluated_at": _iso()}},
            upsert=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("gain_goal state mirror failed: %s", exc)

    _last_eval = result
    return result


async def acknowledge_breach(lane: str, actor: str) -> dict:
    """Operator ack — the ONLY way to resume entries mid-window."""
    from db import db  # noqa: WPS433
    from shared.goals.gain_goal import STATE_FLAG_ID  # noqa: WPS433
    latches = await _load_latches()
    latch = latches.get(lane) or {}
    if not latch.get("breached"):
        return {"ok": False, "reason": "no_active_breach"}
    latch["acked_by"] = actor
    latch["acked_at"] = _iso()
    latches[lane] = latch
    await db["runtime_flags"].update_one(
        {"_id": STATE_FLAG_ID}, {"$set": {"latches": latches}}, upsert=True,
    )
    await evaluate_and_publish()
    return {"ok": True, "lane": lane, "acked_by": actor, "acked_at": latch["acked_at"]}


async def _loop() -> None:
    logger.info("gain_goal worker start interval=%.0fs", EVAL_INTERVAL_SEC)
    while True:
        try:
            await evaluate_and_publish()
            _state["last_error"] = None
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            _state["last_error"] = str(exc)[:300]
            logger.warning("gain_goal evaluation failed: %s", exc)
        await asyncio.sleep(EVAL_INTERVAL_SEC)


def start_if_enabled() -> None:
    if (os.environ.get("GAIN_GOAL_ENABLED") or "true").strip().lower() in (
        "0", "false", "no", "off",
    ):
        logger.info("gain_goal worker disabled")
        return
    if _state.get("running"):
        return
    task = asyncio.get_event_loop().create_task(_loop(), name="gain_goal_worker")
    _state.update(running=True, task=task, started_at=_iso())


async def stop() -> None:
    task = _state.get("task")
    if task and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _state.update(running=False, task=None)

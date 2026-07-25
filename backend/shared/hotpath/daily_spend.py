"""Daily-spend counter — replaces the per-intent Atlas `executions`
aggregate in the risk gate (audit P1 #3).

Spend increments IN MEMORY at execution time, persisted to the
hotpath SQLite DB (one row per UTC day), rebuilt at boot. Operator
RESET SPEND zeroes the local counter directly (write-through) and
the Atlas marker is reconciled by the policy-snapshot refresher.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from shared.hotpath import outbox

logger = logging.getLogger("risedual.hotpath.daily_spend")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_spend (
    day TEXT PRIMARY KEY,
    spent REAL NOT NULL DEFAULT 0,
    reset_at TEXT,
    updated_at TEXT NOT NULL
);
"""

_schema_for: Optional[str] = None
_mem: dict[str, Any] = {"day": None, "spent": 0.0, "reset_at": None}
_bootstrapped = False


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _conn():
    global _schema_for  # noqa: PLW0603
    c = outbox._connect()
    if _schema_for != outbox._DB_PATH:
        c.executescript(_SCHEMA)
        _schema_for = outbox._DB_PATH
    return c


def reset_for_tests() -> None:
    global _schema_for, _bootstrapped  # noqa: PLW0603
    _schema_for = None
    _bootstrapped = True
    _mem.update(day=None, spent=0.0, reset_at=None)


def _load_day(day: str) -> None:
    row = _conn().execute(
        "SELECT spent, reset_at FROM daily_spend WHERE day=?", (day,),
    ).fetchone()
    _mem.update(
        day=day,
        spent=float(row["spent"]) if row else 0.0,
        reset_at=row["reset_at"] if row else None,
    )


def _ensure_today() -> None:
    day = _today()
    if _mem["day"] != day:
        try:
            _load_day(day)
        except Exception as exc:  # noqa: BLE001
            logger.warning("daily_spend load failed: %s", exc)
            _mem.update(day=day, spent=0.0, reset_at=None)


def _persist() -> None:
    conn = _conn()
    with conn:
        conn.execute(
            "INSERT INTO daily_spend (day, spent, reset_at, updated_at) "
            "VALUES (?,?,?,?) ON CONFLICT(day) DO UPDATE SET "
            "spent=excluded.spent, reset_at=excluded.reset_at, "
            "updated_at=excluded.updated_at",
            (_mem["day"], _mem["spent"], _mem["reset_at"], _iso()),
        )


def get_spent() -> float:
    """Hot-path read — memory only after first load. UTC day
    rollover restarts at 0 automatically."""
    _ensure_today()
    return float(_mem["spent"])


def add(notional_usd: float) -> float:
    """Increment at execution time (broker accepted). Memory +
    SQLite commit."""
    _ensure_today()
    _mem["spent"] = float(_mem["spent"]) + max(0.0, float(notional_usd or 0.0))
    try:
        _persist()
    except Exception as exc:  # noqa: BLE001
        logger.warning("daily_spend persist failed: %s", exc)
    return float(_mem["spent"])


def reset(reset_at: Optional[str] = None) -> None:
    """RESET SPEND — zero today's counter."""
    _ensure_today()
    _mem["spent"] = 0.0
    _mem["reset_at"] = reset_at or _iso()
    try:
        _persist()
    except Exception as exc:  # noqa: BLE001
        logger.warning("daily_spend persist failed: %s", exc)


def observe_reset_marker(reset_at: Optional[str]) -> None:
    """Reconcile an Atlas reset marker seen by the snapshot
    refresher. A marker NEWER than our recorded one (and within
    today) zeroes the counter — keeps parity when the reset was
    issued outside this process."""
    if not reset_at:
        return
    _ensure_today()
    day_start = f"{_today()}T00:00:00"
    if str(reset_at) <= day_start:
        return
    if _mem["reset_at"] and str(reset_at) <= str(_mem["reset_at"]):
        return
    logger.info("daily_spend reset via Atlas marker %s", reset_at)
    reset(str(reset_at))


async def bootstrap() -> dict:
    """Boot rebuild. If SQLite already has today's row, use it.
    Otherwise ONE Atlas aggregate (same math as the legacy per-intent
    gate read) seeds the counter — fail-soft to 0."""
    global _bootstrapped  # noqa: PLW0603
    if _bootstrapped:
        return {"skipped": "already_bootstrapped"}
    _bootstrapped = True
    day = _today()
    try:
        row = _conn().execute(
            "SELECT spent FROM daily_spend WHERE day=?", (day,),
        ).fetchone()
    except Exception as exc:  # noqa: BLE001
        logger.warning("daily_spend bootstrap sqlite read failed: %s", exc)
        row = None
    if row is not None:
        _load_day(day)
        return {"source": "sqlite", "spent": float(_mem["spent"])}
    spent = 0.0
    reset_at = None
    try:
        from db import db  # noqa: WPS433
        start = f"{day}T00:00:00"
        doc = await db["runtime_flags"].find_one(
            {"_id": "daily_spend_reset"}, {"reset_at": 1},
        )
        reset_at = (doc or {}).get("reset_at")
        if reset_at and str(reset_at) > start:
            start = str(reset_at)
        pipeline = [
            {"$match": {"ts": {"$gte": start}, "ok": True}},
            {"$group": {"_id": None, "spent": {"$sum": "$notional_usd"}}},
        ]
        async for r in db["executions"].aggregate(pipeline, maxTimeMS=4000):
            spent = float(r.get("spent") or 0.0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("daily_spend bootstrap Atlas aggregate failed: %s", exc)
    _mem.update(day=day, spent=spent, reset_at=str(reset_at) if reset_at else None)
    try:
        _persist()
    except Exception:  # noqa: BLE001
        pass
    logger.info("daily_spend bootstrap: day=%s spent=%.2f", day, spent)
    return {"source": "atlas", "spent": spent}


def get_status() -> dict:
    _ensure_today()
    return {
        "day": _mem["day"],
        "spent_usd": round(float(_mem["spent"]), 2),
        "reset_at": _mem["reset_at"],
        "bootstrapped": _bootstrapped,
    }

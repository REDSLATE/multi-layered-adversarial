"""Exit-plan hot-path store (2026-07-23 — audit P0 #2).

Plans live in MEMORY + SQLite (same DB as the Atlas outbox); Atlas
receives full-snapshot mirrors via `exit_plan_mirror` outbox events
(cold path — dashboards/history only). The Exit Monitor NEVER needs
Atlas to decide whether to close a live position.

Restart: live plans rebuild from SQLite; a one-time bootstrap imports
live plans from Atlas when the SQLite table has never been populated
(prod continuity on the first deploy of this store).
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from shared.hotpath import outbox

logger = logging.getLogger("risedual.hotpath.exit_plans")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS exit_plans (
    plan_id TEXT PRIMARY KEY,
    lane TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_exit_plans_live ON exit_plans (status, lane);
"""

_LIVE = ("active", "exiting")            # monitored by the tick loop
_CACHED = ("active", "exiting", "error")  # kept in memory (panel shows error)

_schema_for: Optional[str] = None
_cache: dict[str, dict] = {}
_cache_loaded = False
_bootstrapped = False


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _conn():
    global _schema_for  # noqa: PLW0603
    c = outbox._connect()
    if _schema_for != outbox._DB_PATH:
        c.executescript(_SCHEMA)
        _schema_for = outbox._DB_PATH
    return c


def reset_for_tests() -> None:
    global _schema_for, _cache_loaded, _bootstrapped  # noqa: PLW0603
    _schema_for = None
    _cache.clear()
    _cache_loaded = False
    _bootstrapped = True  # tests never bootstrap from Atlas


def _load_cache() -> None:
    global _cache_loaded  # noqa: PLW0603
    if _cache_loaded:
        return
    conn = _conn()
    rows = conn.execute(
        "SELECT payload_json FROM exit_plans WHERE status IN (?,?,?)", _CACHED,
    ).fetchall()
    _cache.clear()
    for r in rows:
        p = json.loads(r["payload_json"])
        _cache[p["plan_id"]] = p
    _cache_loaded = True
    logger.info("exit_plans cache loaded: %d live plans", len(_cache))


def _persist(plan: dict) -> None:
    conn = _conn()
    with conn:
        conn.execute(
            "INSERT INTO exit_plans (plan_id, lane, status, payload_json, updated_at) "
            "VALUES (?,?,?,?,?) ON CONFLICT(plan_id) DO UPDATE SET "
            "lane=excluded.lane, status=excluded.status, "
            "payload_json=excluded.payload_json, updated_at=excluded.updated_at",
            (plan["plan_id"], plan.get("lane") or "", plan.get("status") or "",
             json.dumps(plan, default=str), _iso()),
        )


def _mirror(plan: dict) -> None:
    try:
        outbox.enqueue(
            "exit_plan_mirror", plan["plan_id"], dict(plan),
            event_id=f"exit_plan_mirror:{plan['plan_id']}:{uuid.uuid4().hex[:8]}",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "exit plan mirror enqueue failed %s: %s", plan.get("plan_id"), exc,
        )


def upsert(plan: dict, mirror: bool = True) -> None:
    """Memory + SQLite commit; Atlas mirror queued (write-behind)."""
    _load_cache()
    plan = {k: v for k, v in plan.items() if not k.startswith("_")}
    _persist(plan)
    if plan.get("status") in _CACHED:
        _cache[plan["plan_id"]] = plan
    else:
        _cache.pop(plan["plan_id"], None)
    if mirror:
        _mirror(plan)


def get(plan_id: str) -> Optional[dict]:
    _load_cache()
    p = _cache.get(plan_id)
    if p is not None:
        return p
    row = _conn().execute(
        "SELECT payload_json FROM exit_plans WHERE plan_id=?", (plan_id,),
    ).fetchone()
    return json.loads(row["payload_json"]) if row else None


def load_live(lane: Optional[str] = None) -> list[dict]:
    """Plans the tick loop monitors (active + exiting)."""
    _load_cache()
    return [
        p for p in _cache.values()
        if p.get("status") in _LIVE and (lane is None or p.get("lane") == lane)
    ]


def load_panel() -> list[dict]:
    """Operator panel view (active + exiting + error)."""
    _load_cache()
    return list(_cache.values())


def update(plan_id: str, fields: dict) -> Optional[dict]:
    plan = get(plan_id)
    if plan is None:
        return None
    plan = {**plan, **fields}
    upsert(plan)
    return plan


def reserve(plan_id: str, reason: str) -> bool:
    """Atomic trigger reservation — the SQLite conditional UPDATE is
    the arbiter; one active exit order per position, crash-safe."""
    plan = get(plan_id)
    if plan is None or plan.get("status") != "active":
        return False
    conn = _conn()
    with conn:
        cur = conn.execute(
            "UPDATE exit_plans SET status='exiting', updated_at=? "
            "WHERE plan_id=? AND status='active'",
            (_iso(), plan_id),
        )
        if cur.rowcount == 0:
            return False
    upsert({**plan, "status": "exiting", "exit_reason": reason,
            "reserved_at": _iso()})
    return True


def mark_closed(plan_id: str, fields: dict) -> Optional[dict]:
    return update(plan_id, {"status": "closed", **fields})


def counts() -> dict:
    _load_cache()
    out: dict[str, int] = {}
    for p in _cache.values():
        s = p.get("status") or "?"
        out[s] = out.get(s, 0) + 1
    return out


async def bootstrap() -> dict:
    """One-time Atlas → SQLite import for continuity on the first run
    after this store ships (plans previously lived only in Mongo)."""
    global _bootstrapped  # noqa: PLW0603
    if _bootstrapped:
        return {"skipped": "already_bootstrapped"}
    _bootstrapped = True
    conn = _conn()
    existing = conn.execute("SELECT COUNT(*) FROM exit_plans").fetchone()[0]
    if existing > 0:
        _load_cache()
        return {"skipped": "sqlite_already_populated", "rows": existing}
    imported = 0
    try:
        from db import db  # noqa: WPS433
        async for p in db["shared_exit_plans"].find(
            {"status": {"$in": list(_LIVE)}}, {"_id": 0},
        ):
            upsert(p, mirror=False)
            imported += 1
    except Exception as exc:  # noqa: BLE001
        logger.warning("exit_plans bootstrap from Atlas failed: %s", exc)
    logger.info("exit_plans bootstrap imported %d live plans", imported)
    return {"imported": imported}

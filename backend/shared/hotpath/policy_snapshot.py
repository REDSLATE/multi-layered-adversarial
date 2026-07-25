"""ExecutionPolicySnapshot — versioned in-memory gate policy (audit P1 #3/#4).

Doctrine: "No live execution decision should depend on a synchronous
Atlas read." Every gate knob the execution loop consults per intent —
daily cap override, master trading switch, lane toggles, broker
freeze, conviction floor, opportunity policy, daily-spend reset
marker — lives in ONE atomic in-memory snapshot, persisted to the
hotpath SQLite DB and refreshed from Atlas asynchronously.

Write path stays Atlas (source of truth for operator knobs). Admin
mutation endpoints either call `refresh_from_atlas()` (write-through)
or `mark_dirty()` (next async accessor refreshes). The hot path reads
`get()` — pure memory, never blocks.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

from shared.hotpath import outbox

logger = logging.getLogger("risedual.hotpath.policy_snapshot")

REFRESH_INTERVAL_SEC = float(os.environ.get("POLICY_SNAPSHOT_REFRESH_SEC", "20"))
_READ_TIMEOUT_SEC = 4.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS policy_snapshot (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    refreshed_at TEXT
);
"""

_schema_for: Optional[str] = None
_snap: Optional[dict] = None
_version: int = 0
_dirty: bool = True
_state: dict[str, Any] = {"running": False, "task": None, "started_at": None,
                          "last_refresh_at": None, "last_error": None}


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
    global _schema_for, _snap, _version, _dirty  # noqa: PLW0603
    _schema_for = None
    _snap = None
    _version = 0
    _dirty = True


def _defaults() -> dict:
    from shared.opportunity.policy import _merge  # noqa: WPS433
    return {
        "version": 0,
        "source": "defaults",
        "refreshed_at": None,
        "cap_daily_usd_override": None,
        "master_switch_enabled": True,
        "lane_enabled": {"equity": True, "crypto": True},
        "broker_frozen": False,
        "broker_freeze_reason": None,
        "conviction_floor": None,
        "opportunity_policy": _merge({}),
        "daily_spend_reset_at": None,
        "gain_goal": {"block": {}, "throttle": {}},
        "degraded_keys": [],
    }


def _persist(snap: dict) -> None:
    conn = _conn()
    with conn:
        conn.execute(
            "INSERT INTO policy_snapshot (id, version, payload_json, refreshed_at) "
            "VALUES (1,?,?,?) ON CONFLICT(id) DO UPDATE SET "
            "version=excluded.version, payload_json=excluded.payload_json, "
            "refreshed_at=excluded.refreshed_at",
            (snap.get("version", 0), json.dumps(snap, default=str),
             snap.get("refreshed_at")),
        )


def _load_from_sqlite() -> Optional[dict]:
    row = _conn().execute(
        "SELECT payload_json, version FROM policy_snapshot WHERE id=1",
    ).fetchone()
    if not row:
        return None
    snap = json.loads(row["payload_json"])
    snap["source"] = "sqlite"
    return snap


def get() -> dict:
    """Hot-path read. Memory → SQLite recovery → env defaults.
    NEVER touches Atlas."""
    global _snap, _version  # noqa: PLW0603
    if _snap is not None:
        return _snap
    try:
        loaded = _load_from_sqlite()
    except Exception as exc:  # noqa: BLE001
        logger.warning("policy snapshot sqlite recovery failed: %s", exc)
        loaded = None
    _snap = loaded if loaded is not None else _defaults()
    _version = int(_snap.get("version") or 0)
    return _snap


def mark_dirty() -> None:
    global _dirty  # noqa: PLW0603
    _dirty = True


def is_dirty() -> bool:
    return _dirty


async def ensure_fresh() -> dict:
    """Refresh once when an operator write invalidated the snapshot.
    Steady state: pure memory read."""
    if _dirty or _snap is None:
        return await refresh_from_atlas()
    return _snap


def apply_local(**fields: Any) -> dict:
    """Immediate write-through for safety-critical flips (broker
    freeze/thaw). Memory + SQLite commit; Atlas refresh follows."""
    global _snap, _version  # noqa: PLW0603
    base = dict(get())
    base.update(fields)
    _version += 1
    base["version"] = _version
    base["refreshed_at"] = _iso()
    try:
        _persist(base)
    except Exception as exc:  # noqa: BLE001
        logger.warning("policy snapshot persist failed: %s", exc)
    _snap = base
    return base


async def refresh_from_atlas() -> dict:
    """Pull all gate flags from Atlas, atomic-swap the snapshot,
    persist to SQLite. Per-key failure keeps the last-known-good
    value for that key (marked in `degraded_keys`)."""
    global _snap, _version, _dirty  # noqa: PLW0603
    _dirty = False
    from db import db  # noqa: WPS433
    from namespaces import BROKER_FREEZE_STATE  # noqa: WPS433
    from shared.opportunity.policy import _merge  # noqa: WPS433

    base = dict(get())
    degraded: list[str] = []

    async def _read(coll: str, q: dict, proj: Optional[dict] = None):
        return await asyncio.wait_for(
            db[coll].find_one(q, proj), timeout=_READ_TIMEOUT_SEC,
        )

    try:
        doc = await _read("runtime_flags", {"_id": "risk_caps"}, {"cap_daily_usd": 1})
        v = (doc or {}).get("cap_daily_usd")
        base["cap_daily_usd_override"] = float(v) if v is not None else None
    except Exception:  # noqa: BLE001
        degraded.append("cap_daily_usd_override")

    try:
        doc = await _read("runtime_flags", {"_id": "master_trading_switch"},
                          {"_id": 0, "enabled": 1})
        base["master_switch_enabled"] = True if not doc else bool(doc.get("enabled"))
    except Exception:  # noqa: BLE001
        degraded.append("master_switch_enabled")

    try:
        doc = await _read("runtime_flags", {"_id": "lane_enabled"}, {"_id": 0})
        doc = doc or {}
        base["lane_enabled"] = {
            lane: True if doc.get(lane) is None else bool(doc.get(lane))
            for lane in ("equity", "crypto")
        }
    except Exception:  # noqa: BLE001
        degraded.append("lane_enabled")

    try:
        doc = await _read(BROKER_FREEZE_STATE, {"_id": "current"}, {"_id": 0})
        base["broker_frozen"] = bool((doc or {}).get("frozen", False))
        base["broker_freeze_reason"] = (doc or {}).get("reason")
    except Exception:  # noqa: BLE001
        degraded.append("broker_frozen")

    try:
        doc = await _read("runtime_flags", {"_id": "conviction_floor"}, {"value": 1})
        if doc and doc.get("value") is not None:
            base["conviction_floor"] = max(0.0, min(1.0, float(doc["value"])))
        else:
            base["conviction_floor"] = None
    except Exception:  # noqa: BLE001
        degraded.append("conviction_floor")

    try:
        doc = await _read("runtime_flags", {"_id": "opportunity_policy"}, {"_id": 0})
        base["opportunity_policy"] = _merge(doc or {})
    except Exception:  # noqa: BLE001
        degraded.append("opportunity_policy")

    try:
        doc = await _read("runtime_flags", {"_id": "daily_spend_reset"}, {"reset_at": 1})
        base["daily_spend_reset_at"] = (doc or {}).get("reset_at")
    except Exception:  # noqa: BLE001
        degraded.append("daily_spend_reset_at")

    try:
        doc = await _read("runtime_flags", {"_id": "gain_goal_state"},
                          {"_id": 0, "block": 1, "throttle": 1})
        base["gain_goal"] = {
            "block": (doc or {}).get("block") or {},
            "throttle": (doc or {}).get("throttle") or {},
        }
    except Exception:  # noqa: BLE001
        degraded.append("gain_goal")

    _version += 1
    base["version"] = _version
    base["refreshed_at"] = _iso()
    base["source"] = "atlas" if not degraded else "atlas_partial"
    base["degraded_keys"] = degraded
    try:
        _persist(base)
    except Exception as exc:  # noqa: BLE001
        logger.warning("policy snapshot persist failed: %s", exc)
    _snap = base
    _state["last_refresh_at"] = base["refreshed_at"]
    _state["last_error"] = f"degraded: {degraded}" if degraded else None

    try:
        from shared.hotpath import daily_spend  # noqa: WPS433
        daily_spend.observe_reset_marker(base.get("daily_spend_reset_at"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("daily_spend reset-marker reconcile failed: %s", exc)
    return base


# ── effective sync accessors (hot path) ─────────────────────────────

def _env_daily_cap() -> float:
    try:
        return float(os.environ.get("RISEDUAL_CAP_DAILY_USD", "1000"))
    except (TypeError, ValueError):
        return 1000.0


def effective_daily_cap() -> float:
    ov = get().get("cap_daily_usd_override")
    return float(ov) if ov is not None else _env_daily_cap()


def is_lane_enabled(lane: str) -> bool:
    val = (get().get("lane_enabled") or {}).get((lane or "").lower())
    return True if val is None else bool(val)


def is_freeze_on() -> bool:
    """Master Trading Switch — True means trading is FROZEN."""
    return not bool(get().get("master_switch_enabled", True))


def is_broker_frozen() -> bool:
    return bool(get().get("broker_frozen", False))


def get_status() -> dict:
    snap = get()
    return {
        "version": snap.get("version"),
        "source": snap.get("source"),
        "refreshed_at": snap.get("refreshed_at"),
        "dirty": _dirty,
        "degraded_keys": snap.get("degraded_keys") or [],
        "cap_daily_usd_effective": effective_daily_cap(),
        "cap_daily_usd_override": snap.get("cap_daily_usd_override"),
        "master_switch_enabled": snap.get("master_switch_enabled"),
        "lane_enabled": snap.get("lane_enabled"),
        "broker_frozen": snap.get("broker_frozen"),
        "conviction_floor": snap.get("conviction_floor"),
        "daily_spend_reset_at": snap.get("daily_spend_reset_at"),
        "refresher": {
            "running": _state.get("running", False),
            "interval_sec": REFRESH_INTERVAL_SEC,
            "started_at": _state.get("started_at"),
            "last_refresh_at": _state.get("last_refresh_at"),
            "last_error": _state.get("last_error"),
        },
    }


# ── refresher loop / lifecycle ──────────────────────────────────────

async def _loop() -> None:
    logger.info("policy snapshot refresher start interval=%.0fs", REFRESH_INTERVAL_SEC)
    while True:
        try:
            await refresh_from_atlas()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            _state["last_error"] = str(exc)[:300]
            logger.warning("policy snapshot refresh failed: %s", exc)
        await asyncio.sleep(REFRESH_INTERVAL_SEC)


def start_if_enabled() -> None:
    if (os.environ.get("POLICY_SNAPSHOT_ENABLED") or "true").strip().lower() in (
        "0", "false", "no", "off",
    ):
        logger.info("policy snapshot refresher disabled")
        return
    if _state.get("running"):
        return
    task = asyncio.get_event_loop().create_task(_loop(), name="policy_snapshot_refresher")
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

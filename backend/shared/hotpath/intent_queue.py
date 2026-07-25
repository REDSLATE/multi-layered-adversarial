"""Local durable intent queue — router pick without Atlas (audit P2 #6).

Every `shared_intents.insert_one` also enqueues here (memory +
SQLite). The auto-router picks its next executable intents from this
store; Atlas keeps the full intent history asynchronously (existing
write path unchanged). An Atlas outage can no longer stop the router
from discovering work.

Semantics mirror the legacy Atlas pick query exactly: newest-first by
ingest_ts, lookback-bounded, terminal gate_states excluded, poisoned
(3× route-timeout) intents excluded.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from shared.hotpath import outbox

logger = logging.getLogger("risedual.hotpath.intent_queue")

ROUTABLE_ACTIONS = ("BUY", "SELL", "SHORT", "COVER")
DEFAULT_EXCLUDE_STATES = (
    "blocked", "no_trade", "advisory_only", "submitted", "expired_unrouted",
)
_PRUNE_AGE_HOURS = 24.0
_PRUNE_MIN_INTERVAL_S = 600.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS intent_queue (
    intent_id TEXT PRIMARY KEY,
    ingest_ts TEXT NOT NULL,
    lane TEXT,
    symbol TEXT,
    action TEXT,
    gate_state TEXT NOT NULL DEFAULT '',
    executed INTEGER NOT NULL DEFAULT 0,
    route_timeouts INTEGER NOT NULL DEFAULT 0,
    payload_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_intent_queue_pick
    ON intent_queue (executed, ingest_ts);
"""

_schema_for: Optional[str] = None
_cache: dict[str, dict] = {}
_cache_loaded = False
_bootstrapped = False
_last_prune_at = 0.0


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
    global _schema_for, _cache_loaded, _bootstrapped, _last_prune_at  # noqa: PLW0603
    _schema_for = None
    _cache.clear()
    _cache_loaded = False
    _bootstrapped = True  # tests never bootstrap from Atlas
    _last_prune_at = 0.0


def _row_to_entry(r) -> dict:
    return {
        "intent_id": r["intent_id"],
        "ingest_ts": r["ingest_ts"],
        "lane": r["lane"],
        "symbol": r["symbol"],
        "action": r["action"],
        "gate_state": r["gate_state"] or "",
        "executed": bool(r["executed"]),
        "route_timeouts": int(r["route_timeouts"] or 0),
        "payload": json.loads(r["payload_json"]),
    }


def _load_cache() -> None:
    global _cache_loaded  # noqa: PLW0603
    if _cache_loaded:
        return
    cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=_PRUNE_AGE_HOURS)
    ).isoformat()
    rows = _conn().execute(
        "SELECT * FROM intent_queue WHERE ingest_ts >= ?", (cutoff,),
    ).fetchall()
    _cache.clear()
    for r in rows:
        _cache[r["intent_id"]] = _row_to_entry(r)
    _cache_loaded = True
    logger.info("intent_queue cache loaded: %d intents", len(_cache))


def _persist(entry: dict) -> None:
    conn = _conn()
    with conn:
        conn.execute(
            "INSERT INTO intent_queue (intent_id, ingest_ts, lane, symbol, "
            "action, gate_state, executed, route_timeouts, payload_json, "
            "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(intent_id) DO UPDATE SET "
            "gate_state=excluded.gate_state, executed=excluded.executed, "
            "route_timeouts=excluded.route_timeouts, "
            "payload_json=excluded.payload_json, updated_at=excluded.updated_at",
            (entry["intent_id"], entry["ingest_ts"], entry["lane"],
             entry["symbol"], entry["action"], entry["gate_state"],
             1 if entry["executed"] else 0, entry["route_timeouts"],
             json.dumps(entry["payload"], default=str), _iso()),
        )


def enqueue(doc: dict) -> None:
    """Mirror a freshly ingested intent locally. Called right after
    every `shared_intents.insert_one`."""
    _load_cache()
    payload = {k: v for k, v in doc.items() if k != "_id"}
    iid = payload.get("intent_id")
    if not iid:
        return
    entry = {
        "intent_id": iid,
        "ingest_ts": str(payload.get("ingest_ts") or _iso()),
        "lane": (payload.get("lane") or "").lower() or None,
        "symbol": payload.get("symbol"),
        "action": (payload.get("action") or "").upper() or None,
        "gate_state": str(payload.get("gate_state") or ""),
        "executed": bool(payload.get("executed")),
        "route_timeouts": int(payload.get("route_timeouts") or 0),
        "payload": payload,
    }
    _persist(entry)
    _cache[iid] = entry


def enqueue_safe(doc: dict) -> None:
    """Best-effort enqueue — local mirror failure must NEVER block
    intent ingestion."""
    try:
        enqueue(doc)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "intent_queue enqueue failed intent=%s: %s",
            (doc or {}).get("intent_id"), exc,
        )


def pick(
    limit: int = 5,
    lookback_min: int = 60,
    exclude_states: tuple = DEFAULT_EXCLUDE_STATES,
    poison_limit: int = 3,
) -> list[dict]:
    """Next executable intents, newest first. Pure memory. Mirrors
    the legacy Atlas query semantics bit-for-bit."""
    _load_cache()
    _maybe_prune()
    cutoff = (
        datetime.now(timezone.utc) - timedelta(minutes=lookback_min)
    ).isoformat()
    out = []
    for e in _cache.values():
        if e["executed"]:
            continue
        if e["gate_state"] in exclude_states:
            continue
        if e["route_timeouts"] >= poison_limit:
            continue
        if e["ingest_ts"] < cutoff:
            continue
        if e["action"] not in ROUTABLE_ACTIONS or not e["symbol"]:
            continue
        out.append(e)
    out.sort(key=lambda e: e["ingest_ts"], reverse=True)
    picked = []
    for e in out[: max(0, int(limit))]:
        p = dict(e["payload"])
        p["executed"] = e["executed"]
        p["gate_state"] = e["gate_state"] or p.get("gate_state")
        p["route_timeouts"] = e["route_timeouts"]
        picked.append(p)
    return picked


def mark(
    intent_id: str,
    *,
    gate_state: Optional[str] = None,
    executed: Optional[bool] = None,
) -> None:
    _load_cache()
    e = _cache.get(intent_id)
    if e is None:
        row = _conn().execute(
            "SELECT * FROM intent_queue WHERE intent_id=?", (intent_id,),
        ).fetchone()
        if row is None:
            return
        e = _row_to_entry(row)
        _cache[intent_id] = e
    if gate_state is not None:
        e["gate_state"] = gate_state
        e["payload"]["gate_state"] = gate_state
    if executed is not None:
        e["executed"] = bool(executed)
        e["payload"]["executed"] = bool(executed)
    _persist(e)


def mark_safe(intent_id: str, **kwargs: Any) -> None:
    try:
        mark(intent_id, **kwargs)
    except Exception as exc:  # noqa: BLE001
        logger.warning("intent_queue mark failed intent=%s: %s", intent_id, exc)


_VERDICT_TO_STATE = {
    "executed": ("submitted", True),
    "blocked": ("blocked", None),
    "advisory_only": ("advisory_only", None),
    "no_trade": ("no_trade", None),
}


def mark_from_verdict(intent_id: str, verdict: Optional[dict]) -> None:
    """Map a `_route_one` verdict onto the local queue so terminal
    intents are never re-picked. `error` verdicts stay pending
    (broker retry on the next tick — same as Atlas behavior)."""
    v = (verdict or {}).get("verdict")
    mapping = _VERDICT_TO_STATE.get(v)
    if mapping is None:
        return
    state, executed = mapping
    mark_safe(intent_id, gate_state=state, executed=executed)


def bump_route_timeout(intent_id: str, poison_limit: int = 3) -> int:
    """Local mirror of the per-intent route-timeout strike counter.
    At `poison_limit` the intent is terminally blocked."""
    _load_cache()
    e = _cache.get(intent_id)
    if e is None:
        return 0
    e["route_timeouts"] += 1
    e["payload"]["route_timeouts"] = e["route_timeouts"]
    if e["route_timeouts"] >= poison_limit:
        e["gate_state"] = "blocked"
        e["payload"]["gate_state"] = "blocked"
    try:
        _persist(e)
    except Exception as exc:  # noqa: BLE001
        logger.warning("intent_queue timeout persist failed: %s", exc)
    return e["route_timeouts"]


def is_executed(intent_id: str) -> bool:
    """Sync concurrency double-check for the risk gate — replaces the
    per-intent Atlas find_one."""
    _load_cache()
    e = _cache.get(intent_id)
    return bool(e and e["executed"])


def _maybe_prune() -> None:
    global _last_prune_at  # noqa: PLW0603
    now = time.monotonic()
    if now - _last_prune_at < _PRUNE_MIN_INTERVAL_S:
        return
    _last_prune_at = now
    cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=_PRUNE_AGE_HOURS)
    ).isoformat()
    try:
        conn = _conn()
        with conn:
            conn.execute("DELETE FROM intent_queue WHERE ingest_ts < ?", (cutoff,))
        stale = [k for k, e in _cache.items() if e["ingest_ts"] < cutoff]
        for k in stale:
            _cache.pop(k, None)
    except Exception as exc:  # noqa: BLE001
        logger.warning("intent_queue prune failed: %s", exc)


async def bootstrap() -> dict:
    """One-time Atlas → SQLite import so intents ingested before this
    store shipped (or while the pod was down) remain routable."""
    global _bootstrapped  # noqa: PLW0603
    if _bootstrapped:
        return {"skipped": "already_bootstrapped"}
    _bootstrapped = True
    imported = 0
    try:
        from db import db  # noqa: WPS433
        from namespaces import SHARED_INTENTS  # noqa: WPS433
        cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=_PRUNE_AGE_HOURS)
        ).isoformat()
        cursor = (
            db[SHARED_INTENTS]
            .find({"ingest_ts": {"$gte": cutoff}}, {"_id": 0})
            .sort("ingest_ts", -1)
            .max_time_ms(8000)
        )
        _load_cache()
        async for doc in cursor:
            iid = doc.get("intent_id")
            if not iid or iid in _cache:
                continue
            enqueue(doc)
            imported += 1
            if imported >= 2000:
                break
    except Exception as exc:  # noqa: BLE001
        logger.warning("intent_queue bootstrap from Atlas failed: %s", exc)
    logger.info("intent_queue bootstrap imported %d intents", imported)
    return {"imported": imported}


def get_status() -> dict:
    _load_cache()
    pending = sum(
        1 for e in _cache.values()
        if not e["executed"] and e["gate_state"] not in DEFAULT_EXCLUDE_STATES
    )
    states: dict[str, int] = {}
    for e in _cache.values():
        key = "executed" if e["executed"] else (e["gate_state"] or "pending")
        states[key] = states.get(key, 0) + 1
    return {
        "cached": len(_cache),
        "pending": pending,
        "by_state": states,
        "bootstrapped": _bootstrapped,
    }

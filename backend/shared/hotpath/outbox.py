"""Durable SQLite Atlas outbox — write-behind queue (2026-07-23).

Operator doctrine (PRD pin): broker/outcome results commit LOCALLY
first (atomic SQLite insert), Atlas receives events asynchronously
with retry + idempotency. An Atlas outage can no longer drop learning
records, expectancy rows, or permanent receipts.

JSONL (`outbox_audit.jsonl`, same dir) is an emergency append-only
audit trail — never the retry engine.

Idempotency: `id` is the primary key; callers pass a deterministic
event_id (e.g. `exit_outcome:<plan_id>`) and duplicate enqueues are
INSERT OR IGNOREd. Handlers must also be idempotent because a crash
between apply and ack replays the event.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger("risedual.hotpath.outbox")

_DB_PATH = os.environ.get("HOTPATH_DB_PATH", "/app/backend/data/hotpath.sqlite")
MAX_ATTEMPTS = int(os.environ.get("OUTBOX_MAX_ATTEMPTS", "12"))
INTERVAL_SEC = float(os.environ.get("OUTBOX_WRITER_INTERVAL_SEC", "5"))

_conn: Optional[sqlite3.Connection] = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS atlas_outbox (
    id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    atlas_acked_at TEXT,
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_outbox_pending
    ON atlas_outbox (atlas_acked_at, next_attempt_at);
"""


def _connect() -> sqlite3.Connection:
    global _conn  # noqa: PLW0603
    if _conn is None:
        Path(_DB_PATH).parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.executescript(_SCHEMA)
    return _conn


def reset_for_tests(path: str) -> None:
    global _conn, _DB_PATH  # noqa: PLW0603
    if _conn is not None:
        _conn.close()
    _conn = None
    _DB_PATH = str(path)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime] = None) -> str:
    return (dt or _now()).isoformat()


def _audit_line(record: dict) -> None:
    """Emergency append-only JSONL trail. Best-effort."""
    try:
        path = Path(_DB_PATH).parent / "outbox_audit.jsonl"
        with path.open("a") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except Exception:  # noqa: BLE001
        pass


# ── handler registry ────────────────────────────────────────────────

HANDLERS: dict[str, Callable[[str, dict], Awaitable[None]]] = {}


def register_handler(
    event_type: str, fn: Callable[[str, dict], Awaitable[None]],
) -> None:
    HANDLERS[event_type] = fn


# ── enqueue / drain ─────────────────────────────────────────────────

def enqueue(
    event_type: str,
    aggregate_id: str,
    payload: dict[str, Any],
    event_id: Optional[str] = None,
) -> str:
    """Atomic local commit of an Atlas-bound event. Deterministic
    event_id → duplicate enqueues are no-ops (idempotency key)."""
    eid = event_id or f"{event_type}:{aggregate_id}"
    conn = _connect()
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO atlas_outbox "
            "(id, event_type, aggregate_id, payload_json, created_at) "
            "VALUES (?,?,?,?,?)",
            (eid, event_type, aggregate_id,
             json.dumps(payload, default=str), _iso()),
        )
    _audit_line({"id": eid, "event_type": event_type,
                 "aggregate_id": aggregate_id, "ts": _iso(),
                 "payload": payload})
    return eid


async def drain_once(limit: int = 200) -> dict:
    """Apply due, un-acked, non-dead events to Atlas via handlers."""
    conn = _connect()
    now = _iso()
    rows = conn.execute(
        "SELECT * FROM atlas_outbox WHERE atlas_acked_at IS NULL "
        "AND attempt_count < ? "
        "AND (next_attempt_at IS NULL OR next_attempt_at <= ?) "
        "ORDER BY created_at LIMIT ?",
        (MAX_ATTEMPTS, now, limit),
    ).fetchall()
    applied = failed = 0
    for r in rows:
        handler = HANDLERS.get(r["event_type"])
        try:
            if handler is None:
                raise RuntimeError(f"no handler for {r['event_type']}")
            await handler(r["id"], json.loads(r["payload_json"]))
            with conn:
                conn.execute(
                    "UPDATE atlas_outbox SET atlas_acked_at=?, "
                    "last_error=NULL WHERE id=?",
                    (_iso(), r["id"]),
                )
            applied += 1
        except Exception as exc:  # noqa: BLE001
            attempts = r["attempt_count"] + 1
            backoff = min(600.0, 5.0 * (2 ** attempts))
            with conn:
                conn.execute(
                    "UPDATE atlas_outbox SET attempt_count=?, "
                    "next_attempt_at=?, last_error=? WHERE id=?",
                    (attempts, _iso(_now() + timedelta(seconds=backoff)),
                     str(exc)[:400], r["id"]),
                )
            failed += 1
            logger.warning(
                "outbox apply failed id=%s type=%s attempt=%d: %s",
                r["id"], r["event_type"], attempts, str(exc)[:200],
            )
    summary = {"selected": len(rows), "applied": applied, "failed": failed}
    _state["last_drain_at"] = _iso()
    _state["last_drain"] = summary
    return summary


def retry_dead_letters() -> int:
    """Operator reset: dead letters get a fresh attempt budget."""
    conn = _connect()
    with conn:
        cur = conn.execute(
            "UPDATE atlas_outbox SET attempt_count=0, next_attempt_at=NULL "
            "WHERE atlas_acked_at IS NULL AND attempt_count >= ?",
            (MAX_ATTEMPTS,),
        )
    return cur.rowcount


def dead_letters(limit: int = 20) -> list[dict]:
    conn = _connect()
    rows = conn.execute(
        "SELECT id, event_type, aggregate_id, created_at, attempt_count, "
        "last_error FROM atlas_outbox WHERE atlas_acked_at IS NULL "
        "AND attempt_count >= ? ORDER BY created_at DESC LIMIT ?",
        (MAX_ATTEMPTS, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def get_status() -> dict:
    conn = _connect()

    def one(sql: str, *args) -> Any:
        row = conn.execute(sql, args).fetchone()
        return row[0] if row else None

    return {
        "db_path": _DB_PATH,
        "pending": one(
            "SELECT COUNT(*) FROM atlas_outbox WHERE atlas_acked_at IS NULL "
            "AND attempt_count < ?", MAX_ATTEMPTS,
        ),
        "dead_letter": one(
            "SELECT COUNT(*) FROM atlas_outbox WHERE atlas_acked_at IS NULL "
            "AND attempt_count >= ?", MAX_ATTEMPTS,
        ),
        "acked_total": one(
            "SELECT COUNT(*) FROM atlas_outbox WHERE atlas_acked_at IS NOT NULL",
        ),
        "oldest_pending_at": one(
            "SELECT created_at FROM atlas_outbox WHERE atlas_acked_at IS NULL "
            "ORDER BY created_at LIMIT 1",
        ),
        "last_error": one(
            "SELECT last_error FROM atlas_outbox WHERE last_error IS NOT NULL "
            "AND atlas_acked_at IS NULL ORDER BY created_at DESC LIMIT 1",
        ),
        "max_attempts": MAX_ATTEMPTS,
        "writer": {
            "running": _state.get("running", False),
            "interval_sec": INTERVAL_SEC,
            "started_at": _state.get("started_at"),
            "last_drain_at": _state.get("last_drain_at"),
            "last_drain": _state.get("last_drain"),
        },
    }


# ── writer loop / lifecycle ─────────────────────────────────────────

_state: dict[str, Any] = {
    "running": False, "task": None, "started_at": None,
    "last_drain_at": None, "last_drain": None,
}


async def _loop() -> None:
    logger.info("outbox writer start interval=%.0fs db=%s", INTERVAL_SEC, _DB_PATH)
    while True:
        try:
            await drain_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("outbox drain tick failed: %s", exc)
        await asyncio.sleep(INTERVAL_SEC)


def start_if_enabled() -> None:
    if (os.environ.get("OUTBOX_WRITER_ENABLED") or "true").strip().lower() in (
        "0", "false", "no", "off",
    ):
        logger.info("outbox writer disabled via OUTBOX_WRITER_ENABLED")
        return
    if _state.get("running"):
        return
    task = asyncio.get_event_loop().create_task(_loop(), name="atlas_outbox_writer")
    _state.update(running=True, task=task, started_at=_iso())
    logger.info("outbox writer started")


async def stop() -> None:
    task = _state.get("task")
    if task and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _state.update(running=False, task=None)

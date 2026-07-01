"""Trader storage — local truth, best-effort Mongo mirror.

Doctrine pin (2026-07-01, operator directive):
    "No database before broker submit. Local receipt first. Small
    transactional DB second. Mongo third."

This module owns three layers of persistence, in strict priority
order. Every write goes through the pipeline top-to-bottom, and
each stage is independently durable — failure in a later stage
never undoes an earlier stage.

    1. JSONL (append-only file)     — the fastest possible durable
                                      record. One `open('a').write`
                                      + fsync per row. Written
                                      first so the trader can never
                                      lose a broker fill even if
                                      the process is killed the
                                      next microsecond.

    2. SQLite (local file)          — the truth tape. Transactional,
                                      indexable, small. Powers the
                                      risk module's `spent_today`
                                      + idempotency queries. Never
                                      leaves this machine.

    3. Mongo (best-effort mirror)   — fire-and-forget async queue.
                                      For MC's dashboards + long-
                                      term analytics. If Atlas is
                                      down we DROP the mirror,
                                      log a warning, and keep
                                      trading. Trader authority
                                      does not depend on this.

The `record_execution` and `record_receipt` calls are synchronous
from the caller's perspective (JSONL + SQLite happen inline in ~1ms)
but the Mongo mirror is enqueued and drained by
`mongo_mirror_worker()` running as an asyncio task.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional


logger = logging.getLogger("trader.store")

# Module-level state — the trader is a single process; one connection,
# one lock, one mirror queue is sufficient and simpler than DI.
_conn: Optional[sqlite3.Connection] = None
_lock = threading.Lock()
_mirror_q: Optional[asyncio.Queue] = None
_jsonl_dir: Optional[Path] = None
_MIRROR_Q_MAX = 10_000


# ─── schema ────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS executions (
    intent_id           TEXT PRIMARY KEY,
    ts                  TEXT NOT NULL,
    brain               TEXT,
    lane                TEXT,
    action              TEXT,
    symbol              TEXT,
    notional_usd        REAL NOT NULL DEFAULT 0,
    risk_multiplier     REAL NOT NULL DEFAULT 1,
    decision            TEXT,
    seats_json          TEXT,
    angels_json         TEXT,
    risk_ok             INTEGER NOT NULL DEFAULT 0,
    risk_reason         TEXT,
    broker              TEXT,
    broker_order_id     TEXT,
    broker_status       TEXT,
    broker_response_json TEXT,
    exception_type      TEXT,
    exception_msg       TEXT,
    ok                  INTEGER NOT NULL DEFAULT 0,
    mongo_synced        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_executions_ts ON executions(ts);
CREATE INDEX IF NOT EXISTS idx_executions_ok_ts ON executions(ok, ts);
CREATE INDEX IF NOT EXISTS idx_executions_mongo_synced ON executions(mongo_synced);

CREATE TABLE IF NOT EXISTS trader_receipts (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id            TEXT NOT NULL,
    ts                  TEXT NOT NULL,
    lane                TEXT,
    symbol              TEXT,
    last_price          REAL,
    signals_json        TEXT,
    chosen_json         TEXT,
    seats_json          TEXT,
    angels_json         TEXT,
    risk_json           TEXT,
    broker_result_json  TEXT,
    error               TEXT,
    mongo_synced        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_receipts_ts ON trader_receipts(ts);
CREATE INDEX IF NOT EXISTS idx_receipts_lane_ts ON trader_receipts(lane, ts);
CREATE INDEX IF NOT EXISTS idx_receipts_mongo_synced ON trader_receipts(mongo_synced);

CREATE TABLE IF NOT EXISTS seat_cache (
    seat_id             TEXT PRIMARY KEY,   -- e.g. "equity:executor"
    lane                TEXT NOT NULL,
    role                TEXT NOT NULL,
    holder              TEXT,
    risk_multiplier     REAL,
    updated_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS flags_cache (
    key                 TEXT PRIMARY KEY,
    value_json          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today_prefix() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ─── init ──────────────────────────────────────────────────────────

def init(sqlite_path: str, jsonl_dir: str) -> None:
    """Initialize the local store. Idempotent — safe to call twice."""
    global _conn, _mirror_q, _jsonl_dir

    Path(sqlite_path).parent.mkdir(parents=True, exist_ok=True)
    Path(jsonl_dir).mkdir(parents=True, exist_ok=True)
    _jsonl_dir = Path(jsonl_dir)

    conn = sqlite3.connect(
        sqlite_path,
        check_same_thread=False,
        isolation_level=None,   # autocommit; we manage txns via BEGIN
        timeout=5.0,
    )
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_SCHEMA)
    _conn = conn

    _mirror_q = asyncio.Queue(maxsize=_MIRROR_Q_MAX)
    logger.info(
        "trader.store initialized sqlite=%s jsonl_dir=%s",
        sqlite_path, jsonl_dir,
    )


def _require_conn() -> sqlite3.Connection:
    if _conn is None:
        raise RuntimeError("trader.store.init() has not been called")
    return _conn


def _require_q() -> asyncio.Queue:
    if _mirror_q is None:
        raise RuntimeError("trader.store.init() has not been called")
    return _mirror_q


# ─── JSONL helpers ─────────────────────────────────────────────────

def _append_jsonl(filename: str, row: dict) -> None:
    """Append one row to a JSONL file. Never raises — logs and moves
    on. Broker truth is authoritative; a disk hiccup can't undo it."""
    if _jsonl_dir is None:
        return
    try:
        path = _jsonl_dir / filename
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str))
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
    except Exception as e:  # noqa: BLE001
        logger.error("jsonl append failed file=%s err=%s", filename, e)


# ─── executions ────────────────────────────────────────────────────

def record_execution(row: dict) -> None:
    """JSONL → SQLite → enqueue Mongo mirror. Never raises.

    `row` must contain `intent_id` (str, unique) and `ts` (iso str).
    Other fields are optional; missing fields default to None/0.
    """
    _append_jsonl("executions.jsonl", row)

    try:
        with _lock:
            _require_conn().execute(
                """
                INSERT OR REPLACE INTO executions (
                    intent_id, ts, brain, lane, action, symbol,
                    notional_usd, risk_multiplier, decision,
                    seats_json, angels_json,
                    risk_ok, risk_reason,
                    broker, broker_order_id, broker_status,
                    broker_response_json,
                    exception_type, exception_msg,
                    ok, mongo_synced
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)
                """,
                (
                    row["intent_id"], row["ts"],
                    row.get("brain"), row.get("lane"),
                    row.get("action"), row.get("symbol"),
                    float(row.get("notional_usd") or 0.0),
                    float(row.get("risk_multiplier") or 1.0),
                    row.get("decision"),
                    json.dumps(row.get("seats") or {}, default=str),
                    json.dumps(row.get("angels") or {}, default=str),
                    1 if row.get("risk_ok") else 0,
                    row.get("risk_reason"),
                    row.get("broker"), row.get("broker_order_id"),
                    row.get("broker_status"),
                    json.dumps(row.get("broker_response"), default=str)
                        if row.get("broker_response") is not None else None,
                    row.get("exception_type"),
                    row.get("exception_msg"),
                    1 if row.get("ok") else 0,
                ),
            )
    except Exception as e:  # noqa: BLE001
        logger.error("sqlite executions insert failed intent=%s err=%s",
                     row.get("intent_id"), e)

    _enqueue_mirror("executions", row)


def record_receipt(row: dict) -> int:
    """JSONL → SQLite → enqueue Mongo mirror. Returns the SQLite
    rowid (0 on failure). Never raises."""
    _append_jsonl("receipts.jsonl", row)

    rowid = 0
    try:
        with _lock:
            cur = _require_conn().execute(
                """
                INSERT INTO trader_receipts (
                    cycle_id, ts, lane, symbol, last_price,
                    signals_json, chosen_json, seats_json,
                    angels_json, risk_json, broker_result_json,
                    error, mongo_synced
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0)
                """,
                (
                    row.get("cycle_id"), row["ts"],
                    row.get("lane"), row.get("symbol"),
                    row.get("last_price"),
                    json.dumps(row.get("signals") or [], default=str),
                    json.dumps(row.get("chosen"), default=str)
                        if row.get("chosen") is not None else None,
                    json.dumps(row.get("seats") or {}, default=str),
                    json.dumps(row.get("angels") or {}, default=str),
                    json.dumps(row.get("risk") or {}, default=str),
                    json.dumps(row.get("broker_result"), default=str)
                        if row.get("broker_result") is not None else None,
                    row.get("error"),
                ),
            )
            rowid = cur.lastrowid or 0
    except Exception as e:  # noqa: BLE001
        logger.error("sqlite receipts insert failed cycle=%s err=%s",
                     row.get("cycle_id"), e)

    _enqueue_mirror("trader_receipts", row)
    return rowid


# ─── risk queries (SQLite is authoritative) ────────────────────────

def already_executed(intent_id: str) -> bool:
    if not intent_id:
        return False
    with _lock:
        cur = _require_conn().execute(
            "SELECT 1 FROM executions WHERE intent_id = ? AND ok = 1",
            (intent_id,),
        )
        return cur.fetchone() is not None


def daily_spent_usd() -> float:
    prefix = _today_prefix() + "%"
    with _lock:
        cur = _require_conn().execute(
            """
            SELECT COALESCE(SUM(notional_usd), 0.0)
            FROM executions
            WHERE ok = 1 AND ts LIKE ?
            """,
            (prefix,),
        )
        (spent,) = cur.fetchone()
        return float(spent or 0.0)


# ─── seat / flag cache persistence (SQLite is fallback for boot) ───

def upsert_seat_cache(seat_id: str, lane: str, role: str,
                      holder: Optional[str],
                      risk_multiplier: Optional[float]) -> None:
    with _lock:
        _require_conn().execute(
            """
            INSERT INTO seat_cache (seat_id, lane, role, holder,
                                    risk_multiplier, updated_at)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(seat_id) DO UPDATE SET
                holder = excluded.holder,
                risk_multiplier = excluded.risk_multiplier,
                updated_at = excluded.updated_at
            """,
            (seat_id, lane, role, holder, risk_multiplier, _now_iso()),
        )


def read_seat_cache() -> dict[str, dict]:
    """Return {seat_id: {holder, risk_multiplier}}."""
    with _lock:
        cur = _require_conn().execute(
            "SELECT seat_id, lane, role, holder, risk_multiplier "
            "FROM seat_cache"
        )
        return {
            r[0]: {
                "lane": r[1], "role": r[2],
                "holder": r[3], "risk_multiplier": r[4],
            }
            for r in cur.fetchall()
        }


def upsert_flag_cache(key: str, value: Any) -> None:
    with _lock:
        _require_conn().execute(
            """
            INSERT INTO flags_cache (key, value_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value_json = excluded.value_json,
                updated_at = excluded.updated_at
            """,
            (key, json.dumps(value, default=str), _now_iso()),
        )


def read_flag_cache(key: str, default: Any = None) -> Any:
    with _lock:
        cur = _require_conn().execute(
            "SELECT value_json FROM flags_cache WHERE key = ?",
            (key,),
        )
        row = cur.fetchone()
        if not row:
            return default
        try:
            return json.loads(row[0])
        except Exception:  # noqa: BLE001
            return default


# ─── recent-rows reader (powers the MC dashboard fallback) ─────────

def recent_receipts(limit: int = 50, lane: Optional[str] = None,
                    fired_only: bool = False) -> list[dict]:
    where = []
    args: list = []
    if lane:
        where.append("lane = ?")
        args.append(lane.lower())
    if fired_only:
        where.append("chosen_json IS NOT NULL "
                     "AND (chosen_json LIKE '%\"verdict\": \"BUY\"%' "
                     "OR chosen_json LIKE '%\"verdict\": \"SELL\"%')")
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    sql = (f"SELECT cycle_id, ts, lane, symbol, last_price, "
           f"signals_json, chosen_json, seats_json, angels_json, "
           f"risk_json, broker_result_json, error "
           f"FROM trader_receipts {clause} ORDER BY ts DESC LIMIT ?")
    args.append(int(limit))
    with _lock:
        cur = _require_conn().execute(sql, tuple(args))
        rows = cur.fetchall()
    out = []
    for r in rows:
        out.append({
            "cycle_id": r[0], "ts": r[1], "lane": r[2], "symbol": r[3],
            "last_price": r[4],
            "signals": _safe_json(r[5], []),
            "chosen": _safe_json(r[6], None),
            "seats": _safe_json(r[7], {}),
            "angels": _safe_json(r[8], {}),
            "risk": _safe_json(r[9], {}),
            "broker_result": _safe_json(r[10], None),
            "error": r[11],
            "source": "trader",
        })
    return out


def recent_executions(limit: int = 50, lane: Optional[str] = None,
                      ok: Optional[bool] = None) -> list[dict]:
    where = []
    args: list = []
    if lane:
        where.append("lane = ?")
        args.append(lane.lower())
    if ok is not None:
        where.append("ok = ?")
        args.append(1 if ok else 0)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    sql = (f"SELECT intent_id, ts, brain, lane, action, symbol, "
           f"notional_usd, risk_multiplier, decision, seats_json, "
           f"angels_json, risk_ok, risk_reason, broker, "
           f"broker_order_id, broker_status, broker_response_json, "
           f"exception_type, exception_msg, ok "
           f"FROM executions {clause} ORDER BY ts DESC LIMIT ?")
    args.append(int(limit))
    with _lock:
        cur = _require_conn().execute(sql, tuple(args))
        rows = cur.fetchall()
    out = []
    for r in rows:
        out.append({
            "intent_id": r[0], "ts": r[1], "brain": r[2], "lane": r[3],
            "action": r[4], "symbol": r[5],
            "notional_usd": r[6], "risk_multiplier": r[7],
            "decision": r[8],
            "seats": _safe_json(r[9], {}),
            "angels": _safe_json(r[10], {}),
            "risk_ok": bool(r[11]),
            "risk_reason": r[12],
            "broker": r[13], "broker_order_id": r[14],
            "broker_status": r[15],
            "broker_response": _safe_json(r[16], None),
            "exception_type": r[17], "exception_msg": r[18],
            "ok": bool(r[19]),
            "source": "trader",
        })
    return out


def counts() -> dict:
    """Health probe: row counts + pending Mongo-mirror backlog."""
    with _lock:
        c = _require_conn()
        n_exec = c.execute("SELECT COUNT(*) FROM executions").fetchone()[0]
        n_rcpt = c.execute("SELECT COUNT(*) FROM trader_receipts").fetchone()[0]
        pending_exec = c.execute(
            "SELECT COUNT(*) FROM executions WHERE mongo_synced = 0"
        ).fetchone()[0]
        pending_rcpt = c.execute(
            "SELECT COUNT(*) FROM trader_receipts WHERE mongo_synced = 0"
        ).fetchone()[0]
    q = _mirror_q
    return {
        "executions_total": int(n_exec),
        "receipts_total": int(n_rcpt),
        "executions_pending_mongo": int(pending_exec),
        "receipts_pending_mongo": int(pending_rcpt),
        "mirror_queue_size": q.qsize() if q else 0,
        "mirror_queue_max": _MIRROR_Q_MAX,
    }


def prune(days: int, *, keep_pending: bool = True) -> dict:
    """Retention trim. Deletes SQLite rows whose `ts` is older than
    `days` days ago. Mongo mirror (best-effort archive) is the long-
    term store; SQLite only needs to be hot enough for `daily_spent`
    + idempotency + operator dashboards.

    Doctrine pin: never delete a row that hasn't been mirrored to
    Mongo yet unless `keep_pending=False`. Default behavior is
    conservative — if Atlas has been down all week the mirror queue
    is behind, and pruning would silently drop rows that never made
    it to the archive.

    Returns row counts before/after and the cutoff.
    """
    if days < 1:
        raise ValueError(f"days must be >= 1, got {days}")
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=days)
    ).isoformat()
    with _lock:
        c = _require_conn()
        before_exec = c.execute("SELECT COUNT(*) FROM executions").fetchone()[0]
        before_rcpt = c.execute("SELECT COUNT(*) FROM trader_receipts").fetchone()[0]
        if keep_pending:
            # Only prune rows already mirrored to Mongo.
            c.execute(
                "DELETE FROM executions "
                "WHERE ts < ? AND mongo_synced = 1",
                (cutoff,),
            )
            c.execute(
                "DELETE FROM trader_receipts "
                "WHERE ts < ? AND mongo_synced = 1",
                (cutoff,),
            )
        else:
            c.execute("DELETE FROM executions WHERE ts < ?", (cutoff,))
            c.execute("DELETE FROM trader_receipts WHERE ts < ?", (cutoff,))
        after_exec = c.execute("SELECT COUNT(*) FROM executions").fetchone()[0]
        after_rcpt = c.execute("SELECT COUNT(*) FROM trader_receipts").fetchone()[0]
        # Reclaim disk. VACUUM must run outside a transaction; we're
        # in autocommit mode (isolation_level=None) so this is safe.
        try:
            c.execute("VACUUM")
        except sqlite3.OperationalError as e:  # noqa: F841 - can't VACUUM under load
            logger.warning("VACUUM skipped: %s", e)
    return {
        "cutoff_ts": cutoff,
        "keep_pending": keep_pending,
        "executions_before": int(before_exec),
        "executions_after": int(after_exec),
        "executions_deleted": int(before_exec - after_exec),
        "receipts_before": int(before_rcpt),
        "receipts_after": int(after_rcpt),
        "receipts_deleted": int(before_rcpt - after_rcpt),
    }


def _safe_json(s: Optional[str], default: Any) -> Any:
    if s is None:
        return default
    try:
        return json.loads(s)
    except Exception:  # noqa: BLE001
        return default


# ─── Mongo mirror ──────────────────────────────────────────────────

def _enqueue_mirror(collection: str, row: dict) -> None:
    """Non-blocking enqueue. Drops the oldest item on overflow so a
    stalled Mongo never backpressures the trader."""
    q = _mirror_q
    if q is None:
        return
    try:
        q.put_nowait({"collection": collection, "row": row})
    except asyncio.QueueFull:
        try:
            _ = q.get_nowait()   # drop oldest
            q.put_nowait({"collection": collection, "row": row})
            logger.warning("mongo mirror queue overflow — dropped oldest")
        except Exception:  # noqa: BLE001
            logger.error("mongo mirror queue overflow — drop failed; "
                         "new row lost")


async def mongo_mirror_worker(db) -> None:
    """Drain the mirror queue → Mongo. Non-authoritative. Every
    Mongo call is bounded by `asyncio.wait_for` so a hung Atlas
    can never wedge this task. On failure, drop and continue."""
    q = _require_q()
    logger.info("mongo mirror worker started")
    while True:
        try:
            item = await q.get()
        except asyncio.CancelledError:
            logger.info("mongo mirror worker cancelled")
            raise
        collection = item["collection"]
        row = item["row"]
        try:
            if collection == "executions":
                await asyncio.wait_for(
                    db["executions"].replace_one(
                        {"intent_id": row["intent_id"]},
                        {**row, "source": "trader"},
                        upsert=True,
                    ),
                    timeout=3.0,
                )
                await asyncio.to_thread(_mark_synced_exec, row["intent_id"])
            elif collection == "trader_receipts":
                await asyncio.wait_for(
                    db["trader_receipts"].insert_one({**row, "source": "trader"}),
                    timeout=3.0,
                )
                # receipts don't have a natural PK; we use a rough
                # (cycle_id, ts, lane) key to mark synced.
                await asyncio.to_thread(
                    _mark_synced_receipt,
                    row.get("cycle_id"), row.get("ts"), row.get("lane"),
                )
        except asyncio.TimeoutError:
            logger.warning("mongo mirror timeout coll=%s (drop, keep going)",
                           collection)
        except Exception as e:  # noqa: BLE001
            logger.warning("mongo mirror failed coll=%s err=%s",
                           collection, e)


def _mark_synced_exec(intent_id: str) -> None:
    try:
        with _lock:
            _require_conn().execute(
                "UPDATE executions SET mongo_synced = 1 "
                "WHERE intent_id = ?",
                (intent_id,),
            )
    except Exception as e:  # noqa: BLE001
        logger.debug("mark_synced_exec failed intent=%s err=%s",
                     intent_id, e)


def _mark_synced_receipt(cycle_id: Optional[str],
                         ts: Optional[str],
                         lane: Optional[str]) -> None:
    if not cycle_id or not ts:
        return
    try:
        with _lock:
            _require_conn().execute(
                "UPDATE trader_receipts SET mongo_synced = 1 "
                "WHERE cycle_id = ? AND ts = ? AND lane IS ?",
                (cycle_id, ts, lane),
            )
    except Exception as e:  # noqa: BLE001
        logger.debug("mark_synced_receipt failed cycle=%s err=%s",
                     cycle_id, e)


def close() -> None:
    """Shutdown hook. Idempotent."""
    global _conn
    try:
        if _conn is not None:
            _conn.close()
            _conn = None
    except Exception:  # noqa: BLE001
        pass

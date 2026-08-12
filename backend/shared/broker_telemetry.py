"""Broker execution telemetry — SQLite hot store (2026-06 directive).

Comparison telemetry per submission: quote age, spread, trigger
timestamp, submit/ack/fill latency, fill price, slippage, rejection
reason. Lives in the existing outcome-engine SQLite DB; high-frequency
market data is NOT duplicated into MongoDB.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from typing import Optional

logger = logging.getLogger("risedual.broker_telemetry")

_TABLE = """
CREATE TABLE IF NOT EXISTS broker_exec_telemetry (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  broker TEXT NOT NULL,
  symbol TEXT,
  side TEXT,
  trigger_ts TEXT,
  quote_age_ms REAL,
  bid REAL,
  ask REAL,
  spread REAL,
  limit_price REAL,
  submit_latency_ms REAL,
  ack_latency_ms REAL,
  order_id TEXT,
  fill_latency_ms REAL,
  fill_price REAL,
  slippage_pct REAL,
  rejection_reason TEXT
)
"""


def _conn() -> sqlite3.Connection:
    from shared.outcome_engine.store import _get_conn  # noqa: WPS433
    c = _get_conn()
    c.execute(_TABLE)
    return c


async def record_submit(**kw) -> None:
    def _do():
        c = _conn()
        c.execute(
            """INSERT INTO broker_exec_telemetry
               (broker, symbol, side, trigger_ts, quote_age_ms, bid, ask,
                spread, limit_price, submit_latency_ms, ack_latency_ms,
                order_id, rejection_reason)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (kw.get("broker"), kw.get("symbol"), kw.get("side"),
             kw.get("trigger_ts"), kw.get("quote_age_ms"), kw.get("bid"),
             kw.get("ask"), kw.get("spread"), kw.get("limit_price"),
             kw.get("submit_latency_ms"), kw.get("ack_latency_ms"),
             kw.get("order_id"), kw.get("rejection_reason")))
        c.commit()
    try:
        await asyncio.to_thread(_do)
    except Exception as exc:  # noqa: BLE001
        logger.warning("telemetry record failed: %s", exc)


async def record_fill(order_id: str, *, fill_price: float,
                      fill_latency_ms: Optional[float] = None) -> None:
    def _do():
        c = _conn()
        row = c.execute(
            "SELECT limit_price FROM broker_exec_telemetry WHERE "
            "order_id=? ORDER BY id DESC LIMIT 1", (order_id,)).fetchone()
        slip = None
        if row and row[0]:
            slip = round((fill_price - row[0]) / row[0] * 100.0, 6)
        c.execute(
            "UPDATE broker_exec_telemetry SET fill_price=?, "
            "fill_latency_ms=?, slippage_pct=? WHERE order_id=?",
            (fill_price, fill_latency_ms, slip, order_id))
        c.commit()
    try:
        await asyncio.to_thread(_do)
    except Exception as exc:  # noqa: BLE001
        logger.warning("telemetry fill update failed: %s", exc)


async def recent(broker: Optional[str] = None, limit: int = 25) -> list[dict]:
    def _do():
        c = _conn()
        q = "SELECT * FROM broker_exec_telemetry"
        args: tuple = ()
        if broker:
            q += " WHERE broker=?"
            args = (broker,)
        q += " ORDER BY id DESC LIMIT ?"
        cur = c.execute(q, (*args, limit))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    try:
        return await asyncio.to_thread(_do)
    except Exception as exc:  # noqa: BLE001
        logger.warning("telemetry read failed: %s", exc)
        return []

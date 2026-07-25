"""Hot-path candidate cache for the RTH Opportunity Scanner.

Ranked candidates live in SQLite (same DB as the outbox) so Mission
Control's universe consumption never needs a synchronous Atlas read
to know what is worth examining. Advisory data only — no execution
authority lives here.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from shared.hotpath import outbox

_SCHEMA = """
CREATE TABLE IF NOT EXISTS scanner_candidates (
    symbol TEXT PRIMARY KEY,
    score REAL NOT NULL,
    payload_json TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scanner_live ON scanner_candidates (expires_at, score);
"""

_schema_for: Optional[str] = None


def _conn():
    global _schema_for  # noqa: PLW0603
    c = outbox._connect()
    if _schema_for != outbox._DB_PATH:
        c.executescript(_SCHEMA)
        _schema_for = outbox._DB_PATH
    return c


def reset_for_tests() -> None:
    global _schema_for  # noqa: PLW0603
    _schema_for = None


def _iso(dt: Optional[datetime] = None) -> str:
    return (dt or datetime.now(timezone.utc)).isoformat()


def upsert_candidates(cands: list[dict]) -> int:
    conn = _conn()
    with conn:
        for c in cands:
            conn.execute(
                "INSERT INTO scanner_candidates (symbol, score, payload_json, "
                "expires_at, updated_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(symbol) DO UPDATE SET score=excluded.score, "
                "payload_json=excluded.payload_json, "
                "expires_at=excluded.expires_at, updated_at=excluded.updated_at",
                (c["symbol"], float(c["opportunity_score"]),
                 json.dumps(c, default=str), c["expires_at"], _iso()),
            )
    return len(cands)


def invalidate(symbol: str) -> None:
    conn = _conn()
    with conn:
        conn.execute("DELETE FROM scanner_candidates WHERE symbol=?", (symbol.upper(),))


def purge_expired() -> int:
    conn = _conn()
    with conn:
        cur = conn.execute(
            "DELETE FROM scanner_candidates WHERE expires_at <= ?", (_iso(),),
        )
    return cur.rowcount


def live_candidates(limit: int = 25) -> list[dict]:
    rows = _conn().execute(
        "SELECT payload_json FROM scanner_candidates WHERE expires_at > ? "
        "ORDER BY score DESC LIMIT ?", (_iso(), limit),
    ).fetchall()
    return [json.loads(r["payload_json"]) for r in rows]


def status() -> dict:
    conn = _conn()
    now = _iso()
    live = conn.execute(
        "SELECT COUNT(*) FROM scanner_candidates WHERE expires_at > ?", (now,),
    ).fetchone()[0]
    expired = conn.execute(
        "SELECT COUNT(*) FROM scanner_candidates WHERE expires_at <= ?", (now,),
    ).fetchone()[0]
    newest = conn.execute(
        "SELECT MAX(updated_at) FROM scanner_candidates",
    ).fetchone()[0]
    return {"live": live, "expired_pending_purge": expired, "newest_at": newest}

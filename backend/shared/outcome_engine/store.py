"""SQLite hot store for signal outcome records (hot-path doctrine:
local SQLite is the durable primary; Mongo mirror is for dashboards).
"""
from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path
from typing import Any, Optional

_DB_PATH = os.environ.get("OUTCOME_DB_PATH", "/app/backend/data/rise_outcomes.sqlite")
_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS rise_signal_outcomes (
    outcome_id TEXT PRIMARY KEY,
    signal_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    lane TEXT NOT NULL,
    brain TEXT NOT NULL,
    side TEXT NOT NULL,
    signal_time TEXT NOT NULL,
    signal_price REAL NOT NULL,
    confidence REAL NOT NULL,
    theoretical_outcome TEXT NOT NULL,
    theoretical_return_pct REAL,
    executed INTEGER NOT NULL,
    actual_entry_time TEXT,
    actual_entry_price REAL,
    actual_exit_time TEXT,
    actual_exit_price REAL,
    actual_return_pct REAL,
    entry_delay_seconds REAL,
    entry_slippage_pct REAL,
    edge_capture_ratio REAL,
    attribution TEXT NOT NULL,
    gate_rejection_reason TEXT,
    metadata_json TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rise_outcomes_brain_time
    ON rise_signal_outcomes (brain, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_rise_outcomes_signal
    ON rise_signal_outcomes (signal_id);
CREATE INDEX IF NOT EXISTS idx_rise_outcomes_lane_time
    ON rise_signal_outcomes (lane, created_at);
"""


def _get_conn() -> sqlite3.Connection:
    global _conn  # noqa: PLW0603
    if _conn is None:
        Path(_DB_PATH).parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(_DB_PATH, check_same_thread=False, timeout=15)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(_SCHEMA)
        _conn.commit()
    return _conn


_COLS = (
    "outcome_id", "signal_id", "symbol", "lane", "brain", "side",
    "signal_time", "signal_price", "confidence", "theoretical_outcome",
    "theoretical_return_pct", "executed", "actual_entry_time",
    "actual_entry_price", "actual_exit_time", "actual_exit_price",
    "actual_return_pct", "entry_delay_seconds", "entry_slippage_pct",
    "edge_capture_ratio", "attribution", "gate_rejection_reason",
    "metadata_json", "created_at",
)


def save(record: dict[str, Any]) -> None:
    row = {c: record.get(c) for c in _COLS}
    row["executed"] = 1 if record.get("executed") else 0
    with _lock:
        conn = _get_conn()
        conn.execute(
            f"INSERT OR REPLACE INTO rise_signal_outcomes "
            f"({', '.join(_COLS)}) VALUES ({', '.join(':' + c for c in _COLS)})",
            row,
        )
        conn.commit()


def has_signal(signal_id: str) -> bool:
    with _lock:
        cur = _get_conn().execute(
            "SELECT 1 FROM rise_signal_outcomes WHERE signal_id = ? LIMIT 1",
            (signal_id,))
        return cur.fetchone() is not None


def recent(limit: int = 50, brain: Optional[str] = None,
           lane: Optional[str] = None) -> list[dict]:
    q = "SELECT * FROM rise_signal_outcomes"
    conds, params = [], []
    if brain:
        conds.append("brain = ?")
        params.append(brain)
    if lane:
        conds.append("lane = ?")
        params.append(lane)
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY created_at DESC LIMIT ?"
    params.append(int(limit))
    with _lock:
        cur = _get_conn().execute(q, params)
        return [dict(r) for r in cur.fetchall()]


def rollup(since_iso: Optional[str] = None, min_samples: int = 20) -> dict:
    """Per-brain and per-attribution aggregates for the Kernel /
    Hot Brain Router. Brains under `min_samples` are flagged
    `gathering` so a handful of trades can't skew routing. The
    signal_execution_gap (theoretical win rate − actual win rate)
    is the first Kernel metric: a large positive gap means execution/
    gating/timing degradation, NOT a weak signal."""
    cond, params = "", []
    if since_iso:
        cond = " WHERE created_at >= ?"
        params.append(since_iso)
    with _lock:
        conn = _get_conn()
        by_attr = [dict(r) for r in conn.execute(
            f"SELECT attribution, COUNT(*) n, "
            f"ROUND(AVG(theoretical_return_pct), 5) avg_theoretical, "
            f"ROUND(AVG(actual_return_pct), 5) avg_actual "
            f"FROM rise_signal_outcomes{cond} GROUP BY attribution "
            f"ORDER BY n DESC", params).fetchall()]
        by_brain = [dict(r) for r in conn.execute(
            f"SELECT brain, lane, COUNT(*) n, "
            f"SUM(CASE WHEN theoretical_outcome = 'PROFIT' THEN 1 ELSE 0 END) signal_wins, "
            f"SUM(CASE WHEN executed = 1 AND actual_return_pct > 0 THEN 1 ELSE 0 END) actual_wins, "
            f"SUM(CASE WHEN executed = 1 THEN 1 ELSE 0 END) n_executed, "
            f"SUM(CASE WHEN attribution = 'BAD_SIGNAL' THEN 1 ELSE 0 END) bad_signals, "
            f"SUM(CASE WHEN attribution LIKE 'GOOD_SIGNAL%' THEN 1 ELSE 0 END) good_signals_lost_by_pipeline, "
            f"ROUND(AVG(theoretical_return_pct), 5) avg_theoretical, "
            f"ROUND(AVG(edge_capture_ratio), 3) avg_edge_capture, "
            f"ROUND(AVG(entry_delay_seconds), 1) avg_entry_delay_s "
            f"FROM rise_signal_outcomes{cond} GROUP BY brain, lane "
            f"ORDER BY n DESC", params).fetchall()]
        total = conn.execute(
            f"SELECT COUNT(*) n FROM rise_signal_outcomes{cond}",
            params).fetchone()["n"]
    for b in by_brain:
        theo_wr = b["signal_wins"] / b["n"] if b["n"] else None
        act_wr = (b["actual_wins"] / b["n_executed"]
                  if b["n_executed"] else None)
        b["theoretical_win_rate"] = round(theo_wr, 3) if theo_wr is not None else None
        b["actual_win_rate"] = round(act_wr, 3) if act_wr is not None else None
        b["signal_execution_gap"] = (
            round(theo_wr - act_wr, 3)
            if theo_wr is not None and act_wr is not None else None)
        b["kernel_ready"] = b["n"] >= min_samples
        b["sample_state"] = "ready" if b["kernel_ready"] else f"gathering ({b['n']}/{min_samples})"
    return {"total": total, "min_samples": min_samples,
            "by_attribution": by_attr, "by_brain": by_brain}


def counts() -> dict:
    with _lock:
        n = _get_conn().execute(
            "SELECT COUNT(*) n FROM rise_signal_outcomes").fetchone()["n"]
    return {"rows": n, "db_path": _DB_PATH}

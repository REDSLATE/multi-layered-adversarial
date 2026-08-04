"""Tape Quality Gate (2026-08-04, Forven adoption plan A2).

Stale or gappy bars silently distort momentum scores and entry-timing
extension math. This module gives every tape consumer a single verdict:

  assess(bars) -> {ok, reason, completeness, max_gap_bars, age_sec,
                   tf_sec, fingerprint{n, span_min, last_ts}}

Failure reasons: thin_tape · stale_tape · gappy_tape · incomplete_tape.
Call sites: momentum scanner (granular reject instead of scoring bad
tape), entry timing gate (BAD_TAPE_QUALITY fail-closed block, never
re-armable), entry re-arm watcher (skip-with-reason).

Knobs in `runtime_flags._id=tape_quality`. Timeframe is INFERRED from
median bar spacing so 1m/5m tapes need no caller plumbing.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

FLAG_ID = "tape_quality"
DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "min_bars": 10,
    "min_completeness": 0.95,
    "max_gap_bars": 12,
    "max_staleness_tf_mult": 5.0,
}


def _ts(v) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(str(v))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def assess(bars: list[dict], cfg: Optional[dict] = None,
           now: Optional[datetime] = None) -> dict:
    """Pure verdict over chronological bars. Fail order: thin →
    stale → gappy → incomplete."""
    c = {**DEFAULTS, **(cfg or {})}
    now = now or datetime.now(timezone.utc)
    out: dict[str, Any] = {"ok": False, "reason": None,
                           "completeness": None, "max_gap_bars": None,
                           "age_sec": None, "tf_sec": None,
                           "fingerprint": {"n": len(bars),
                                           "span_min": None,
                                           "last_ts": None}}
    stamps = [t for t in (_ts(b.get("ts")) for b in bars) if t]
    if len(stamps) < int(c["min_bars"]):
        out["reason"] = "thin_tape"
        return out
    deltas = sorted((stamps[i] - stamps[i - 1]).total_seconds()
                    for i in range(1, len(stamps)))
    deltas = [d for d in deltas if d > 0]
    if not deltas:
        out["reason"] = "thin_tape"
        return out
    tf = deltas[len(deltas) // 2]  # median spacing = inferred tf
    span = (stamps[-1] - stamps[0]).total_seconds()
    age = (now - stamps[-1]).total_seconds()
    max_gap = max(0.0, max(deltas) / tf - 1.0)
    completeness = min(1.0, len(stamps) / (span / tf + 1.0))
    out.update(tf_sec=round(tf, 1), age_sec=round(age, 1),
               max_gap_bars=round(max_gap, 1),
               completeness=round(completeness, 4))
    out["fingerprint"] = {"n": len(stamps),
                          "span_min": round(span / 60.0, 1),
                          "last_ts": stamps[-1].isoformat()}
    if age > float(c["max_staleness_tf_mult"]) * tf:
        out["reason"] = "stale_tape"
        return out
    if max_gap > float(c["max_gap_bars"]):
        out["reason"] = "gappy_tape"
        return out
    if completeness < float(c["min_completeness"]):
        out["reason"] = "incomplete_tape"
        return out
    out.update(ok=True, reason="tape_ok")
    return out


async def get_tape_config() -> dict:
    from db import db  # noqa: WPS433
    doc = await db["runtime_flags"].find_one(
        {"_id": FLAG_ID}, {"_id": 0}, max_time_ms=3000) or {}
    return {**DEFAULTS, **doc}


async def assess_with_config(bars: list[dict]) -> dict:
    try:
        cfg = await get_tape_config()
    except Exception:  # noqa: BLE001
        cfg = dict(DEFAULTS)  # config read failure must not decide trades
    if not cfg.get("enabled", True):
        return {"ok": True, "reason": "gate_disabled",
                "fingerprint": {"n": len(bars)}}
    return assess(bars, cfg)

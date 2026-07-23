"""Exit policy knobs — lane-scoped SL/TP/max-hold defaults.

Stored in `runtime_flags._id=exit_policy` (survives restarts, NOT in
retention sweep). Brain-authored target/stop on the executed intent
beats these defaults; these beat nothing — no position goes unbounded.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from db import db

POLICY_FLAG_ID = "exit_policy"

DEFAULTS: dict[str, Any] = {
    "equity": {"enabled": False, "sl_pct": 3.0, "tp_pct": 6.0, "max_hold_h": 24.0},
    "crypto": {"enabled": False, "sl_pct": 3.0, "tp_pct": 8.0, "max_hold_h": 48.0},
    "escalate_after_s": 120.0,
}

_LANE_FIELDS = {"enabled", "sl_pct", "tp_pct", "max_hold_h"}

# Last-known-good policy (2026-07-23 hot-path audit P0 fix): a Mongo
# outage previously collapsed get_policy() to DEFAULTS where
# enabled=False — silently disabling stop-loss enforcement while
# positions stayed open. Now an Atlas failure returns the last
# successfully loaded policy instead; DEFAULTS apply only before the
# first successful load.
_LAST_GOOD: dict | None = None


async def get_policy() -> dict:
    """Merged view: stored overrides on top of DEFAULTS. On Mongo
    failure, fall back to the LAST KNOWN GOOD policy (stamped
    `_stale`), then DEFAULTS."""
    global _LAST_GOOD  # noqa: PLW0603
    try:
        doc = await db["runtime_flags"].find_one({"_id": POLICY_FLAG_ID}) or {}
    except Exception:  # noqa: BLE001
        if _LAST_GOOD is not None:
            return {**_LAST_GOOD, "_stale": True}
        doc = {}
    out: dict = {"escalate_after_s": float(
        doc.get("escalate_after_s", DEFAULTS["escalate_after_s"])
    )}
    for lane in ("equity", "crypto"):
        merged = dict(DEFAULTS[lane])
        stored = doc.get(lane) or {}
        for k in _LANE_FIELDS:
            if k in stored and stored[k] is not None:
                merged[k] = stored[k]
        merged["enabled"] = bool(merged["enabled"])
        for k in ("sl_pct", "tp_pct", "max_hold_h"):
            merged[k] = float(merged[k])
        out[lane] = merged
    _LAST_GOOD = out
    return out


async def set_policy(lane: str, fields: dict, updated_by: str) -> dict:
    """Persist lane knobs. Caller validates ranges."""
    update = {
        f"{lane}.{k}": v for k, v in fields.items() if k in _LANE_FIELDS
    }
    update["updated_by"] = updated_by
    update["updated_at"] = datetime.now(timezone.utc).isoformat()
    await db["runtime_flags"].update_one(
        {"_id": POLICY_FLAG_ID}, {"$set": update}, upsert=True,
    )
    return await get_policy()

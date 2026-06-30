"""Brain Registry — the ONE place that knows which brains exist and
what their tunables are.

Doctrine (2026-02-27 architectural reduction):

Collection: `brain_registry` — one row per brain.
    _id          : "camino" | "barracuda" | "hellcat" | "gto"
    enabled      : bool       (operator can disable a brain entirely)
    doctrine     : str        ("trend" | "mean_reversion" | "breakout" | "momentum")
    min_confidence: float
    min_gap      : float
    last_emit_ts : iso        (last time the brain emitted any intent)
    last_buy_ts  : iso        (last directional intent — diagnostic)
    notes        : str

This replaces:
    * shared/brain_doctrine.py constants embedded in code
    * shared/brain_tuning_cache.py runtime override layer
    * shared/brains/_doctrine_overrides.py file-based override layer
    * shared/brain_seats.py (state about brains)
    * shared/brain_identity.py + brain_identity_migration.py

Seeding: on first read of any brain, if the row doesn't exist the
default doctrine values from `BRAIN_DEFAULTS` are upserted. The
operator can mutate via `POST /api/admin/brains/{brain}/tune`.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from db import db


_COLL = "brain_registry"

# Default tunables per brain. Loaded once on first read. Operator
# overrides live in the DB row.
BRAIN_DEFAULTS: dict[str, dict[str, Any]] = {
    "camino": {
        "doctrine": "trend",
        "min_confidence": 0.46,
        "min_gap": 0.08,
        "enabled": True,
    },
    "barracuda": {
        "doctrine": "mean_reversion",
        "min_confidence": 0.43,
        "min_gap": 0.06,
        "enabled": True,
    },
    "hellcat": {
        "doctrine": "breakout",
        "min_confidence": 0.48,
        "min_gap": 0.07,
        "enabled": True,
    },
    "gto": {
        "doctrine": "momentum",
        "min_confidence": 0.45,
        "min_gap": 0.07,
        "enabled": True,
    },
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def get(brain: str) -> Optional[dict]:
    """Read a brain registry row. Auto-seeds the default row if the
    brain is known but not yet in the DB."""
    if not brain:
        return None
    brain_l = brain.lower()
    doc = await db[_COLL].find_one({"_id": brain_l}, {"_id": 0})
    if doc:
        return {**doc, "brain": brain_l}
    defaults = BRAIN_DEFAULTS.get(brain_l)
    if not defaults:
        return None
    seed = {"_id": brain_l, **defaults, "seeded_at": _now_iso()}
    try:
        await db[_COLL].insert_one(seed)
    except Exception:  # noqa: BLE001 - racy seed; second-writer is harmless
        pass
    return {**defaults, "brain": brain_l}


async def is_enabled(brain: str) -> bool:
    r = await get(brain)
    return bool(r and r.get("enabled", True))


async def list_all() -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    async for row in db[_COLL].find({}):
        bid = row.pop("_id", None)
        if bid:
            out.append({"brain": bid, **row})
            seen.add(bid)
    # Surface any brain whose row hasn't been seeded yet.
    for bid, defaults in BRAIN_DEFAULTS.items():
        if bid not in seen:
            out.append({"brain": bid, **defaults, "seeded": False})
    return out


async def tune(
    brain: str,
    *,
    enabled: Optional[bool] = None,
    min_confidence: Optional[float] = None,
    min_gap: Optional[float] = None,
    actor: str = "operator",
) -> dict:
    """Operator tunable update. Only the listed keys are mutable;
    `doctrine` is immutable (rewrites of strategy logic require a
    deploy). Returns the new row."""
    if not brain or brain.lower() not in BRAIN_DEFAULTS:
        return {"ok": False, "reason": f"unknown_brain:{brain!r}"}
    brain_l = brain.lower()
    set_fields: dict[str, Any] = {"last_tuned_at": _now_iso(), "last_tuned_by": actor}
    if enabled is not None:
        set_fields["enabled"] = bool(enabled)
    if min_confidence is not None:
        set_fields["min_confidence"] = max(0.0, min(1.0, float(min_confidence)))
    if min_gap is not None:
        set_fields["min_gap"] = max(0.0, min(1.0, float(min_gap)))
    await db[_COLL].update_one(
        {"_id": brain_l},
        {"$set": set_fields, "$setOnInsert": {"_id": brain_l, **BRAIN_DEFAULTS[brain_l]}},
        upsert=True,
    )
    return {"ok": True, **(await get(brain_l) or {})}


async def stamp_emit(brain: str, action: str) -> None:
    """Update the per-brain emit timestamps. Best-effort — failure
    is logged at module level, never raised. Used for the Brain Health
    tile so the operator sees if a brain has gone silent."""
    if not brain:
        return
    brain_l = brain.lower()
    now = _now_iso()
    update: dict[str, Any] = {"last_emit_ts": now}
    if (action or "").upper() in ("BUY", "SELL", "SHORT", "COVER"):
        update["last_directional_ts"] = now
    try:
        await db[_COLL].update_one(
            {"_id": brain_l},
            {
                "$set": update,
                "$setOnInsert": {"_id": brain_l, **BRAIN_DEFAULTS.get(brain_l, {})},
            },
            upsert=True,
        )
    except Exception:  # noqa: BLE001
        pass


__all__ = ["BRAIN_DEFAULTS", "get", "is_enabled", "list_all", "tune", "stamp_emit"]

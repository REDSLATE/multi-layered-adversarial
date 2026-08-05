"""Entry-mode governor (2026-08-05 operator directive).

"Stop it from paying tuition while you diagnose why it loses."

Three modes, DEFAULT = exit_only (fail-closed — a missing/broken
config row must never re-enable live entries):
  exit_only — NO new automated entries (BUY/SHORT). Exits, stops,
              manual trading, scanning and intent generation all
              continue. Every would-be entry that passed the full
              gate chain is recorded as a hypothetical fill in
              `shadow_fills` and blocked at the broker router, so the
              missed-entry ledger keeps scoring forward expectancy.
  canary    — tiny capped re-entry lane: at most N trades/day (knob),
              everything else identical to live. The ONLY road back
              from exit_only, and only after the promotion gate is
              green.
  live      — normal automated entries.

Mode changes are audited on the flag doc. SELLs are NEVER touched.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("risedual.execution_mode")

FLAG_ID = "entry_mode"
MODES = ("live", "exit_only", "canary")
DEFAULT_MODE = "exit_only"
DEFAULTS: dict[str, Any] = {
    "mode": DEFAULT_MODE,
    "canary_max_trades_per_day": 3,
}
SHADOW_FILLS = "shadow_fills"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _f(v) -> Optional[float]:
    try:
        f = float(v)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


async def get_entry_mode_config() -> dict:
    from db import db  # noqa: WPS433
    try:
        doc = await db["runtime_flags"].find_one(
            {"_id": FLAG_ID}, {"_id": 0}, max_time_ms=3000) or {}
    except Exception:  # noqa: BLE001
        doc = {}  # fail CLOSED → defaults → exit_only
    cfg = {**DEFAULTS, **doc}
    if cfg.get("mode") not in MODES:
        cfg["mode"] = DEFAULT_MODE
    return cfg


def hypo_fill_price(intent: dict) -> Optional[float]:
    et = intent.get("entry_timing_receipt") or {}
    p = _f(et.get("confirmation_price"))
    if p:
        return p
    snap = intent.get("snapshot") or {}
    bid, ask = _f(snap.get("bid")), _f(snap.get("ask"))
    if bid and ask and ask >= bid:
        return round((bid + ask) / 2.0, 8)
    return _f(snap.get("price")) or _f(intent.get("price_at_signal"))


async def record_shadow_fill(intent: dict, notional_usd: float,
                             why: str) -> None:
    """Hypothetical fill for a fully-gated entry that exit-only
    blocked — the forward-recorded signal the promotion gate scores."""
    from db import db  # noqa: WPS433
    try:
        await db[SHADOW_FILLS].update_one(
            {"_id": f"shadow-{intent.get('intent_id')}"},
            {"$setOnInsert": {
                "intent_id": intent.get("intent_id"),
                "symbol": intent.get("symbol"),
                "lane": intent.get("lane"),
                "stack": intent.get("stack"),
                "source": intent.get("source") or intent.get("brain"),
                "action": intent.get("action"),
                "notional_usd": round(float(notional_usd or 0), 2),
                "hypo_price": hypo_fill_price(intent),
                "blocked_why": why,
                "ts": _now_iso(),
            }}, upsert=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("shadow fill write failed: %s", exc)


async def _canary_trades_today() -> int:
    from db import db  # noqa: WPS433
    day_start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0).isoformat()
    return await db["executions"].count_documents(
        {"action": {"$in": ["BUY", "SHORT"]}, "ok": True,
         "ts": {"$gte": day_start}}, maxTimeMS=4000)


async def gate_new_entry(intent: dict, notional_usd: float) -> tuple[bool, str]:
    """(allowed, why). Called by the broker router for BUY/SHORT only.
    Records the shadow fill whenever it blocks."""
    cfg = await get_entry_mode_config()
    mode = cfg["mode"]
    if mode == "live":
        return True, "live"
    if mode == "canary":
        try:
            used = await _canary_trades_today()
        except Exception:  # noqa: BLE001
            used = 10 ** 6  # count unavailable → fail closed
        cap = int(cfg.get("canary_max_trades_per_day") or 3)
        if used < cap:
            return True, f"canary ({used + 1}/{cap} today)"
        why = (f"exit_only_mode: canary daily cap reached ({used}/{cap}) "
               "— entry recorded as shadow fill")
        await record_shadow_fill(intent, notional_usd, why)
        return False, why
    why = ("exit_only_mode: new automated entries are prohibited "
           "(2026-08-05 operator directive) — entry recorded as "
           "shadow fill; exits/stops/manual trading unaffected")
    await record_shadow_fill(intent, notional_usd, why)
    return False, why

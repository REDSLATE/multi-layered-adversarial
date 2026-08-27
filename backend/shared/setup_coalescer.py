"""Setup-aware intent coalescing (2026-06 operator directive, revised).

One market opportunity record per lane + symbol + side + active market
setup — ACROSS THE WHOLE STACK. If GTO, Camino, Hellcat and Barracuda
all react to the same underlying move, the first qualifying stack
decision creates the actionable intent; every subsequent brain output
attaches to the same setup as a per-brain contribution (confidence,
timestamps, signal_count) instead of becoming an independent
executable intent or outcome observation.

A NEW setup (and therefore a new actionable intent) is permitted when
the prior setup terminates:
    * setup TTL expired (no re-signal within the window)
    * opposite-side entry appeared (side flip)
    * primary intent executed and the position cycle completed
    * price moved materially away from the setup anchor (new structure)
    * regime state materially changed (HMM top-state flip)

DOCTRINE: never a gate. The first qualified intent always proceeds;
repeats attach; ANY error fails open (intent proceeds as normal).
Repeats are never thrown away — they live as a per-brain time series
under the setup record (`shared_setups`).

Kill switch: runtime_flags `_id=setup_coalescer` {enabled: bool}.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

logger = logging.getLogger("risedual.setup_coalescer")

COLLECTION = "shared_setups"
FLAG_ID = "setup_coalescer"
DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "setup_ttl_min": 90,
    "price_drift_pct": 5.0,
    "series_cap": 50,
}

_OPPOSITE_ENTRY = {"BUY": ("SELL", "SHORT"), "SHORT": ("COVER", "BUY")}
_indexed = False


async def _cfg() -> dict:
    try:
        from db import db  # noqa: WPS433
        doc = await db["runtime_flags"].find_one(
            {"_id": FLAG_ID}, {"_id": 0}, max_time_ms=3000) or {}
    except Exception:  # noqa: BLE001
        doc = {}
    return {**DEFAULTS, **doc}


async def _ensure_index() -> None:
    global _indexed  # noqa: PLW0603
    if _indexed:
        return
    try:
        from db import db  # noqa: WPS433
        await db[COLLECTION].create_index([("setup_key", 1), ("status", 1)])
        await db[COLLECTION].create_index([("setup_id", 1)], unique=True)
        _indexed = True
    except Exception:  # noqa: BLE001
        pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _price_of(doc: dict) -> Optional[float]:
    for v in (doc.get("signal_price"), (doc.get("snapshot") or {}).get("price"),
              (doc.get("snapshot") or {}).get("last")):
        try:
            if v is not None and float(v) > 0:
                return float(v)
        except (TypeError, ValueError):
            continue
    return None


def _wave_mode(lane: str, symbol: str) -> Optional[dict]:
    """Read-only wave-mode lookup (OBSERVE_ONLY consumer). None on any
    failure — setup logic then falls back to the price-drift heuristic."""
    try:
        from mc_pulse.snapshot_service import current_wave_mode  # noqa: WPS433
        return current_wave_mode(lane, symbol)
    except Exception:  # noqa: BLE001
        return None


async def _termination_reason(active: dict, doc: dict, cfg: dict) -> Optional[str]:
    from db import db  # noqa: WPS433
    now = datetime.now(timezone.utc)
    try:
        last_seen = datetime.fromisoformat(str(active["last_seen"]).replace("Z", "+00:00"))
        if now - last_seen > timedelta(minutes=float(cfg["setup_ttl_min"])):
            return "setup_ttl_expired"
    except (KeyError, ValueError):
        pass

    # Wave-mode structure boundary (2026-06, operator-approved):
    # the per-symbol closed-bar mode (hysteresis built in) is a cleaner
    # "did the market genuinely reset?" signal than raw price drift.
    #   * mode changed since setup anchor → new structure → terminate
    #   * mode unchanged → continuation move → SKIP the price-drift
    #     check (price continuing inside the same trend leg must not
    #     split the setup artificially)
    #   * wave unavailable → fall back to the price-drift heuristic
    wave_now = _wave_mode(active.get("lane") or doc.get("lane") or "",
                          doc.get("symbol") or "")
    wave_anchor = active.get("wave_mode")
    if wave_now and wave_anchor:
        if wave_now["mode"] != wave_anchor:
            return "wave_mode_change"
    else:
        price = _price_of(doc)
        anchor = active.get("anchor_price")
        if price and anchor:
            drift = abs(price / float(anchor) - 1.0) * 100.0
            if drift > float(cfg["price_drift_pct"]):
                return "price_structure_change"

    try:
        flip = await db["shared_intents"].find_one(
            {"symbol": doc.get("symbol"), "lane": doc.get("lane"),
             "action": {"$in": list(_OPPOSITE_ENTRY.get(active["side"], ()))},
             "ingest_ts": {"$gt": active["first_seen"]}},
            {"_id": 1}, max_time_ms=3000)
        if flip:
            return "side_flip"
    except Exception:  # noqa: BLE001
        pass

    try:
        primary = await db["shared_intents"].find_one(
            {"intent_id": active.get("primary_intent_id")},
            {"executed": 1}, max_time_ms=3000)
        if primary and primary.get("executed"):
            pos = await db["positions"].find_one(
                {"symbol": doc.get("symbol"),
                 "status": {"$in": ["open", "OPEN", "active"]}},
                {"_id": 1}, max_time_ms=3000)
            if not pos:
                return "position_cycle_complete"
    except Exception:  # noqa: BLE001
        pass

    new_state = (doc.get("regime_ctx") or {}).get("top_state")
    old_state = active.get("regime_top_state")
    if new_state is not None and old_state is not None and new_state != old_state:
        return "regime_change"
    return None


async def coalesce_or_register(doc: dict) -> Optional[dict]:
    """None → intent proceeds normally (doc stamped with setup_id /
    setup_role="primary"). dict → repeat coalesced onto an existing
    setup; caller must NOT insert a new executable intent."""
    action = (doc.get("action") or "").upper()
    if action not in ("BUY", "SHORT"):
        return None
    try:
        return await _impl(doc, action)
    except Exception as exc:  # noqa: BLE001
        logger.warning("setup_coalescer fail-open intent_id=%s: %s",
                       doc.get("intent_id"), exc)
        return None


async def _impl(doc: dict, action: str) -> Optional[dict]:
    cfg = await _cfg()
    if not cfg.get("enabled", True):
        return None
    from db import db  # noqa: WPS433
    await _ensure_index()

    lane = (doc.get("lane") or "unset").lower()
    symbol = doc.get("symbol") or "?"
    brain = doc.get("stack_canonical") or doc.get("stack") or "unknown"
    conf = float(doc.get("confidence") or 0.0)
    now = _now()
    # STACK-LEVEL key (operator revision): one setup across all brains.
    setup_key = f"{lane}:{symbol}:{action}"

    active = await db[COLLECTION].find_one(
        {"setup_key": setup_key, "status": "active"}, max_time_ms=3000)

    if active:
        reason = await _termination_reason(active, doc, cfg)
        if reason:
            await db[COLLECTION].update_one(
                {"setup_id": active["setup_id"]},
                {"$set": {"status": "terminated",
                          "terminated_reason": reason,
                          "terminated_at": now}})
            active = None
        else:
            sig_n = int(active.get("signal_count") or 1) + 1
            entry = {"ts": now, "brain": brain, "confidence": conf}
            await db[COLLECTION].update_one(
                {"setup_id": active["setup_id"]},
                {"$set": {"last_seen": now, "latest_confidence": conf,
                          "signal_count": sig_n,
                          f"contributions.{brain}.last_seen": now,
                          f"contributions.{brain}.latest_confidence": conf},
                 "$max": {"max_confidence": conf,
                          f"contributions.{brain}.max_confidence": conf},
                 "$inc": {f"contributions.{brain}.signal_count": 1},
                 "$setOnInsert": {},
                 "$push": {"confidence_series": {
                     "$each": [entry], "$slice": -int(cfg["series_cap"])}}})
            await db[COLLECTION].update_one(
                {"setup_id": active["setup_id"],
                 f"contributions.{brain}.first_seen": {"$exists": False}},
                {"$set": {f"contributions.{brain}.first_seen": now,
                          f"contributions.{brain}.initial_confidence": conf}})
            # keep the PRIMARY intent's picture current for dashboards
            await db["shared_intents"].update_one(
                {"intent_id": active["primary_intent_id"]},
                {"$set": {"repeat_last_seen": now,
                          "latest_repeat_confidence": conf},
                 "$max": {"max_repeat_confidence": conf},
                 "$inc": {"repeat_count": 1}})
            return {"coalesced": True,
                    "setup_id": active["setup_id"],
                    "primary_intent_id": active["primary_intent_id"],
                    "signal_count": sig_n,
                    "primary_brain": active.get("primary_brain")}

    setup_id = f"{setup_key}:{uuid.uuid4().hex[:8]}"
    wave = _wave_mode(lane, symbol)
    await db[COLLECTION].insert_one({
        "setup_id": setup_id, "setup_key": setup_key,
        "lane": lane, "symbol": symbol, "side": action,
        "status": "active",
        "primary_intent_id": doc.get("intent_id"),
        "primary_brain": brain,
        "first_seen": now, "last_seen": now,
        "signal_count": 1,
        "initial_confidence": conf, "latest_confidence": conf,
        "max_confidence": conf,
        "anchor_price": _price_of(doc),
        "regime_top_state": (doc.get("regime_ctx") or {}).get("top_state"),
        "wave_mode": (wave or {}).get("mode"),
        "wave_as_of": (wave or {}).get("as_of"),
        "contributions": {brain: {
            "first_seen": now, "last_seen": now, "signal_count": 1,
            "initial_confidence": conf, "latest_confidence": conf,
            "max_confidence": conf}},
        "confidence_series": [{"ts": now, "brain": brain, "confidence": conf}],
    })
    doc["setup_id"] = setup_id
    doc["setup_role"] = "primary"
    return None

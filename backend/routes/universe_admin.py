"""Watchlist (operator pins) + universe quality knobs.

Doctrine (2026-07-22, operator: "the picks are terrible — nothing on
my watchlist is being picked"): the universe refresher already merges
`patterns_universe` rows with `pinned=true` into every refresh (pins
rank first, bypass hysteresis and quality filters). What was missing
was any operator surface to SET a pin. This module is that surface,
plus runtime quality knobs so screener penny-pump noise can be
throttled without a deploy:

  * `min_price_equity`     — raise the equity price floor (default $1)
  * `screener_admit_cap`   — cap non-pinned screener admits per cycle
                             (0 = pins-only universe)

Knobs live in `runtime_flags._id=universe_quality` (same pattern as
the conviction floor knob) and are read fresh at every refresh tick.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from auth import get_current_user
from db import db
from shared.broker_symbol_resolver import has_kraken_mapping

router = APIRouter(prefix="/admin/universe", tags=["universe-admin"])

_EQUITY_RE = re.compile(r"^[A-Z]{1,5}$")
_CRYPTO_BASE_RE = re.compile(r"^[A-Z0-9]{2,12}$")

QUALITY_FLAG_ID = "universe_quality"


def _normalize_symbol(symbol: str, lane: str) -> str:
    s = (symbol or "").strip().upper()
    if lane == "equity":
        if not _EQUITY_RE.match(s):
            raise HTTPException(
                status_code=422,
                detail=f"invalid equity symbol {symbol!r} (1-5 letters)",
            )
        return s
    # crypto: accept BTC / BTC/USD / BTC-USD / BTCUSD / CRYPTO:BTC-USD
    # / Kraken internals (XBT, XXBTZUSD) → BTC/USD. Single normalizer
    # shared with the BUY allowlist so entries can never drift.
    from shared.risk_sizer.buy_allowlist import normalize_crypto_symbol  # noqa: WPS433
    canonical = normalize_crypto_symbol(s)
    base = canonical.split("/", 1)[0] if canonical else ""
    if not base or not _CRYPTO_BASE_RE.match(base):
        raise HTTPException(
            status_code=422, detail=f"invalid crypto symbol {symbol!r}",
        )
    return canonical


@router.get("/watchlist")
async def get_watchlist(_user: dict = Depends(get_current_user)):  # noqa: B008
    pins: dict[str, list] = {"equity": [], "crypto": []}
    async for r in db["patterns_universe"].find(
        {"pinned": True, "active": True},
        {"_id": 0, "symbol": 1, "lane": 1, "pinned_by": 1, "pinned_at": 1},
    ).sort("symbol", 1):
        lane = r.get("lane")
        if lane in pins:
            pins[lane].append(r)

    # Universe composition per lane (what the brains actually see).
    composition: dict[str, dict] = {}
    for lane in ("equity", "crypto"):
        doc = await db["live_universe"].find_one({"lane": lane}) or {}
        syms = doc.get("symbols") or []
        composition[lane] = {
            "total": len(syms),
            "pinned": sum(1 for s in syms if s.get("pinned")),
            "under_4": sum(
                1 for s in syms if 0 < (s.get("price") or 0) < 4.0
            ) if lane == "equity" else None,
            "refreshed_at": doc.get("refreshed_at"),
        }

    flags = await db["runtime_flags"].find_one({"_id": QUALITY_FLAG_ID}) or {}
    from shared.universe.refresher import DEFAULT_CORE_EQUITY  # noqa: WPS433
    return {
        "pins": pins,
        "composition": composition,
        "quality": {
            "min_price_equity": flags.get("min_price_equity"),
            "screener_admit_cap": flags.get("screener_admit_cap"),
            "universe_cap_equity": flags.get("universe_cap_equity"),
            "universe_cap_crypto": flags.get("universe_cap_crypto"),
            "core_equity_symbols": flags.get("core_equity_symbols"),
            "core_equity_default": DEFAULT_CORE_EQUITY,
        },
    }


@router.post("/watchlist")
async def add_pin(
    body: dict,
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    lane = (body.get("lane") or "").strip().lower()
    if lane not in ("equity", "crypto"):
        raise HTTPException(status_code=422, detail="lane must be equity|crypto")
    sym = _normalize_symbol(body.get("symbol", ""), lane)

    if lane == "crypto":
        base = sym.split("/")[0]
        canonical = f"CRYPTO:{base}-USD"
        if not has_kraken_mapping(canonical):
            raise HTTPException(
                status_code=422,
                detail=(
                    f"{sym} has no Kraken pair mapping — add it in the "
                    f"Kraken Pair Map editor first, then pin it here"
                ),
            )

    now = datetime.now(timezone.utc).isoformat()
    await db["patterns_universe"].update_one(
        {"symbol": sym, "lane": lane},
        {"$set": {
            "symbol": sym,
            "lane": lane,
            "active": True,
            "pinned": True,
            "pinned_by": _user.get("email") or "unknown",
            "pinned_at": now,
        }},
        upsert=True,
    )
    return {"ok": True, "symbol": sym, "lane": lane, "pinned_at": now}


@router.delete("/watchlist/{lane}/{symbol}")
async def remove_pin(
    lane: str,
    symbol: str,
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    lane = lane.strip().lower()
    if lane not in ("equity", "crypto"):
        raise HTTPException(status_code=422, detail="lane must be equity|crypto")
    sym = _normalize_symbol(symbol, lane)
    res = await db["patterns_universe"].update_one(
        {"symbol": sym, "lane": lane, "pinned": True},
        {"$set": {"pinned": False}},
    )
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail=f"{sym} is not pinned")
    return {"ok": True, "symbol": sym, "lane": lane}


@router.post("/quality")
async def set_quality(
    body: dict,
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    update: dict = {}
    if "min_price_equity" in body:
        v = body["min_price_equity"]
        if v is None:
            update["min_price_equity"] = None
        else:
            try:
                f = float(v)
            except (TypeError, ValueError):
                raise HTTPException(status_code=422, detail="min_price_equity must be a number")
            if not (0.0 <= f <= 10_000.0):
                raise HTTPException(status_code=422, detail="min_price_equity out of range")
            update["min_price_equity"] = f
    if "screener_admit_cap" in body:
        v = body["screener_admit_cap"]
        if v is None:
            update["screener_admit_cap"] = None
        else:
            try:
                i = int(v)
            except (TypeError, ValueError):
                raise HTTPException(status_code=422, detail="screener_admit_cap must be an integer")
            if not (0 <= i <= 200):
                raise HTTPException(status_code=422, detail="screener_admit_cap out of range")
            update["screener_admit_cap"] = i
    for cap_key in ("universe_cap_equity", "universe_cap_crypto"):
        if cap_key in body:
            v = body[cap_key]
            if v is None:
                update[cap_key] = None
            else:
                try:
                    i = int(v)
                except (TypeError, ValueError):
                    raise HTTPException(status_code=422, detail=f"{cap_key} must be an integer")
                if not (1 <= i <= 300):
                    raise HTTPException(status_code=422, detail=f"{cap_key} out of range [1,300]")
                update[cap_key] = i
    if "core_equity_symbols" in body:
        v = body["core_equity_symbols"]
        if v is None:
            update["core_equity_symbols"] = None  # revert to default list
        else:
            if not isinstance(v, list):
                raise HTTPException(status_code=422, detail="core_equity_symbols must be a list")
            cleaned = []
            for s in v[:100]:
                sym = str(s).strip().upper()
                if not _EQUITY_RE.match(sym):
                    raise HTTPException(status_code=422, detail=f"invalid core symbol {s!r}")
                if sym not in cleaned:
                    cleaned.append(sym)
            update["core_equity_symbols"] = cleaned
    if not update:
        raise HTTPException(status_code=422, detail="nothing to update")

    update["updated_by"] = _user.get("email") or "unknown"
    update["updated_at"] = datetime.now(timezone.utc).isoformat()
    await db["runtime_flags"].update_one(
        {"_id": QUALITY_FLAG_ID}, {"$set": update}, upsert=True,
    )
    return {"ok": True, **update}


@router.get("/entry-timing")
async def get_entry_timing(_user: dict = Depends(get_current_user)):  # noqa: B008
    """Entry Timing Gate config — per-universe-class thresholds."""
    from shared.risk_sizer.entry_timing import get_config  # noqa: WPS433
    return {"ok": True, "entry_timing": await get_config()}


@router.put("/entry-timing")
async def put_entry_timing(
    body: dict,
    user: dict = Depends(get_current_user),  # noqa: B008
):
    """Update gate config. Body: {enabled?: bool, profiles?: {class:
    {threshold overrides}}}. Disabling requires confirm="DISABLE_GATE"
    — turning off chase protection must never happen by accident."""
    from shared.risk_sizer.entry_timing import (  # noqa: WPS433
        DEFAULT_PROFILES, FLAG_ID as ET_FLAG_ID, get_config,
        invalidate_cache as et_invalidate,
    )
    if body.get("enabled") is False and body.get("confirm") != "DISABLE_GATE":
        raise HTTPException(
            status_code=422,
            detail='disabling the entry-timing gate requires '
                   'confirm="DISABLE_GATE"',
        )
    prev = await get_config()
    update: dict = {}
    if "enabled" in body:
        update["enabled"] = bool(body["enabled"])
    if isinstance(body.get("profiles"), dict):
        clean = {}
        for name, over in body["profiles"].items():
            if name in DEFAULT_PROFILES and isinstance(over, dict):
                clean[name] = {
                    k: over[k] for k in DEFAULT_PROFILES[name] if k in over
                }
        update["profiles"] = clean
    if not update:
        raise HTTPException(status_code=422, detail="nothing to update")
    update["updated_by"] = user.get("email")
    update["updated_at"] = datetime.now(timezone.utc).isoformat()
    await db["runtime_flags"].update_one(
        {"_id": ET_FLAG_ID}, {"$set": update}, upsert=True,
    )
    et_invalidate()
    await db["crypto_buy_allowlist_audit"].insert_one({
        "ts": update["updated_at"],
        "kind": "entry_timing",
        "updated_by": user.get("email"),
        "previous": prev,
        "next": {**prev, **{k: v for k, v in update.items()
                            if k in ("enabled", "profiles")}},
    })
    return {"ok": True, "entry_timing": await get_config()}


@router.get("/entry-timing/stats")
async def entry_timing_stats(_user: dict = Depends(get_current_user)):  # noqa: B008
    """Timing tile: blocked-vs-fired outcomes over 24h/7d — tells
    the operator whether the caps protect the account or merely
    suppress opportunity."""
    from namespaces import SHARED_INTENTS  # noqa: WPS433
    now = datetime.now(timezone.utc)
    out: dict = {}
    for label, hours in (("24h", 24), ("7d", 168)):
        cut = (now - timedelta(hours=hours)).isoformat()
        fired = await db[SHARED_INTENTS].count_documents(
            {"action": "BUY", "executed": True, "ingest_ts": {"$gte": cut}},
            maxTimeMS=8000)
        blocked = await db[SHARED_INTENTS].count_documents(
            {"risk_reason": {"$regex": "^entry_timing:"},
             "ingest_ts": {"$gte": cut}}, maxTimeMS=8000)
        trig_q = {"created_at": {"$gte": cut}}
        trig: dict = {}
        for state in ("WATCHING", "REARMED", "EXPIRED", "INVALIDATED"):
            trig[state.lower()] = await db["entry_rearm_triggers"].count_documents(
                {**trig_q, "state": state}, maxTimeMS=8000)
        rearm_filled = await db[SHARED_INTENTS].count_documents(
            {"rearm_of": {"$exists": True}, "executed": True,
             "ingest_ts": {"$gte": cut}}, maxTimeMS=8000)
        # avg extension at actual fill (chase level that still fires)
        ext_pipe = [
            {"$match": {"action": "BUY", "executed": True,
                        "ingest_ts": {"$gte": cut},
                        "entry_timing_receipt.extension_from_confirmation_pct":
                            {"$exists": True}}},
            {"$group": {"_id": None, "avg": {"$avg":
                "$entry_timing_receipt.extension_from_confirmation_pct"},
                "n": {"$sum": 1}}},
        ]
        avg_ext = None
        async for r in db[SHARED_INTENTS].aggregate(ext_pipe, maxTimeMS=8000):
            avg_ext = round(r["avg"], 3) if r["n"] else None
        # entry improvement from waiting: block price vs re-entry price
        imp_pipe = [
            {"$match": {**trig_q, "state": "REARMED",
                        "block_price": {"$gt": 0},
                        "new_confirmation_price": {"$gt": 0}}},
            {"$project": {"imp": {"$multiply": [100, {"$divide": [
                {"$subtract": ["$block_price", "$new_confirmation_price"]},
                "$block_price"]}]}}},
            {"$group": {"_id": None, "avg": {"$avg": "$imp"}, "n": {"$sum": 1}}},
        ]
        avg_improvement = None
        async for r in db["entry_rearm_triggers"].aggregate(imp_pipe, maxTimeMS=8000):
            avg_improvement = round(r["avg"], 3) if r["n"] else None
        # chase avoided (% price gave back after we refused to chase):
        # blocked → later invalidated/faded; vs missed by waiting
        # (% price kept running on EXPIRED no-pullback triggers)
        moved_pipe = lambda state, invert: [  # noqa: E731
            {"$match": {**trig_q, "state": state, "block_price": {"$gt": 0},
                        "last_price": {"$gt": 0}}},
            {"$project": {"pct": {"$multiply": [100 * invert, {"$divide": [
                {"$subtract": ["$last_price", "$block_price"]},
                "$block_price"]}]}}},
            {"$group": {"_id": None, "avg": {"$avg": "$pct"}, "n": {"$sum": 1}}},
        ]
        avoided_pct = missed_pct = None
        async for r in db["entry_rearm_triggers"].aggregate(
                moved_pipe("INVALIDATED", -1), maxTimeMS=8000):
            avoided_pct = round(r["avg"], 3) if r["n"] else None
        async for r in db["entry_rearm_triggers"].aggregate(
                moved_pipe("EXPIRED", 1), maxTimeMS=8000):
            missed_pct = round(r["avg"], 3) if r["n"] else None
        out[label] = {
            "entries_fired": fired,
            "late_entries_blocked": blocked,
            "triggers": trig,
            "rearmed_filled": rearm_filled,
            "avg_extension_at_fill_pct": avg_ext,
            "avg_reentry_improvement_pct": avg_improvement,
            "chase_avoided_avg_pct": avoided_pct,
            "missed_by_waiting_avg_pct": missed_pct,
        }
        # ── Prod Deploy Watch additions (2026-08-01) ──
        from shared.risk_sizer.rearm_report import (  # noqa: WPS433
            child_outcome_counts,
        )
        organic_q = {"action": "BUY", "ingest_ts": {"$gte": cut},
                     "intent_id": {"$not": {
                         "$regex": "^(validate-|rearmval|expireval)"}}}
        out[label]["organic_buy_intents"] = await db[
            SHARED_INTENTS].count_documents(organic_q, maxTimeMS=8000)
        srcs: dict = {}
        async for r in db[SHARED_INTENTS].aggregate([
            {"$match": {"ingest_ts": {"$gte": cut},
                        "entry_timing_receipt": {"$exists": True}}},
            {"$group": {"_id":
                "$entry_timing_receipt.confirmation_source",
                "n": {"$sum": 1}}},
        ], maxTimeMS=8000):
            srcs[r["_id"] or "missing"] = r["n"]
        out[label]["confirmation_sources"] = srcs
        by_class: dict = {}
        async for r in db[SHARED_INTENTS].aggregate([
            {"$match": {"risk_reason": {"$regex": "^entry_timing:"},
                        "ingest_ts": {"$gte": cut}}},
            {"$group": {"_id": "$entry_timing_receipt.universe_class",
                        "n": {"$sum": 1}}},
        ], maxTimeMS=8000):
            by_class[r["_id"] or "unknown"] = r["n"]
        out[label]["blocks_by_class"] = by_class
        out[label]["rearm_children"] = await child_outcome_counts(db, cut)
        dup = 0
        async for r in db["entry_rearm_triggers"].aggregate([
            {"$match": {"created_at": {"$gte": cut}}},
            {"$group": {"_id": None, "n": {"$sum": {"$ifNull": [
                "$duplicate_blocks_prevented", 0]}}}},
        ], maxTimeMS=8000):
            dup = r["n"]
        out[label]["duplicate_suppressions"] = dup
    from shared.risk_sizer.rearm_report import first_organic_rearm  # noqa: WPS433
    return {"ok": True, "windows": out,
            "first_prod_rearm": await first_organic_rearm(db)}


@router.get("/buy-eligibility")
async def buy_eligibility_get(_user: dict = Depends(get_current_user)):  # noqa: B008
    """Hybrid BUY eligibility knobs (2026-08-03) + live probe support."""
    from shared.risk_sizer.buy_eligibility import get_eligibility_config  # noqa: WPS433
    return {"ok": True, "config": await get_eligibility_config()}


@router.get("/buy-eligibility/probe")
async def buy_eligibility_probe(
    symbol: str,
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    """Dry-run a symbol through the eligibility rules (no side effects)."""
    from shared.risk_sizer.buy_eligibility import evaluate_buy_eligibility  # noqa: WPS433
    allowed, receipt = await evaluate_buy_eligibility(symbol)
    return {"ok": True, "allowed": allowed, "receipt": receipt}


class EligibilityKnobs(BaseModel):
    mode: Optional[str] = None
    min_dollar_vol_24h: Optional[float] = Field(default=None, ge=0)
    max_spread_bps: Optional[float] = Field(default=None, ge=1, le=1000)
    max_notional_offlist_usd: Optional[float] = Field(default=None, ge=1)
    max_pct_of_24h_vol: Optional[float] = Field(default=None, ge=0.01, le=10)
    denylist: Optional[list[str]] = None


@router.post("/buy-eligibility")
async def buy_eligibility_update(
    body: EligibilityKnobs,
    user: dict = Depends(get_current_user),  # noqa: B008
):
    from shared.risk_sizer.buy_eligibility import (  # noqa: WPS433
        FLAG_ID as ELIG_FLAG, get_eligibility_config, reset_for_tests,
    )
    changes = {k: v for k, v in body.model_dump().items() if v is not None}
    if "mode" in changes and changes["mode"] not in ("static", "dynamic", "hybrid"):
        raise HTTPException(422, "mode must be static | dynamic | hybrid")
    if changes:
        changes["updated_at"] = datetime.now(timezone.utc).isoformat()
        changes["updated_by"] = user.get("email") or "operator"
        await db["runtime_flags"].update_one(
            {"_id": ELIG_FLAG}, {"$set": changes}, upsert=True)
        reset_for_tests()
    return {"ok": True, "config": await get_eligibility_config()}


@router.get("/entry-timing/rearm-timeline")
async def entry_timing_rearm_timeline(
    trigger_id: Optional[str] = None,
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    """The single linked timeline for the first (or a specific)
    organic re-arm: original intent → block → watch → re-arm → child
    → second gate chain → queue proof → broker → fills."""
    from shared.risk_sizer.rearm_report import build_rearm_timeline  # noqa: WPS433
    return await build_rearm_timeline(db, trigger_id)


@router.get("/entry-timing/health")
async def entry_timing_health(_user: dict = Depends(get_current_user)):  # noqa: B008
    """Deployment guards for the three 2026-08-01 P0 fixes:
    confirmation derivation, child-in-local-queue, freeze/thaw
    roadguard consistency."""
    from shared.risk_sizer.rearm_report import health_checks  # noqa: WPS433
    return await health_checks(db)


@router.get("/crypto-buy-allowlist")
async def get_crypto_buy_allowlist(_user: dict = Depends(get_current_user)):  # noqa: B008
    """Allowlist-only BUY universe (2026-07-28). SELLs never gated.
    Includes the last 10 audit rows (who/when/previous)."""
    from shared.risk_sizer.buy_allowlist import get_allowlist  # noqa: WPS433
    audit = await db["crypto_buy_allowlist_audit"].find(
        {}, {"_id": 0},
    ).sort("ts", -1).max_time_ms(4000).to_list(10)
    return {"ok": True, "allowlist": await get_allowlist(), "audit": audit}


@router.get("/crypto-buy-allowlist/held-stats")
async def crypto_buy_allowlist_held_stats(
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    """How many crypto BUYs the allowlist held in the last 1h/24h/7d,
    plus the most recent held intents (doctrine evidence preserved on
    the intent doc — this is the review surface for deciding whether
    an A-quality exception is ever justified)."""
    from namespaces import SHARED_INTENTS  # noqa: WPS433
    held_q = {"risk_reason": "risk_sizer:not_in_buy_allowlist"}
    now = datetime.now(timezone.utc)
    counts: dict = {}
    for label, hours in (("1h", 1), ("24h", 24), ("7d", 168)):
        cutoff = (now - timedelta(hours=hours)).isoformat()
        counts[label] = await db[SHARED_INTENTS].count_documents(
            {**held_q, "ingest_ts": {"$gte": cutoff}}, maxTimeMS=8000,
        )
    recent_raw = await db[SHARED_INTENTS].find(
        held_q,
        {"_id": 0, "intent_id": 1, "symbol": 1, "stack": 1, "action": 1,
         "confidence": 1, "ingest_ts": 1,
         "doctrine_packet.base_labels.quality": 1,
         "doctrine_packet.base_labels.score": 1},
    ).sort("ingest_ts", -1).max_time_ms(8000).to_list(15)
    recent = []
    for r in recent_raw:
        base = ((r.pop("doctrine_packet", None) or {}).get("base_labels") or {})
        r["doctrine_quality"] = base.get("quality")
        r["doctrine_score"] = base.get("score")
        recent.append(r)
    return {"ok": True, "held_counts": counts, "recent_held": recent}


@router.get("/crypto-buy-allowlist/override")
async def get_crypto_buy_allowlist_override(
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    """A-quality override policy — SHIPPED DISABLED. Read-only view."""
    from shared.risk_sizer.allowlist_override import get_override_policy  # noqa: WPS433
    return {"ok": True, "override": await get_override_policy()}


@router.put("/crypto-buy-allowlist/override")
async def put_crypto_buy_allowlist_override(
    body: dict,
    user: dict = Depends(get_current_user),  # noqa: B008
):
    """Update the override policy. Enabling requires an explicit
    `confirm: "ENABLE_OVERRIDE"` — an override must never happen by
    accident, and never solely on doctrine score (all checks in
    shared/risk_sizer/allowlist_override.py must pass)."""
    from shared.risk_sizer.allowlist_override import (  # noqa: WPS433
        DEFAULT_POLICY, OVERRIDE_FLAG_ID, get_override_policy,
        invalidate_cache,
    )
    if bool(body.get("enabled")) and body.get("confirm") != "ENABLE_OVERRIDE":
        raise HTTPException(
            status_code=422,
            detail='enabling the override requires confirm="ENABLE_OVERRIDE"',
        )
    prev = await get_override_policy()
    update = {
        k: body[k] for k in DEFAULT_POLICY if k in body
    }
    update["updated_by"] = user.get("email")
    update["updated_at"] = datetime.now(timezone.utc).isoformat()
    await db["runtime_flags"].update_one(
        {"_id": OVERRIDE_FLAG_ID}, {"$set": update}, upsert=True,
    )
    invalidate_cache()
    await db["crypto_buy_allowlist_audit"].insert_one({
        "ts": datetime.now(timezone.utc).isoformat(),
        "kind": "override_policy",
        "updated_by": user.get("email"),
        "previous": prev,
        "next": {**prev, **update},
    })
    return {"ok": True, "override": await get_override_policy()}


@router.put("/crypto-buy-allowlist")
async def put_crypto_buy_allowlist(
    body: dict,
    user: dict = Depends(get_current_user),  # noqa: B008
):
    """Replace the allowlist. Body: {enabled: bool, symbols: [..]}.
    Symbols accept BTC / BTC/USD / BTCUSD / CRYPTO:BTC-USD / Kraken
    internal pair names — all normalized to BASE/USD. Every change is
    audited with the previous value."""
    from shared.risk_sizer.buy_allowlist import (  # noqa: WPS433
        DEFAULT_ALLOWLIST, FLAG_ID, invalidate_cache,
    )
    enabled = bool(body.get("enabled", True))
    raw = body.get("symbols")
    if not isinstance(raw, list):
        raise HTTPException(status_code=422, detail="symbols must be a list")
    symbols = sorted({_normalize_symbol(s, "crypto") for s in raw})
    if enabled and not symbols:
        raise HTTPException(
            status_code=422,
            detail="enabled allowlist cannot be empty — that would block "
                   "ALL crypto BUYs; disable it instead",
        )
    prev = await db["runtime_flags"].find_one(
        {"_id": FLAG_ID}, {"_id": 0}, max_time_ms=4000,
    )
    doc = {
        "enabled": enabled,
        "symbols": symbols,
        "updated_by": user.get("email"),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    await db["runtime_flags"].update_one(
        {"_id": FLAG_ID}, {"$set": doc}, upsert=True,
    )
    invalidate_cache()
    # Config audit — kept forever (controls doctrine, no TTL).
    await db["crypto_buy_allowlist_audit"].insert_one({
        "ts": doc["updated_at"],
        "kind": "allowlist",
        "updated_by": user.get("email"),
        "previous": prev or {"source": "default", "enabled": True,
                             "symbols": list(DEFAULT_ALLOWLIST)},
        "next": {"enabled": enabled, "symbols": symbols},
    })
    return {"ok": True, "allowlist": doc}


@router.post("/refresh")
async def force_refresh(_user: dict = Depends(get_current_user)):  # noqa: B008
    """Rebuild both lane universes NOW (pins + knobs take effect
    immediately instead of waiting for the 15-min cycle)."""
    from shared.universe.refresher import refresh_all_lanes  # noqa: WPS433
    results = await refresh_all_lanes(force=True)
    out = {}
    for lane, rep in results.items():
        if isinstance(rep, dict):
            sizes = rep.get("sizes") or {}
            out[lane] = {
                "final": sizes.get("final"),
                "published": rep.get("published"),
                "skipped": rep.get("skipped"),
            }
    return {"ok": True, "lanes": out}

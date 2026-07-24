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
from datetime import datetime, timezone

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
    # crypto: accept BTC, BTC/USD, CRYPTO:BTC-USD → BTC/USD
    s = s.removeprefix("CRYPTO:")
    base = s.split("/")[0].split("-")[0]
    if not _CRYPTO_BASE_RE.match(base):
        raise HTTPException(
            status_code=422, detail=f"invalid crypto symbol {symbol!r}",
        )
    return f"{base}/USD"


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

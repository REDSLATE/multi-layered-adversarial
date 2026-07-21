"""Kraken Pair Map Editor — operator-managed pair overrides.

Doctrine: the static BROKER_SYMBOL_MAP['kraken'] top-30 stays
authoritative in code. Operators extend coverage at runtime here —
every added pair is validated against Kraken's public AssetPairs API
BEFORE it is saved, so a typo can never route a live order to a
nonexistent pair. Overrides live in `kraken_pair_overrides` and are
mirrored in-process by `broker_symbol_resolver`.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException

from auth import get_current_user
from db import db
from shared.broker_symbol_resolver import (
    BROKER_SYMBOL_MAP,
    KRAKEN_OVERRIDES_COLLECTION,
    ensure_kraken_overrides_fresh,
)

router = APIRouter(prefix="/admin/kraken-pairs", tags=["kraken-pair-editor"])

_BASE_RE = re.compile(r"^[A-Z0-9]{2,12}$")


def _normalize_base(symbol: str) -> str:
    """`ygg`, `YGG/USD`, `CRYPTO:YGG-USD` → `YGG`."""
    s = (symbol or "").strip().upper()
    s = s.removeprefix("CRYPTO:")
    for sep in ("/", "-"):
        if sep in s:
            s = s.split(sep, 1)[0]
    if not _BASE_RE.match(s):
        raise HTTPException(status_code=422, detail=f"invalid symbol {symbol!r}")
    return s


async def _kraken_validate(candidate_pair: str) -> dict:
    """Ask Kraken's public AssetPairs whether the pair exists. Returns
    the pair detail dict (altname, ordermin, ...) or raises 422."""
    url = "https://api.kraken.com/0/public/AssetPairs"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(url, params={"pair": candidate_pair})
            body = r.json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=502,
            detail=f"Kraken AssetPairs probe failed: {exc}",
        )
    if body.get("error"):
        raise HTTPException(
            status_code=422,
            detail=(
                f"Kraken does not recognize pair {candidate_pair!r}: "
                f"{body['error']}"
            ),
        )
    result = body.get("result") or {}
    if not result:
        raise HTTPException(
            status_code=422,
            detail=f"Kraken returned no data for {candidate_pair!r}",
        )
    return next(iter(result.values()))


@router.get("")
async def list_pairs(_user: dict = Depends(get_current_user)):  # noqa: B008
    await ensure_kraken_overrides_fresh(force=True)
    overrides = []
    async for d in db[KRAKEN_OVERRIDES_COLLECTION].find({}).sort("ts", -1):
        overrides.append({
            "canonical": d["_id"],
            "symbol": d.get("symbol"),
            "kraken_pair": d.get("kraken_pair"),
            "ordermin": d.get("ordermin"),
            "added_by": d.get("added_by"),
            "ts": d.get("ts"),
        })
    # Quick-add suggestions: symbols recently rejected for no mapping.
    pipe = [
        {"$match": {
            "gate_state": "rejected_at_ingest",
            "broker_reason": "no_kraken_pair_mapping",
        }},
        {"$group": {"_id": "$symbol", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": 8},
    ]
    suggestions = []
    known = set(BROKER_SYMBOL_MAP["kraken"]) | {o["canonical"] for o in overrides}
    async for d in db["shared_intents"].aggregate(pipe, maxTimeMS=6000):
        sym = d["_id"]
        if not sym:
            continue
        try:
            canonical = f"CRYPTO:{_normalize_base(sym)}-USD"
        except HTTPException:
            continue
        if canonical not in known:
            suggestions.append({"symbol": sym, "rejected_count": d["count"]})
    return {
        "static_count": len(BROKER_SYMBOL_MAP["kraken"]),
        "static": sorted(BROKER_SYMBOL_MAP["kraken"]),
        "overrides": overrides,
        "suggestions": suggestions,
    }


@router.post("")
async def add_pair(
    body: dict,
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    base = _normalize_base(body.get("symbol", ""))
    canonical = f"CRYPTO:{base}-USD"
    if canonical in BROKER_SYMBOL_MAP["kraken"]:
        raise HTTPException(
            status_code=409, detail=f"{canonical} is already in the static map",
        )
    existing = await db[KRAKEN_OVERRIDES_COLLECTION].find_one({"_id": canonical})
    if existing:
        raise HTTPException(
            status_code=409, detail=f"{canonical} is already mapped",
        )
    detail = await _kraken_validate(f"{base}USD")
    altname = detail.get("altname") or f"{base}USD"
    now = datetime.now(timezone.utc).isoformat()
    await db[KRAKEN_OVERRIDES_COLLECTION].update_one(
        {"_id": canonical},
        {"$set": {
            "symbol": f"{base}/USD",
            "kraken_pair": altname,
            "ordermin": detail.get("ordermin"),
            "added_by": _user.get("email") or "unknown",
            "ts": now,
        }},
        upsert=True,
    )
    await ensure_kraken_overrides_fresh(force=True)
    return {
        "ok": True,
        "canonical": canonical,
        "kraken_pair": altname,
        "ordermin": detail.get("ordermin"),
        "ts": now,
    }


@router.delete("/{base}")
async def remove_pair(
    base: str,
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    canonical = f"CRYPTO:{_normalize_base(base)}-USD"
    if canonical in BROKER_SYMBOL_MAP["kraken"]:
        raise HTTPException(
            status_code=422,
            detail=f"{canonical} is in the static code map — not removable here",
        )
    res = await db[KRAKEN_OVERRIDES_COLLECTION].delete_one({"_id": canonical})
    if res.deleted_count == 0:
        raise HTTPException(status_code=404, detail=f"{canonical} has no override")
    await ensure_kraken_overrides_fresh(force=True)
    return {"ok": True, "removed": canonical}

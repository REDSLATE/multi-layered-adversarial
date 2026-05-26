"""Cross-brain memory join — Shellys linked together (2026-05-24).

Doctrine:
    Each brain ships its resolved memories to MC via `/api/runtime/shelly/memories`.
    Each brain self-labels via `/api/ingest/memory-labels` (safe / review / quarantine).
    MC owns the unified corpus.

    This endpoint exposes a TOPIC-KEYED CROSS-BRAIN JOIN: any brain
    asking "what memories exist for AAPL?" gets back rows from ALL FOUR
    brains, source-tagged, with two doctrine guarantees:

      1. QUARANTINE CONTAGION
         If ANY brain has filed a `quarantine` label for a memory_id,
         that memory is excluded from the safe peer view — corpus-wide.
         One brain saying "don't train on this" kills it everywhere.
         (Quarantine corpus is still queryable separately for forensics.)

      2. PER-SOURCE WEIGHTING
         Each brain's `safe` rows carry a `source_weight` derived from
         that brain's resolved win rate. A brain with 60% wins earns
         weight 1.20; a brain at 40% earns 0.80. Brains receive
         ready-to-train data with the calibrator's vote baked in.

         The weight formula (operator-tunable via env):
             weight = clamp(0.5, 2.0,  2.0 * win_rate)
         where win_rate is computed from `shared_brain_outcomes` over
         the last 90 days (env: `MEMORY_LINK_WIN_WINDOW_DAYS`).
         Brains with no resolved outcomes default to weight 1.0
         (neutral — neither penalized nor boosted).

Auth: any brain's X-Runtime-Token unlocks the endpoint. Operator may
      revoke a brain's read by rotating its ingest token.

Cache: server-side 60s cache per (symbol, lane). Brains polling on
       heartbeat hit cache 4-6 times per real query.
"""
from __future__ import annotations

import os
import time
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query

from db import db
from namespaces import (
    BRAIN_MEMORIES,
    DISCUSSION_PARTICIPANTS,
    SHARED_MEMORY,
    SHARED_OUTCOMES,
)


router = APIRouter(prefix="/runtime", tags=["cross-brain-memory"])


# Cache config
_CACHE_TTL_S = 60.0
_cache: dict[tuple, tuple[float, dict]] = {}

# Weight clamp + scaling — env tunable so the operator can adjust
# without redeploy.
WEIGHT_MIN = float(os.environ.get("MEMORY_LINK_WEIGHT_MIN", "0.5"))
WEIGHT_MAX = float(os.environ.get("MEMORY_LINK_WEIGHT_MAX", "2.0"))
WEIGHT_SCALE = float(os.environ.get("MEMORY_LINK_WEIGHT_SCALE", "2.0"))
WIN_WINDOW_DAYS = int(os.environ.get("MEMORY_LINK_WIN_WINDOW_DAYS", "90"))


def _resolve_runtime_from_token(token: str) -> Optional[str]:
    for brain in DISCUSSION_PARTICIPANTS:
        expected = os.environ.get(f"{brain.upper()}_INGEST_TOKEN")
        if expected and token == expected:
            return brain
    return None


def _compute_weight(wins: int, losses: int) -> float:
    """Derive a [WEIGHT_MIN, WEIGHT_MAX] weight from win/loss counts.

    Neutral (1.0) when there's no data. Scales linearly with win rate."""
    total = wins + losses
    if total == 0:
        return 1.0
    win_rate = wins / total
    raw = WEIGHT_SCALE * win_rate
    return max(WEIGHT_MIN, min(WEIGHT_MAX, round(raw, 4)))


async def _per_brain_weights() -> dict[str, dict]:
    """Compute the weight table once per request.

    Reads `shared_brain_outcomes` for the last WIN_WINDOW_DAYS, counts
    wins/losses per brain (using the `actual` field — populated by both
    the operator-driven and `auto:market-data` resolvers), returns a
    map of brain -> {wins, losses, win_rate, source_weight}.
    """
    from datetime import datetime, timedelta, timezone
    since = (datetime.now(timezone.utc) - timedelta(days=WIN_WINDOW_DAYS)).isoformat()

    pipeline = [
        {"$match": {"resolved_at": {"$gte": since}}},
        {"$group": {
            "_id": {"brain": "$runtime", "label": "$actual"},
            "count": {"$sum": 1},
        }},
    ]
    counts: dict[str, dict[str, int]] = {}
    async for r in db[SHARED_OUTCOMES].aggregate(pipeline):
        brain = (r["_id"]["brain"] or "").lower()
        label = (r["_id"]["label"] or "").lower()
        if not brain or label not in {"win", "loss"}:
            continue
        bucket = counts.setdefault(brain, {"wins": 0, "losses": 0})
        bucket["wins" if label == "win" else "losses"] += int(r["count"])

    out: dict[str, dict] = {}
    for brain in DISCUSSION_PARTICIPANTS:
        wins = counts.get(brain, {}).get("wins", 0)
        losses = counts.get(brain, {}).get("losses", 0)
        total = wins + losses
        out[brain] = {
            "wins": wins,
            "losses": losses,
            "directional_resolved": total,
            "win_rate": round(wins / total, 4) if total else None,
            "source_weight": _compute_weight(wins, losses),
            "window_days": WIN_WINDOW_DAYS,
        }
    return out


async def _quarantined_memory_ids(symbol: str) -> set[str]:
    """Memory IDs labeled `quarantine` by ANY brain.

    Schema-tightening 2026-05-25:
        New emitters write the `memory_id` FK directly on
        `shared_labeled_memories` (see `MemoryLabelIn` in
        `shared/ingest.py`). The PRIMARY path is now a direct FK
        lookup. Legacy rows (older self-labels that pre-date the FK)
        are picked up via the regex fallback on `payload_summary` +
        `reason` so existing quarantines stay enforced. Both paths
        union into a single set.

        Once every brain emits the FK and the backfill script has
        backstamped legacy rows, the regex fallback can be deleted —
        but it stays on by default until the operator confirms.
    """
    import re
    quarantined: set[str] = set()

    # ── PRIMARY: direct FK ────────────────────────────────────────
    fk_cursor = db[SHARED_MEMORY].find(
        {"label": "quarantine",
         "$or": [
             {"memory_id": {"$exists": True, "$ne": None}},
             {"decision_id": {"$exists": True, "$ne": None}},
         ]},
        {"_id": 0, "memory_id": 1, "decision_id": 1},
    )
    async for row in fk_cursor:
        mid = row.get("memory_id")
        did = row.get("decision_id")
        if mid:
            quarantined.add(str(mid).strip())
        if did:
            quarantined.add(str(did).strip())

    # ── FALLBACK: legacy rows without FK — parse decision_id out of
    # payload_summary / reason. Skipped if BOTH fields are absent on
    # every row (i.e. brains have fully migrated).
    decision_id_re = re.compile(r"decision_id=([A-Za-z0-9_-]+)", re.IGNORECASE)
    legacy_cursor = db[SHARED_MEMORY].find(
        {"label": "quarantine",
         "memory_id": {"$in": [None, ""]},
         "decision_id": {"$in": [None, ""]}},
        {"_id": 0, "payload_summary": 1, "reason": 1},
    )
    async for row in legacy_cursor:
        for field in ("payload_summary", "reason"):
            value = row.get(field) or ""
            for match in decision_id_re.finditer(str(value)):
                quarantined.add(match.group(1).strip())
    return quarantined


async def _memories_for_symbol(symbol: str, lane: Optional[str], limit: int) -> list[dict]:
    """Pull memories from ALL brains matching the symbol. Source-tagged
    by `brain` field already present on each row.

    Uses the text index on `brain_memories.text_summary` if present;
    falls back to regex on symbol field for safety.
    """
    query: dict = {"symbol": symbol.upper()}
    if lane:
        query["lane"] = lane
    cursor = db[BRAIN_MEMORIES].find(
        query,
        {"_id": 0, "memory_id": 1, "decision_id": 1, "brain": 1,
         "symbol": 1, "lane": 1, "decided_at": 1, "decision": 1,
         "resolution": 1, "text_summary": 1, "features": 1},
    ).sort("decided_at", -1).limit(limit)
    return [row async for row in cursor]


async def _build_response(
    symbol: str, lane: Optional[str], limit: int, include_quarantined: bool,
) -> dict:
    """Build the cross-brain joined response."""
    rows = await _memories_for_symbol(symbol, lane, limit)
    quarantined_ids = await _quarantined_memory_ids(symbol)
    weights = await _per_brain_weights()

    safe_rows: list[dict] = []
    quarantined_rows: list[dict] = []
    counts_by_brain: dict[str, int] = {b: 0 for b in DISCUSSION_PARTICIPANTS}

    for row in rows:
        brain = (row.get("brain") or "").lower()
        if brain in counts_by_brain:
            counts_by_brain[brain] += 1
        # Doctrine: quarantine contagion. Check both decision_id and
        # memory_id against the quarantined set.
        ids = {row.get("memory_id"), row.get("decision_id")}
        is_quarantined = any(i in quarantined_ids for i in ids if i)
        weight = weights.get(brain, {}).get("source_weight", 1.0)
        row["source_brain"] = brain
        row["source_weight"] = weight
        row["quarantined"] = is_quarantined
        if is_quarantined:
            quarantined_rows.append(row)
        else:
            safe_rows.append(row)

    response: dict = {
        "symbol": symbol.upper(),
        "lane": lane,
        "counts_by_brain": counts_by_brain,
        "weights_by_brain": weights,
        "quarantine_corpus_size": len(quarantined_ids),
        "peer_memories": safe_rows,
        "safe_count": len(safe_rows),
        "quarantined_count": len(quarantined_rows),
    }
    if include_quarantined:
        response["quarantined_memories"] = quarantined_rows
    return response


# ─────────────────────────── endpoint ───────────────────────────


@router.get("/quarantined-memory-ids")
async def quarantined_memory_ids(
    symbol: Optional[str] = Query(None, max_length=32,
                                  description="Optional symbol filter (currently unused — quarantines are corpus-wide)."),
    x_runtime_token: str | None = Header(default=None, alias="X-Runtime-Token"),
) -> dict:
    """Doctrine-firewall handshake (2026-05-25).

    Returns the union of all memory_ids + decision_ids any brain has
    self-labeled `quarantine`. Brains MUST call this before computing
    the memory modulator and exclude these IDs from their similarity
    pool — otherwise quarantined memories silently influence confidence
    via cosine match, defeating the firewall.

    Doctrine guarantee: if brain A flags a memory as poisoned, brain B's
    modulator MUST NOT train against it. This endpoint is how that
    contract is honored.

    Cached for 30s so the modulator (called on every directional intent)
    doesn't pound Mongo.
    """
    if not x_runtime_token:
        raise HTTPException(status_code=401, detail="X-Runtime-Token required")
    asked_by = _resolve_runtime_from_token(x_runtime_token)
    if not asked_by:
        raise HTTPException(status_code=401, detail="invalid runtime ingest token")

    cache_key = ("__quarantine_only__", symbol)
    now = time.monotonic()
    cached = _cache.get(cache_key)
    if cached and (now - cached[0]) < 30.0:
        return {**cached[1], "asked_by": asked_by, "cache_hit": True}

    ids = await _quarantined_memory_ids(symbol or "")
    payload = {
        "quarantined_ids": sorted(ids),
        "count": len(ids),
        "as_of_monotonic": now,
    }
    _cache[cache_key] = (now, payload)
    return {**payload, "asked_by": asked_by, "cache_hit": False}


@router.get("/memories")
async def cross_brain_memories(
    symbol: str = Query(..., min_length=1, max_length=32),
    lane: Optional[str] = Query(None, pattern="^(crypto|equity|options|futures|fx|unknown)$"),
    limit: int = Query(50, ge=1, le=200),
    include_quarantined: bool = Query(False),
    x_runtime_token: str | None = Header(default=None, alias="X-Runtime-Token"),
) -> dict:
    """Topic-keyed cross-brain memory join.

    Query: `?symbol=AAPL&lane=equity&limit=50`
    Returns memories from all 4 brains for this symbol, source-tagged
    and source-weighted by each brain's recent win rate. Quarantined
    memories are excluded from `peer_memories` unless `include_quarantined`.
    """
    # When called directly (tests), Query defaults may be FieldInfo objects.
    # Coerce to plain values so the function works both as a FastAPI route
    # and as a unit-testable async function.
    if not isinstance(limit, int):
        limit = 50
    if not isinstance(include_quarantined, bool):
        include_quarantined = False
    if lane is not None and not isinstance(lane, str):
        lane = None

    if not x_runtime_token:
        raise HTTPException(status_code=401, detail="X-Runtime-Token required")
    asked_by = _resolve_runtime_from_token(x_runtime_token)
    if not asked_by:
        raise HTTPException(status_code=401, detail="invalid runtime ingest token")

    # Server-side cache keyed on (symbol, lane, limit, include_quarantined).
    # Cross-brain identity doesn't affect the response — all brains see
    # the same data — so the cache key omits asked_by.
    cache_key = (symbol.upper(), lane, limit, include_quarantined)
    now = time.monotonic()
    cached = _cache.get(cache_key)
    if cached and (now - cached[0]) < _CACHE_TTL_S:
        return {**cached[1], "asked_by": asked_by, "cache_hit": True}

    data = await _build_response(symbol, lane, limit, include_quarantined)
    _cache[cache_key] = (now, data)
    return {**data, "asked_by": asked_by, "cache_hit": False}

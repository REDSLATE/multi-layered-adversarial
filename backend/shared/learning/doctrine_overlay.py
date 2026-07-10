"""Doctrine overlay engine — bounded, read-only translator from
approved learning lessons into runtime modifiers.

Doctrine (2026-02-19 operator directive):

    Approved lessons DO NOT mutate doctrine constants. Instead, the
    resolver applies them as BOUNDED OVERLAYS on top of the base
    doctrine value:

        base_notional * get_notional_multiplier(dims)   # 0.80 .. 1.20
        base_threshold + get_threshold_delta(dims)      # -0.20 .. +0.20

    That contract guarantees a noisy lesson can never rewrite the
    foundation — the worst it can do is nudge sizing/thresholds
    within a ±20 % band. A rejected or unapproved lesson has zero
    effect: multiplier = 1.0, delta = 0.0.

Design principles:
    * READ-ONLY. This module never writes to `learning_lessons`,
      `learning_buckets`, or any doctrine collection.
    * NO broker access. Pure Mongo read → pure math.
    * TTL cache so a per-intent lookup doesn't hit Mongo. Refreshed
      on a fixed interval; approvals land within ~60 seconds.
    * EXACT match on bucket dimensions. Fuzzy / partial dim matching
      is a Stage 3+ decision; today an approved lesson applies ONLY
      to intents whose bucket dims match verbatim.
    * NOT WIRED into `auto_router` yet. This module ships standalone
      so its math can be reviewed before being connected to live
      sizing/thresholds.

Callable surface:
    get_notional_multiplier(dims: dict) -> float   # 0.80 .. 1.20
    get_threshold_delta(dims: dict)     -> float   # -0.20 .. +0.20
    reload_overlay_cache_for_tests()               # forces cache miss

`dims` uses the same shape produced by
`shared.learning.bucket_analyzer._extract_bucket_from_experience`:
    {lane, action, notional_source, rvol_band, spread_band, doctrine_band}
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

from shared.learning.bucket_analyzer import _bucket_key
from shared.learning.lesson_proposer import LEARNING_LESSONS

logger = logging.getLogger("shared.learning.doctrine_overlay")

# Hard clamp — even a runaway approval-workflow bug cannot push
# sizing beyond ±20 % of the base doctrine value. Operator directive.
MODIFIER_MIN = -0.20
MODIFIER_MAX = 0.20

# Multiplier band — a rejected/absent lesson yields 1.0 (base sizing);
# an approved edge lesson yields up to 1.20; an approved bleed lesson
# yields down to 0.80.
MULTIPLIER_CENTRE = 1.0
MULTIPLIER_MIN = MULTIPLIER_CENTRE + MODIFIER_MIN  # 0.80
MULTIPLIER_MAX = MULTIPLIER_CENTRE + MODIFIER_MAX  # 1.20

# TTL — approvals land within ~60s; picking a slightly shorter window
# so operator-approved lessons take effect on the next auto-router
# tick without hammering Mongo.
CACHE_TTL_SEC = 30

# Default per-kind modifier magnitude when a lesson is approved but
# doesn't carry an explicit `modifier` value. Bounded by the hard
# clamp above; kept small so the first live overlay is intentionally
# gentle.
DEFAULT_EDGE_MODIFIER = 0.10   # approved edge → +10% (up to +20%)
DEFAULT_BLEED_MODIFIER = -0.10  # approved bleed → -10% (down to -20%)


def _clamp(value: float) -> float:
    """Hard-clamp `value` to the ±0.20 modifier band."""
    if value > MODIFIER_MAX:
        return MODIFIER_MAX
    if value < MODIFIER_MIN:
        return MODIFIER_MIN
    return float(value)


def _dims_id(dims: dict) -> Optional[str]:
    """Compute the same bucket_id the analyzer/proposer use, so we
    can look up approved lessons by their (lane, action, …) tuple.

    Returns None if `dims` is missing any required key — we refuse
    to guess; a partial dim vector might match the wrong lesson.
    """
    required = (
        "lane", "action", "notional_source",
        "rvol_band", "spread_band", "doctrine_band",
    )
    if not isinstance(dims, dict):
        return None
    if any(dims.get(k) is None for k in required):
        return None
    try:
        bid, _ = _bucket_key(**{k: dims[k] for k in required})
    except (TypeError, KeyError):
        return None
    return bid


# ── In-process TTL cache ──────────────────────────────────────────
#
# Structure: {bucket_id: {"kind": "edge"|"bleed", "modifier": float}}
# A missing key means "no approved lesson for this bucket".
_cache: dict[str, dict[str, Any]] = {}
_cache_loaded_at: float = 0.0


def reload_overlay_cache_for_tests() -> None:
    """Force the next call to reload from Mongo. Tests only."""
    global _cache_loaded_at
    _cache_loaded_at = 0.0
    _cache.clear()


async def _ensure_cache_fresh(db) -> None:
    """Reload `_cache` from `learning_lessons` if the TTL has expired.

    Best-effort — a query failure leaves the previous cache in place;
    we never let a Mongo hiccup crash a caller in the sizing path.
    """
    global _cache_loaded_at
    now = time.monotonic()
    if _cache_loaded_at and (now - _cache_loaded_at) < CACHE_TTL_SEC:
        return

    try:
        cur = db[LEARNING_LESSONS].find(
            {"state": "approved"},
            {"_id": 0, "bucket_id": 1, "kind": 1, "proposal": 1, "modifier": 1},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("doctrine_overlay: cache reload query failed: %s", exc)
        return

    new_cache: dict[str, dict[str, Any]] = {}
    try:
        async for row in cur:
            bid = row.get("bucket_id")
            if not bid:
                continue
            kind = row.get("kind")
            # Operator can pin an explicit `modifier` on the lesson
            # doc (approved 0.15 = "boost sizing 15%"). Fall back to
            # the default-per-kind if absent.
            explicit = row.get("modifier")
            if explicit is None:
                explicit = (
                    row.get("proposal", {}).get("modifier")
                    if isinstance(row.get("proposal"), dict)
                    else None
                )
            if explicit is None:
                if kind == "edge":
                    modifier = DEFAULT_EDGE_MODIFIER
                elif kind == "bleed":
                    modifier = DEFAULT_BLEED_MODIFIER
                else:
                    continue
            else:
                try:
                    modifier = float(explicit)
                except (TypeError, ValueError):
                    continue
            new_cache[bid] = {"kind": kind, "modifier": _clamp(modifier)}
    except Exception as exc:  # noqa: BLE001
        logger.warning("doctrine_overlay: cache reload iter failed: %s", exc)
        return

    _cache.clear()
    _cache.update(new_cache)
    _cache_loaded_at = now


async def _lookup_modifier(dims: dict, db) -> float:
    """Return the RAW clamped modifier (in ±0.20) for these dims,
    or 0.0 if no approved lesson matches. Db is required so the
    cache can lazy-refresh."""
    bid = _dims_id(dims)
    if bid is None:
        return 0.0
    await _ensure_cache_fresh(db)
    hit = _cache.get(bid)
    if hit is None:
        return 0.0
    return _clamp(hit.get("modifier", 0.0))


async def get_notional_multiplier(dims: dict, *, db) -> float:
    """Return the notional multiplier for these bucket dims.

    Range: **0.80 .. 1.20** — a value AROUND 1.0, so callers can
    write `notional = base * multiplier` directly without worrying
    about signs.

    Default (no approved lesson matches): 1.0 (no change).
    """
    modifier = await _lookup_modifier(dims, db)
    # Multiplier = 1.0 + clamped modifier. Both bounds enforced.
    m = MULTIPLIER_CENTRE + modifier
    if m > MULTIPLIER_MAX:
        return MULTIPLIER_MAX
    if m < MULTIPLIER_MIN:
        return MULTIPLIER_MIN
    return m


async def get_threshold_delta(dims: dict, *, db) -> float:
    """Return the threshold delta for these bucket dims.

    Range: **-0.20 .. +0.20** — a raw signed modifier for callers
    that want to *add* to a threshold constant (e.g. lowering the
    quality-score floor by 0.10 for an approved edge bucket).

    Default (no approved lesson matches): 0.0 (no change).
    """
    modifier = await _lookup_modifier(dims, db)
    return _clamp(modifier)

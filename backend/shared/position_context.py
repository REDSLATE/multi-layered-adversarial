"""Position context builder — injected into brain decisions.

Doctrine pin (operator directive, post-AAPL incident, 2026-06-XX):

    Brains were previously fed only `action = BUY/SELL`. That layer
    is blind to "what side am I already on for this symbol?" — which
    is exactly what made the AAPL misread possible: 130 BUYs flooded
    against an open SHORT because the brain emit logic treated every
    BUY as "open long" with no awareness of inventory state.

    This module gives the brain a `position_context` per (lane,
    symbol) BEFORE it decides. The brain sees:

        {
          "symbol": "MSFT",
          "current_side": "SHORT",
          "signed_qty": -3,
          "market_value": -1200,
          "unrealized_pl": 45.20,
          "allowed_transitions": [
              "BUY_TO_REDUCE",
              "BUY_TO_CLOSE",
              "SELL_TO_ADD_SHORT",
          ],
        }

    The brain reads `allowed_transitions` and knows: "I am SHORT
    MSFT. BUY does not mean OPEN_LONG, it means REDUCE/COVER. SELL
    means ADD_SHORT." That is the missing piece this layer plugs in.

This module:
    * Reads live broker positions per lane (equity → Public.com or
      Alpaca; crypto → Kraken).
    * Normalizes every position dict via `normalize_position`.
    * Caches the per-lane snapshot for ~10s so the brain runner can
      hit the context lookup on every tick without hammering the
      broker. Cache TTL is intentionally short — brain ticks are
      ~45s, so the cache costs at most one stale read.
    * Returns FLAT context if the broker can't be reached (fail-
      closed: brain treats unknown as FLAT, which means it will
      emit OPEN_LONG / OPEN_SHORT verbs the gate chain can still
      block if the operator has lane execution off).

This is descriptive evidence injected into the brain. It is NOT a
gate. The brain can still emit any action; the new schema fields
just make WHAT THAT ACTION MEANS visible to the operator and to
the audit log.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from shared.position_model import (
    allowed_transitions_for,
    normalize_position,
)


logger = logging.getLogger("risedual.position_context")


# ── Snapshot cache ──────────────────────────────────────────────
# Per-lane cache. Key = lane string ("equity" | "crypto").
# Value = (epoch_seconds_fetched, [normalized_position_dict, ...]).
#
# Doctrine (2026-06-10, P1 follow-up to the 130-trade AAPL incident):
# TTL was 10s but Public.com indexes fills in ~500ms. The 9.5s gap
# was the amnesia window — brains saw FLAT while the broker was
# building inventory. Shortened to 2s for steady-state cheapness and
# wired `invalidate_for_lane()` so the auto-router can punch the cache
# the instant it submits an order, guaranteeing the NEXT brain tick
# sees the freshly-incurred position.
_CACHE: dict[str, tuple[float, list[dict]]] = {}
_CACHE_LOCK = asyncio.Lock()
_CACHE_TTL_SEC = 2.0


def _flat_context(symbol: str, lane: str) -> dict:
    """Return a FLAT context for a symbol we have no broker data on.

    Doctrine: the brain MUST be told something. Returning None here
    would invite the runner to silently skip the injection and we'd
    be back to BUY/SELL-only thinking. FLAT is the honest default —
    any open BUY/SELL the brain emits becomes an OPEN_LONG /
    OPEN_SHORT, which is correct behavior when we genuinely have no
    position on the symbol.
    """
    return {
        "symbol": symbol,
        "lane": lane,
        "current_side": "FLAT",
        "signed_qty": 0.0,
        "qty_abs": 0.0,
        "market_value": None,
        "avg_entry_price": None,
        "unrealized_pl": None,
        "allowed_transitions": allowed_transitions_for("flat"),
        "source": "no_position",
    }


async def _fetch_lane_positions(lane: str) -> list[dict]:
    """Pull raw positions from the broker for this lane and normalize.

    Failure modes — all return an empty list (caller falls through to
    FLAT context per symbol):
        * no adapter configured for lane
        * adapter raises on list_positions
        * adapter returns non-list

    Doctrine: we never raise. The brain runner ticks every 45s; a
    transient broker hiccup must not crash the decision loop.
    """
    try:
        if lane == "equity":
            # Public.com is the sole equity broker (per broker_router
            # doctrine). If unavailable, the equity context degrades
            # to FLAT for every symbol — the gate chain still owns
            # whether the order actually fires.
            from shared.broker_router import _get_equity_adapter  # noqa: WPS433
            adapter = await _get_equity_adapter()
        elif lane == "crypto":
            from shared.crypto.broker_adapter import get_kraken_adapter  # noqa: WPS433
            adapter = await get_kraken_adapter()
        else:
            return []
        if adapter is None:
            return []
        raw = await adapter.list_positions()
        if not isinstance(raw, list):
            return []
        normalized: list[dict] = []
        for item in raw:
            try:
                normalized.append(normalize_position(item))
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "normalize_position failed lane=%s raw=%r err=%s",
                    lane, item, exc,
                )
        return normalized
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "position_context fetch failed lane=%s err=%s — "
            "brains will see FLAT context this tick", lane, exc,
        )
        return []


async def _get_lane_snapshot(lane: str) -> list[dict]:
    """Cached fetch — bounded to one call per lane per ~10s."""
    now = time.time()
    async with _CACHE_LOCK:
        cached = _CACHE.get(lane)
        if cached and (now - cached[0]) < _CACHE_TTL_SEC:
            return cached[1]
    snap = await _fetch_lane_positions(lane)
    async with _CACHE_LOCK:
        _CACHE[lane] = (now, snap)
    return snap


async def get_position_context(symbol: str, lane: str) -> dict:
    """Build the position_context the brain runner injects into the
    brain's snapshot before evaluate().

    Always returns a dict — never None. If we have no live position
    on the symbol, returns the FLAT context.
    """
    sym = (symbol or "").strip()
    if not sym:
        return _flat_context(sym, lane)

    snap = await _get_lane_snapshot(lane)
    # Match on symbol (case-insensitive on equities; crypto symbols
    # already canonicalized by the adapter).
    sym_upper = sym.upper()
    match: Optional[dict] = None
    for p in snap:
        if str(p.get("symbol", "")).upper() == sym_upper:
            match = p
            break

    if match is None or abs(float(match.get("signed_qty") or 0)) <= 1e-9:
        return _flat_context(sym, lane)

    side = str(match.get("side", "FLAT")).lower()
    return {
        "symbol": sym,
        "lane": lane,
        "current_side": match.get("side", "FLAT"),
        "signed_qty": float(match.get("signed_qty") or 0),
        "qty_abs": float(match.get("qty_abs") or 0),
        "market_value": match.get("market_value"),
        "avg_entry_price": match.get("avg_entry_price"),
        "unrealized_pl": match.get("unrealized_pl"),
        "allowed_transitions": allowed_transitions_for(side),
        "source": "broker_live",
    }


def invalidate_cache() -> None:
    """Test hook — wipe the cache so the next call re-fetches."""
    _CACHE.clear()


def invalidate_for_lane(lane: str) -> None:
    """Doctrine (2026-06-10, post-AAPL): the auto-router calls this
    the instant it hands an order to the broker. The next brain tick
    on this lane re-fetches positions fresh — no 2s wait, no 10s wait,
    no amnesia window. Cheap because the next fetch is ~50ms.

    Symmetry with the in-flight order dedupe layer:
        * auto_router submits     → invalidate_for_lane(lane)
        * brain tick re-fetches   → sees fresh broker state on next tick
    """
    _CACHE.pop(lane, None)


__all__ = [
    "get_position_context",
    "invalidate_cache",
    "invalidate_for_lane",
]

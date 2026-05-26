"""Spread-bps enrichment — MC-side fallback ladder.

Doctrine pin (2026-05-26):
    Brains SHOULD ship `spread_bps` in their `doctrine_snapshot`. When
    they don't, MC walks a fallback ladder rather than failing
    RoadGuard with `ROADGUARD_MISSING_SPREAD_BPS` (the silent kill
    that's been muting most of Camaro's intents).

    Ladder (highest trust → lowest):
      1. brain                — value from the intent's snapshot
      2. mc_derived_bid_ask   — computed via canonical formula from
                                snapshot.bid / snapshot.ask
      3. mc_indicator_cache   — most recent `shared_indicator_snapshots`
                                row carrying bid/ask/spread within
                                the freshness window
      4. mc_kraken_public     — crypto only; public Kraken ticker
                                (no auth) when enabled
      5. sentinel_unknown     — `SPREAD_BPS_UNKNOWN = 9999.0`

    The chosen value AND `spread_source` are stamped on the enriched
    snapshot so the operator can audit how MC arrived at the number.
    RoadGuard's existing 50-bps (equity) / 200-bps (crypto) cap logic
    is untouched — if MC's enrichment can only produce the sentinel,
    the gate still fails closed.

Side effects: zero by default. The Kraken fallback is OPT-IN via env
(`SPREAD_FETCH_KRAKEN_ENABLED=true`) so default ingest stays
network-free.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from db import db
from shared.calibration.snapshot_contract import (
    SPREAD_BPS_UNKNOWN,
    compute_spread_bps,
)


logger = logging.getLogger(__name__)


# Operator-tunable freshness window for MC's indicator-cache fallback.
INDICATOR_CACHE_MAX_AGE_S: int = int(
    os.environ.get("SPREAD_INDICATOR_CACHE_MAX_AGE_S", "600"),
)

# Kraken fallback — opt-in (default OFF so ingest stays network-free).
KRAKEN_FALLBACK_ENABLED: bool = (
    os.environ.get("SPREAD_FETCH_KRAKEN_ENABLED", "false").lower() == "true"
)
KRAKEN_FALLBACK_TIMEOUT_S: float = float(
    os.environ.get("SPREAD_FETCH_KRAKEN_TIMEOUT_S", "2.0"),
)


# Diagnostic source tags — stable wire constants. Tripwires assert on these.
SRC_BRAIN = "brain"
SRC_MC_DERIVED = "mc_derived_bid_ask"
SRC_MC_INDICATOR_CACHE = "mc_indicator_cache"
SRC_MC_KRAKEN = "mc_kraken_public"
SRC_SENTINEL = "sentinel_unknown"


def _to_float(value) -> Optional[float]:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def _from_brain_snapshot(snapshot: dict) -> Optional[float]:
    """Step 1: trust the brain's own spread_bps if it's a valid number."""
    raw = snapshot.get("spread_bps")
    if raw is None:
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    # SPREAD_BPS_UNKNOWN from a brain is a "give up" signal — treat it
    # like missing so the ladder keeps trying. The brain's intent was
    # "I don't know" not "the spread is genuinely 99.99%".
    if v == SPREAD_BPS_UNKNOWN:
        return None
    return v


def _from_bid_ask(snapshot: dict) -> Optional[float]:
    """Step 2: derive from snapshot.bid + snapshot.ask."""
    bid = snapshot.get("bid")
    ask = snapshot.get("ask")
    if bid is None or ask is None:
        return None
    v = compute_spread_bps(bid, ask)
    return v if v != SPREAD_BPS_UNKNOWN else None


async def _from_indicator_cache(symbol: str) -> Optional[float]:
    """Step 3: latest indicator snapshot's bid/ask if fresh enough."""
    try:
        from namespaces import SHARED_INDICATOR_SNAPSHOTS  # noqa: WPS433
        row = await db[SHARED_INDICATOR_SNAPSHOTS].find_one(
            {"symbol": symbol.upper()},
            {"_id": 0, "indicators": 1, "computed_at": 1, "last_bar_ts": 1},
            sort=[("computed_at", -1)],
        )
    except Exception:  # noqa: BLE001
        return None
    if not row:
        return None
    # Freshness check.
    try:
        captured = row.get("computed_at") or row.get("last_bar_ts")
        if isinstance(captured, str):
            captured = datetime.fromisoformat(captured.replace("Z", "+00:00"))
        if isinstance(captured, datetime):
            if captured.tzinfo is None:
                captured = captured.replace(tzinfo=timezone.utc)
            age_s = (datetime.now(timezone.utc) - captured).total_seconds()
            if age_s > INDICATOR_CACHE_MAX_AGE_S:
                return None
    except Exception:  # noqa: BLE001
        return None
    ind = row.get("indicators") or {}
    if "spread_bps" in ind:
        try:
            v = float(ind["spread_bps"])
            if 0 <= v < SPREAD_BPS_UNKNOWN:
                return v
        except (TypeError, ValueError):
            pass
    bid = ind.get("bid")
    ask = ind.get("ask")
    if bid is not None and ask is not None:
        v = compute_spread_bps(bid, ask)
        if v != SPREAD_BPS_UNKNOWN:
            return v
    return None


# ── Kraken public-ticker fallback (crypto only) ────────────────────────


def _kraken_pair_for(symbol: str) -> Optional[str]:
    """Translate MC's symbol to Kraken's pair convention.

    `BTC/USD` → `XBTUSD`, `ETH-USDT` → `ETHUSDT`. Returns None for
    unknown symbols rather than guessing — we don't want to query the
    wrong pair.
    """
    if not symbol:
        return None
    s = symbol.upper().replace("/", "").replace("-", "")
    # Kraken's quirky alias: BTC → XBT
    if s.startswith("BTC"):
        s = "X" + s[1:] if s[1:].startswith("BT") else "XBT" + s[3:]
    return s if s.isalnum() else None


async def _from_kraken_public(symbol: str) -> Optional[float]:
    """Step 4 (opt-in): public Kraken ticker. No auth required."""
    if not KRAKEN_FALLBACK_ENABLED:
        return None
    pair = _kraken_pair_for(symbol)
    if not pair:
        return None
    try:
        async with httpx.AsyncClient(timeout=KRAKEN_FALLBACK_TIMEOUT_S) as c:
            r = await c.get(
                "https://api.kraken.com/0/public/Ticker",
                params={"pair": pair},
            )
            r.raise_for_status()
            body = r.json()
            if body.get("error"):
                return None
            # Result shape: {"result": {<pair_actual>: {"a": [ask...], "b": [bid...]}}}
            result = body.get("result") or {}
            for _, payload in result.items():
                a_arr = payload.get("a") or []
                b_arr = payload.get("b") or []
                if not a_arr or not b_arr:
                    continue
                v = compute_spread_bps(b_arr[0], a_arr[0])
                if v != SPREAD_BPS_UNKNOWN:
                    return v
    except Exception as e:  # noqa: BLE001
        logger.debug("kraken fallback failed for %s: %r", symbol, e)
    return None


# ─────────────────── public entry point ───────────────────


async def enrich_snapshot_spread(
    snapshot: dict, *, symbol: str, lane: Optional[str],
) -> tuple[dict, dict]:
    """Walk the fallback ladder until a spread_bps is sourced.

    Always returns:
      (enriched_snapshot, diagnostics)
    where `enriched_snapshot` is a NEW dict (the caller's dict is not
    mutated) with `spread_bps` and `spread_source` set, and
    `diagnostics` carries per-attempt info for the audit row.

    Doctrine: never raises. Worst-case path stamps the SPREAD_BPS_UNKNOWN
    sentinel so RoadGuard fails closed with a specific source tag.
    """
    diag: dict = {"attempts": [], "elapsed_ms": 0}
    t0 = time.monotonic()

    enriched = dict(snapshot or {})

    # Step 1 — brain-supplied.
    v = _from_brain_snapshot(enriched)
    diag["attempts"].append({"source": SRC_BRAIN, "got": v})
    if v is not None:
        enriched["spread_bps"] = v
        enriched["spread_source"] = SRC_BRAIN
        diag["elapsed_ms"] = round((time.monotonic() - t0) * 1000.0, 2)
        return enriched, diag

    # Step 2 — derive from bid/ask if present.
    v = _from_bid_ask(enriched)
    diag["attempts"].append({"source": SRC_MC_DERIVED, "got": v})
    if v is not None:
        enriched["spread_bps"] = v
        enriched["spread_source"] = SRC_MC_DERIVED
        diag["elapsed_ms"] = round((time.monotonic() - t0) * 1000.0, 2)
        return enriched, diag

    # Step 3 — indicator cache.
    v = await _from_indicator_cache(symbol)
    diag["attempts"].append({"source": SRC_MC_INDICATOR_CACHE, "got": v})
    if v is not None:
        enriched["spread_bps"] = v
        enriched["spread_source"] = SRC_MC_INDICATOR_CACHE
        diag["elapsed_ms"] = round((time.monotonic() - t0) * 1000.0, 2)
        return enriched, diag

    # Step 4 — crypto-only Kraken public fallback (opt-in).
    if (lane or "").lower() == "crypto":
        v = await _from_kraken_public(symbol)
        diag["attempts"].append({"source": SRC_MC_KRAKEN, "got": v})
        if v is not None:
            enriched["spread_bps"] = v
            enriched["spread_source"] = SRC_MC_KRAKEN
            diag["elapsed_ms"] = round((time.monotonic() - t0) * 1000.0, 2)
            return enriched, diag

    # Step 5 — sentinel. RoadGuard will fail closed with a specific
    # source tag; the operator can see MC tried but no source had data.
    enriched["spread_bps"] = SPREAD_BPS_UNKNOWN
    enriched["spread_source"] = SRC_SENTINEL
    diag["attempts"].append({"source": SRC_SENTINEL, "got": SPREAD_BPS_UNKNOWN})
    diag["elapsed_ms"] = round((time.monotonic() - t0) * 1000.0, 2)
    return enriched, diag

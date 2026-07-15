"""Live Universe — atomic accessor + writer.

Doctrine (2026-07-15, iter-30, operator-pinned):

    "Whatever Webull or Kraken has for each day as symbols to roll
    with them and not lock down anything. What they offer is what
    we look at. Like their top gainers and losers from the
    session."

    Discovery is broker-driven. Every 15 minutes the refresher
    replaces the active per-lane universe document with a freshly-
    computed list of gainers + losers + most-active (per lane
    doctrine). Operator pins (from `patterns_universe`, pinned=True)
    are merged in so the operator can always ensure a specific
    ticker gets brain attention.

    Atomic swap: the refresher BUILDS off to the side, VALIDATES,
    then replaces the active document in one `replace_one`. A
    refresh failure leaves the previous universe intact — the
    pulse builder MUST NEVER see an empty universe just because
    the screener call timed out.

Shape (see `refresher.py` for the writer):

    {
      "_id": "equity",                  # canonical id — one per lane
      "lane": "equity",
      "generation_id": "equity_20260715T153000Z",
      "refreshed_at": "2026-07-15T15:30:00+00:00",
      "expires_at":  "2026-07-15T15:45:00+00:00",
      "source": "webull_screener",
      "symbols": [
        {
          "canonical_symbol": "NVDA",
          "broker_instrument_id": "913255996",
          "source_reasons": ["most_active", "top_gainer"],
          "rank": 1,
          "change_pct": 4.8,
          "volume": 123456789,
          "pinned": False,
          "tradable": True,
        },
        ...
      ]
    }

Consumers (`mc_pulse.snapshot_service`, feeders) read the WHOLE
document, then use `symbols` for iteration + `generation_id` to
stamp per-tick provenance. All four brains in a single pulse tick
MUST see the same `generation_id`.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from db import db
from namespaces import LIVE_UNIVERSE, UNIVERSE_REFRESH_REPORTS

logger = logging.getLogger("risedual.universe.live")


KNOWN_LANES: tuple[str, ...] = ("equity", "crypto")


def _iso(dt: datetime) -> str:
    return dt.isoformat()


async def read_universe(lane: str) -> Optional[dict]:
    """Return the active universe doc for `lane`, or None if none.

    Returns the raw document (with `_id`, `symbols[]`, generation_id).
    Consumers should be defensive — an empty `symbols` array MUST
    be treated as "no universe today, fall back", never as "no
    symbols allowed today."
    """
    lane_u = (lane or "").lower().strip()
    if lane_u not in KNOWN_LANES:
        return None
    try:
        return await db[LIVE_UNIVERSE].find_one({"_id": lane_u})
    except Exception as exc:  # noqa: BLE001
        logger.warning("read_universe(%s) failed: %s", lane_u, exc)
        return None


async def read_all_universes() -> dict[str, dict]:
    """Return `{lane: doc}` for every known lane. Missing lanes
    resolve to an empty dict entry (`{}`) so callers can uniformly
    check `.get("symbols") or []` without None checks."""
    out: dict[str, dict] = {}
    for lane in KNOWN_LANES:
        doc = await read_universe(lane)
        if doc is not None:
            out[lane] = doc
    return out


async def replace_universe_atomically(
    lane: str,
    generation_id: str,
    symbols: list[dict],
    *,
    source: str,
    refreshed_at: datetime,
    expires_at: datetime,
) -> None:
    """Replace the active universe for `lane` in ONE Mongo op.

    The caller is responsible for having already validated the new
    universe (non-empty when a real universe is expected, all rows
    match the schema, etc.). This function does NOT compare against
    the previous doc — that's the refresher's job (for the report).

    Atomicity note: `replace_one` on a `_id` primary key is a single
    document operation; readers between the refresher's build and
    write phases see the OLD universe. Once the write returns, all
    subsequent reads see the NEW one. There is no in-between state
    where symbols is missing.
    """
    lane_u = (lane or "").lower().strip()
    if lane_u not in KNOWN_LANES:
        raise ValueError(f"unknown lane {lane!r}")
    doc = {
        "_id": lane_u,
        "lane": lane_u,
        "generation_id": generation_id,
        "refreshed_at": _iso(refreshed_at),
        "expires_at": _iso(expires_at),
        "source": source,
        "symbols": symbols,
    }
    await db[LIVE_UNIVERSE].replace_one(
        {"_id": lane_u}, doc, upsert=True,
    )


async def append_refresh_report(report: dict) -> None:
    """Append a refresh report to the audit ledger. Fail-soft —
    never raises."""
    try:
        await db[UNIVERSE_REFRESH_REPORTS].insert_one(report)
    except Exception as exc:  # noqa: BLE001
        logger.warning("append_refresh_report failed: %s", exc)


async def recent_refresh_reports(
    lane: Optional[str] = None, limit: int = 20,
) -> list[dict]:
    """Return the most recent refresh reports (all lanes if `lane`
    is None). Best-effort — returns [] on read error."""
    query: dict = {}
    if lane:
        query["lane"] = (lane or "").lower().strip()
    try:
        cur = (
            db[UNIVERSE_REFRESH_REPORTS]
            .find(query, {"_id": 0})
            .sort("refreshed_at", -1)
            .limit(max(1, min(200, limit)))
        )
        return [d async for d in cur]
    except Exception as exc:  # noqa: BLE001
        logger.warning("recent_refresh_reports failed: %s", exc)
        return []


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def generation_id_for(lane: str, at: datetime) -> str:
    """Build a stable generation_id from lane + timestamp.

    Format: `{lane}_{YYYYMMDD}T{HHMMSS}Z`. Sortable + human-readable
    at a glance; unique per refresh at 1s resolution.
    """
    stamp = at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{lane.lower().strip()}_{stamp}"

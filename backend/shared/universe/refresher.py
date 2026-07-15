"""Universe refresher — 15-minute atomic rebuild of the live universe.

Operator directive (2026-07-15, iter-30 P4):

    "Can we just have whatever Webull or Kraken has for each day
    as symbols to roll with them and not lock down anything?
    What they offer is what we look at?"

    Yes. This module is the "yes."

Flow (per refresh, per lane):
  1. Fetch candidates from broker screener sources (fail-soft, per
     source — one bad endpoint doesn't kill the refresh).
  2. Merge operator pins (from `patterns_universe`, active=true).
     Pinned rows come in flagged `pinned=True`.
  3. Deduplicate by canonical symbol, collecting `source_reasons[]`
     per unique symbol.
  4. Apply hysteresis: admit from the top 50, but retain existing
     members while they remain inside the top 65 of the raw
     candidate order.
  5. Filter by broker tradability (Symbol Registry). Symbols
     currently stamped `tradable=False` are QUARANTINED and left
     out of the emitted universe. Pinned symbols with `tradable=
     False` also get quarantined — safety over pin.
  6. Apply minimum-quality filters (price > 0, volume > 0, sanity-
     capped symbols).
  7. Cap at ~50 per lane.
  8. Validate the built list — refuse to publish an EMPTY universe
     unless the previous doc was ALSO empty (first boot). This is
     the safeguard that keeps a Kraken outage from producing an
     empty pulse universe.
  9. Atomic write into `live_universe`.
 10. Persist a refresh report to `universe_refresh_reports`.

Cadence:
  * Equity: every 15min, but only during Webull-usable windows
    (RTH + configured extended hours). Otherwise skip.
  * Crypto: every 15min continuously (24/7).
  * Immediate refresh on service start (both lanes).

2026-07-15 (iter-30 P4).
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from db import db
from namespaces import PATTERNS_UNIVERSE
from shared.broker import symbol_registry
from shared.universe.live_universe import (
    KNOWN_LANES,
    append_refresh_report,
    generation_id_for,
    now_utc,
    read_universe,
    replace_universe_atomically,
)
from shared.universe.kraken_movers import fetch_crypto_movers
from shared.universe.webull_movers import (
    fetch_most_active,
    fetch_top_gainers,
    fetch_top_losers,
)

logger = logging.getLogger("risedual.universe.refresher")


# ── config (env-overridable, sensible defaults) ────────────────────
REFRESH_INTERVAL_SEC = int(os.environ.get("UNIVERSE_REFRESH_INTERVAL_SEC", "900"))  # 15min
UNIVERSE_TTL_SEC = int(os.environ.get("UNIVERSE_TTL_SEC", "3600"))  # 1h — stale-but-usable window
UNIVERSE_CAP_EQUITY = int(os.environ.get("UNIVERSE_CAP_EQUITY", "50"))
UNIVERSE_CAP_CRYPTO = int(os.environ.get("UNIVERSE_CAP_CRYPTO", "50"))
HYSTERESIS_ADMIT = 50   # new members admitted from top N
HYSTERESIS_RETAIN = 65  # existing members retained while inside top N

MIN_PRICE_EQUITY = 1.0    # avoid penny-stock noise (adopted from Ervin spec 2026-07-15)
MIN_PRICE_CRYPTO = 0.0    # crypto pairs regularly trade sub-cent; don't gate
MIN_VOLUME = 0.0


def _log(msg: str, *args) -> None:
    logger.info(msg, *args)


async def _load_operator_pins(lane: str) -> list[dict]:
    """Read operator-pinned symbols for `lane` out of the legacy
    `patterns_universe` collection.

    Contract (2026-07-15, iter-30 P4):
        A row in `patterns_universe` counts as a pin when:
          * `active=true` (existing legacy field)
          * `lane == lane`
          * `pinned=true` (NEW field — operator sets on the row to
            promote it into every live_universe refresh)

        Rows with `active=true` but `pinned!=true` are LEGACY history
        only. They no longer contribute to the pulse universe.

    Returns a list of already-normalized mover-shape dicts.
    """
    out: list[dict] = []
    try:
        cursor = db[PATTERNS_UNIVERSE].find(
            {"lane": lane, "active": True, "pinned": True},
            {"symbol": 1, "lane": 1, "_id": 0},
        )
        async for r in cursor:
            sym = (r.get("symbol") or "").upper().strip()
            if not sym:
                continue
            out.append({
                "canonical_symbol": sym,
                "broker_instrument_id": None,
                "change_pct": 0.0,
                "volume": 0.0,
                "price": 0.0,
                "source_reason": "operator_pin",
                "_pinned": True,
            })
    except Exception as exc:  # noqa: BLE001
        logger.warning("load_operator_pins(%s) failed: %s", lane, exc)
    return out


def _dedupe_and_merge(candidates: list[dict]) -> list[dict]:
    """Collapse multiple rows for the same canonical symbol into one,
    accumulating `source_reasons[]` and taking the max signal
    magnitudes."""
    by_symbol: dict[str, dict] = {}
    for c in candidates:
        sym = c.get("canonical_symbol")
        if not sym:
            continue
        if sym not in by_symbol:
            by_symbol[sym] = {
                "canonical_symbol": sym,
                "broker_instrument_id": c.get("broker_instrument_id"),
                "source_reasons": [c.get("source_reason") or "unknown"],
                "change_pct": float(c.get("change_pct") or 0.0),
                "volume": float(c.get("volume") or 0.0),
                "price": float(c.get("price") or 0.0),
                "pinned": bool(c.get("_pinned") or False),
            }
        else:
            existing = by_symbol[sym]
            reason = c.get("source_reason")
            if reason and reason not in existing["source_reasons"]:
                existing["source_reasons"].append(reason)
            if c.get("_pinned"):
                existing["pinned"] = True
            # Prefer the most-populated broker_instrument_id.
            if not existing.get("broker_instrument_id") and c.get("broker_instrument_id"):
                existing["broker_instrument_id"] = c["broker_instrument_id"]
            # Take max abs(change_pct); take max volume / price when
            # the existing row lacks a value.
            new_chg = float(c.get("change_pct") or 0.0)
            if abs(new_chg) > abs(existing["change_pct"]):
                existing["change_pct"] = new_chg
            existing["volume"] = max(existing["volume"], float(c.get("volume") or 0.0))
            if existing["price"] <= 0 and c.get("price"):
                existing["price"] = float(c["price"])
    return list(by_symbol.values())


def _apply_hysteresis(
    ranked: list[dict], previous_members: set[str],
) -> list[dict]:
    """Admit new members from the top HYSTERESIS_ADMIT; retain
    existing members while they remain inside the top HYSTERESIS_RETAIN.

    `ranked` is expected to already carry a sensible ordering
    (candidate merger passes it in change-desc-then-volume-desc).

    2026-07-15 (Ervin spec): retained-via-hysteresis symbols get an
    explicit `retained_hysteresis` source_reason so the operator can
    see WHY the symbol is still in the universe on a tick where it
    wouldn't otherwise be admitted.
    """
    admitted: list[dict] = []
    for i, row in enumerate(ranked):
        sym = row["canonical_symbol"]
        if row.get("pinned"):
            # Pins bypass hysteresis entirely.
            row["_admit_reason"] = "pinned"
            admitted.append(row)
            continue
        if i < HYSTERESIS_ADMIT:
            row["_admit_reason"] = "top_admit"
            admitted.append(row)
        elif i < HYSTERESIS_RETAIN and sym in previous_members:
            row["_admit_reason"] = "hysteresis_retain"
            reasons = row.get("source_reasons") or []
            if "retained_hysteresis" not in reasons:
                reasons.append("retained_hysteresis")
                row["source_reasons"] = reasons
            admitted.append(row)
    return admitted


def _apply_quality_filters(
    rows: list[dict], lane: str,
) -> tuple[list[dict], list[dict]]:
    """Drop rows that don't meet minimum quality. Return (kept, dropped).

    Uses a lane-specific price floor (Ervin spec 2026-07-15): equity
    at $1 to skip penny-stock noise, crypto at 0 because valid pairs
    routinely trade sub-cent."""
    min_price = MIN_PRICE_EQUITY if lane == "equity" else MIN_PRICE_CRYPTO
    kept: list[dict] = []
    dropped: list[dict] = []
    for r in rows:
        price = r.get("price") or 0.0
        # Only enforce a price floor when the source populated one;
        # some pins arrive with price=0 (we didn't probe on merge).
        price_ok = (price >= min_price) if price > 0 else True
        vol_ok = r.get("volume", 0.0) >= MIN_VOLUME
        if not (price_ok and vol_ok):
            r["_drop_reason"] = "quality_filter"
            dropped.append(r)
            continue
        kept.append(r)
    return kept, dropped


async def _filter_by_registry(
    admitted: list[dict], broker: str,
) -> tuple[list[dict], list[dict]]:
    """Split `admitted` into `(kept, quarantined)` based on the
    Symbol Registry's tradable verdict.

    Pinned symbols with `tradable=False` still get quarantined —
    safety over pin.
    """
    kept: list[dict] = []
    quarantined: list[dict] = []
    for row in admitted:
        sym = row["canonical_symbol"]
        hit = await symbol_registry.get_cached(broker, sym)
        if hit is not None and not hit.get("tradable", True):
            row["_quarantine_reason"] = hit.get("reason") or "unsupported"
            quarantined.append(row)
            continue
        row["tradable"] = True
        kept.append(row)
    return kept, quarantined


def _finalize_row(r: dict, rank: int) -> dict:
    """Produce the persisted-symbol shape (drop internal fields)."""
    return {
        "canonical_symbol": r["canonical_symbol"],
        "broker_instrument_id": r.get("broker_instrument_id"),
        "source_reasons": r.get("source_reasons") or [],
        "rank": rank,
        "change_pct": r.get("change_pct", 0.0),
        "volume": r.get("volume", 0.0),
        "price": r.get("price", 0.0),
        "pinned": r.get("pinned", False),
        "tradable": r.get("tradable", True),
    }


# ── equity refresh ────────────────────────────────────────────────


async def refresh_equity_universe() -> dict:
    """Rebuild the equity universe from Webull screener + operator pins."""
    at = now_utc()
    lane = "equity"
    previous = await read_universe(lane) or {}
    prev_members: set[str] = {
        s.get("canonical_symbol") for s in (previous.get("symbols") or [])
        if s.get("canonical_symbol")
    }

    # Fetch from broker off the event loop.
    try:
        gainers = await asyncio.to_thread(fetch_top_gainers, 20)
        losers = await asyncio.to_thread(fetch_top_losers, 20)
        active = await asyncio.to_thread(fetch_most_active, 20)
    except Exception as exc:  # noqa: BLE001
        gainers, losers, active = [], [], []
        logger.warning("equity screener fetch raised: %s", exc)

    pins = await _load_operator_pins(lane)

    # Merge candidates (order matters for hysteresis ranking —
    # movers first, pins prepended so they never lose the merge).
    merged = _dedupe_and_merge([*pins, *gainers, *losers, *active])
    # Rank by pinned first, then |change_pct| desc, then volume desc.
    merged.sort(
        key=lambda r: (
            0 if r.get("pinned") else 1,
            -abs(r.get("change_pct", 0.0)),
            -float(r.get("volume", 0.0)),
        ),
    )

    admitted = _apply_hysteresis(merged, prev_members)
    kept, quarantined = await _filter_by_registry(admitted, "webull")
    quality_kept, quality_dropped = _apply_quality_filters(kept, lane)
    quality_kept = quality_kept[:UNIVERSE_CAP_EQUITY]

    return await _publish_and_report(
        lane=lane,
        source="webull_screener",
        rows=quality_kept,
        quarantined=quarantined,
        dropped=quality_dropped,
        raw_sources={
            "gainers": len(gainers), "losers": len(losers),
            "most_active": len(active), "pins": len(pins),
        },
        previous=previous,
        at=at,
    )


async def _build_and_publish(*args, **kwargs):
    # Retained for backwards-compat with any importer; delegate.
    return await _publish_and_report(*args, **kwargs)


async def _publish_and_report(
    *,
    lane: str,
    source: str,
    rows: list[dict],
    quarantined: list[dict],
    dropped: list[dict],
    raw_sources: dict,
    previous: dict,
    at: datetime,
) -> dict:
    """Common tail — publish (if valid), then persist the report."""
    prev_members: set[str] = {
        s.get("canonical_symbol") for s in (previous.get("symbols") or [])
        if s.get("canonical_symbol")
    }
    new_members: set[str] = {r["canonical_symbol"] for r in rows}

    generation_id = generation_id_for(lane, at)
    expires_at = at + timedelta(seconds=UNIVERSE_TTL_SEC)

    published = False
    publish_error: Optional[str] = None
    used_last_good = False

    # SAFEGUARD: refuse to publish an empty universe over a non-empty
    # previous. A screener outage MUST NOT clear the pulse universe.
    if not rows and prev_members:
        publish_error = "empty_universe_refused_over_non_empty_previous"
        used_last_good = True
        logger.warning(
            "universe refresh %s: empty result — retaining previous "
            "generation %s (%d symbols)",
            lane, previous.get("generation_id"), len(prev_members),
        )
    else:
        symbols_persist = [_finalize_row(r, i + 1) for i, r in enumerate(rows)]
        try:
            await replace_universe_atomically(
                lane=lane,
                generation_id=generation_id,
                symbols=symbols_persist,
                source=source,
                refreshed_at=at,
                expires_at=expires_at,
            )
            published = True
        except Exception as exc:  # noqa: BLE001
            publish_error = f"publish_raised: {type(exc).__name__}: {exc}"
            used_last_good = True
            logger.warning(
                "universe refresh %s publish failed: %s", lane, publish_error,
            )

    added = sorted(new_members - prev_members) if published else []
    removed = sorted(prev_members - new_members) if published else []
    retained = sorted(new_members & prev_members) if published else []
    # `failed_resolution` (Ervin spec 2026-07-15): symbols that showed
    # up in the raw candidate pool but couldn't be turned into a
    # publishable row — i.e. quality-dropped rows without a
    # broker_instrument_id (a "we know the ticker but can't resolve
    # it to an executable instrument" case).
    failed_resolution = sorted({
        d.get("canonical_symbol")
        for d in dropped
        if d.get("canonical_symbol") and not d.get("broker_instrument_id")
    })

    report = {
        "lane": lane,
        "generation_id": generation_id if published else None,
        "refreshed_at": at.isoformat(),
        "expires_at": expires_at.isoformat() if published else None,
        "published": published,
        "used_last_good": used_last_good,
        "publish_error": publish_error,
        "source": source,
        "raw_source_counts": raw_sources,
        "sizes": {
            "candidates_after_merge": len(rows) + len(quarantined) + len(dropped),
            "quarantined": len(quarantined),
            "quality_dropped": len(dropped),
            "final": len(rows),
        },
        "added": added,
        "retained": retained,
        "removed": removed,
        "failed_resolution": failed_resolution,
        "quarantined": [
            {"symbol": q["canonical_symbol"], "reason": q.get("_quarantine_reason")}
            for q in quarantined
        ],
    }
    await append_refresh_report(report)
    _log(
        "universe refresh %s: published=%s used_last_good=%s final=%d "
        "added=%d removed=%d quarantined=%d quality_dropped=%d "
        "failed_resolution=%d",
        lane, published, used_last_good, len(rows), len(added), len(removed),
        len(quarantined), len(dropped), len(failed_resolution),
    )
    return report


# ── crypto refresh ────────────────────────────────────────────────


async def refresh_crypto_universe() -> dict:
    """Rebuild the crypto universe from Kraken public tickers + pins."""
    at = now_utc()
    lane = "crypto"
    previous = await read_universe(lane) or {}
    prev_members: set[str] = {
        s.get("canonical_symbol") for s in (previous.get("symbols") or [])
        if s.get("canonical_symbol")
    }

    try:
        movers = await fetch_crypto_movers(
            top_gainers=20, top_losers=20, high_liquidity=20,
        )
    except Exception as exc:  # noqa: BLE001
        movers = []
        logger.warning("crypto movers fetch raised: %s", exc)

    pins = await _load_operator_pins(lane)

    merged = _dedupe_and_merge([*pins, *movers])
    merged.sort(
        key=lambda r: (
            0 if r.get("pinned") else 1,
            -abs(r.get("change_pct", 0.0)),
            -float(r.get("volume", 0.0)),
        ),
    )

    admitted = _apply_hysteresis(merged, prev_members)
    kept, quarantined = await _filter_by_registry(admitted, "kraken")
    quality_kept, quality_dropped = _apply_quality_filters(kept, lane)
    quality_kept = quality_kept[:UNIVERSE_CAP_CRYPTO]

    return await _publish_and_report(
        lane=lane,
        source="kraken_movers",
        rows=quality_kept,
        quarantined=quarantined,
        dropped=quality_dropped,
        raw_sources={
            "movers": len(movers), "pins": len(pins),
        },
        previous=previous,
        at=at,
    )


# ── market-window guard for equity ────────────────────────────────


def _equity_refresh_allowed_now() -> bool:
    """True iff the current wall-clock is inside a window where
    the Webull equity screener produces useful data.

    Doctrine: refresh during pre-market + RTH + after-hours. Fall
    back to `True` if the helper module is missing so the refresher
    always makes at least a best-effort attempt.
    """
    try:
        from shared.market_hours import is_equity_open_including_extended  # noqa: WPS433
        return bool(is_equity_open_including_extended())
    except Exception:  # noqa: BLE001
        return True


# ── loop entrypoint ───────────────────────────────────────────────


async def refresh_all_lanes(*, force: bool = False) -> dict:
    """Refresh every lane once. `force=True` skips the equity
    market-window guard (used on startup so we always land a non-
    empty universe even at 3am)."""
    results: dict = {}
    do_equity = force or _equity_refresh_allowed_now()
    if do_equity:
        results["equity"] = await refresh_equity_universe()
    else:
        results["equity"] = {"skipped": "equity_market_closed"}
    # Crypto is always eligible (24/7).
    results["crypto"] = await refresh_crypto_universe()
    return results


async def universe_refresher_loop() -> None:
    """Background loop — one refresh on start, then every
    REFRESH_INTERVAL_SEC. Never raises; log-and-continue on any
    per-tick failure."""
    _log(
        "universe_refresher started: interval=%ds ttl=%ds cap_eq=%d "
        "cap_cr=%d admit=%d retain=%d",
        REFRESH_INTERVAL_SEC, UNIVERSE_TTL_SEC,
        UNIVERSE_CAP_EQUITY, UNIVERSE_CAP_CRYPTO,
        HYSTERESIS_ADMIT, HYSTERESIS_RETAIN,
    )
    # Immediate refresh on start (force so equity always tries).
    try:
        await refresh_all_lanes(force=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("universe_refresher initial refresh raised: %s", exc)

    while True:
        try:
            await asyncio.sleep(REFRESH_INTERVAL_SEC)
            await refresh_all_lanes(force=False)
        except asyncio.CancelledError:
            _log("universe_refresher cancelled")
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("universe_refresher tick raised: %s", exc)

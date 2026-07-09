"""Outcome resolver — updates `learning_experiences` rows with post-trade
P&L attribution once time has passed.

For each captured experience the resolver walks the horizon ladder:
    * 5-minute mark  → `outcome_5m_bps`
    * 15-minute mark → `outcome_15m_bps`
    * 1-hour mark    → `outcome_1h_bps`

`bps` = signed basis points of price move from the entry price,
sign-flipped by intent direction so a "correct" SELL that went DOWN
produces a POSITIVE bps.

Design:
    * Idempotent — a single horizon field is written once and only
      once. If a resolution attempt fails (e.g. broker outage), the
      row stays "pending" and gets retried on the next sweep.
    * Two-layer skip: we skip rows still under the horizon age AND
      rows where the field is already populated. So the resolver
      can safely be called back-to-back without redundant work.
    * Crypto-first: Stage 1 supports Kraken ticker lookups end-to-
      end. Stage 2 wires the equity mark-price feed with a two-tier
      fallback:
          1. Webull v2 equity_snapshot (primary — same vendor as
             execution, natural alignment with fill prices).
          2. `shared_ohlcv_bars` latest-bar close (fallback —
             covers the case where Webull quotes are throttled or
             the market-data session hasn't logged in yet; also
             the Polygon flat-file / daily feeder's landing zone,
             so this doubles as the Polygon last-quote fallback
             the operator asked for).
      Both tiers are best-effort; a total miss still lands as
      `skipped_missing_mark` and the row stays pending for a
      future sweep.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from shared.learning.live_loop import LEARNING_EXPERIENCES

logger = logging.getLogger("shared.learning.outcome_resolver")

# Horizon table: field name → age in seconds before we attempt
# resolution. Ordered so the loop walks 5m → 15m → 1h and stops
# at the first "not yet ripe" field per row.
HORIZONS: list[tuple[str, str, int]] = [
    ("outcome_5m_bps",  "outcome_resolved_at_5m",  5 * 60),
    ("outcome_15m_bps", "outcome_resolved_at_15m", 15 * 60),
    ("outcome_1h_bps",  "outcome_resolved_at_1h",  60 * 60),
]

# Per-sweep cap so a fresh backfill doesn't hammer Kraken or Polygon
# on the first tick after deploy. Adjust once we know steady-state
# volume.
RESOLVER_BATCH_CAP = 200


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _bps(entry: float, mark: float, action: str) -> float:
    """Signed basis-points-of-move relative to entry price.

    action=BUY  → positive when mark > entry (we went UP after buying)
    action=SELL → positive when mark < entry (we went DOWN after selling)
    """
    if not entry or entry <= 0:
        return 0.0
    raw = (mark - entry) / entry * 10_000.0
    return raw if action == "BUY" else -raw


async def _fetch_equity_mark_webull(symbol: str) -> Optional[float]:
    """Primary equity mark source — Webull v2 `equity_snapshot`.

    Webull's snapshot dict shape (per `shared/broker/webull.py:414`):
        {"price": <last_trade>, "ask": <ask>, "bid": <bid>, ...}
    We prefer `price` (last trade) over `ask` so the mark reflects
    execution, not top-of-book quote. Runs the sync SDK call off
    the event loop via `asyncio.to_thread`.
    """
    try:
        from shared.market_data.webull_quotes import get_quotes_client  # noqa: WPS433
    except Exception as exc:  # noqa: BLE001
        logger.debug("outcome_resolver: webull_quotes import failed: %s", exc)
        return None

    def _lookup() -> Optional[float]:
        client = get_quotes_client()
        if client is None:
            return None
        snap = client.equity_snapshot(symbol) or {}
        for key in ("price", "last", "lastPrice", "deal_price", "ask"):
            v = snap.get(key)
            if v is None:
                continue
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            if fv > 0.0:
                return fv
        return None

    try:
        return await asyncio.to_thread(_lookup)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "outcome_resolver: webull equity mark failed symbol=%s: %s",
            symbol, exc,
        )
        return None


async def _fetch_equity_mark_bars_fallback(symbol: str) -> Optional[float]:
    """Fallback equity mark — latest close from `shared_ohlcv_bars`.

    The federated bar store is populated by the Polygon grouped-daily
    feeder AND (when it lands) the flatfiles feeder, so hitting Mongo
    here is effectively "Polygon last-known price without spending a
    live API call". Any source / any tf — we take whatever is fresh.

    This is authoritative-enough for a 5m/15m/1h P&L attribution when
    Webull is unavailable; stale-close bias is smaller than the noise
    floor at these horizons for the universe MC actually trades.
    """
    try:
        from db import db  # noqa: WPS433
        from namespaces import SHARED_OHLCV_BARS  # noqa: WPS433
    except Exception as exc:  # noqa: BLE001
        logger.debug("outcome_resolver: db import failed: %s", exc)
        return None

    try:
        row = await db[SHARED_OHLCV_BARS].find_one(
            {"symbol": symbol},
            {"_id": 0, "c": 1, "ts": 1, "source": 1, "tf": 1},
            sort=[("ts", -1)],
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "outcome_resolver: bars fallback query failed symbol=%s: %s",
            symbol, exc,
        )
        return None

    if not row:
        return None
    close = row.get("c")
    try:
        fv = float(close) if close is not None else None
    except (TypeError, ValueError):
        return None
    if fv is None or fv <= 0.0:
        return None
    return fv


async def _fetch_mark_price(lane: str, symbol: str) -> Optional[float]:
    """Return the current mark price for `symbol` on `lane`.

    Crypto: uses the existing Kraken public ticker helper.
    Equity: two-tier — Webull v2 last-trade primary, `shared_ohlcv_bars`
            latest-close fallback (Polygon-fed).
    """
    if not symbol:
        return None
    lane = (lane or "").lower()

    if lane == "crypto":
        try:
            from shared.crypto.broker_adapter import _ticker_price  # noqa: WPS433
            # `_ticker_price` accepts pair keys like "XBTUSD"; the
            # symbol convention in learning_experiences varies
            # (`BTC/USD`, `ETH/USD`). Normalise slashes out.
            pair = symbol.replace("/", "").upper()
            if pair.startswith("BTC"):  # Kraken quirk
                pair = pair.replace("BTC", "XBT", 1)
            return float(await _ticker_price(pair))
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "outcome_resolver: crypto mark fetch failed "
                "symbol=%s: %s", symbol, exc,
            )
            return None

    if lane == "equity":
        sym = symbol.upper().strip()
        mark = await _fetch_equity_mark_webull(sym)
        if mark is not None:
            return mark
        return await _fetch_equity_mark_bars_fallback(sym)

    return None


async def resolve_pending_outcomes(db) -> dict:
    """Walk pending `learning_experiences` and populate any horizon
    field whose age is ripe and whose entry price is known.

    Returns a counts dict for observability. Never raises.
    """
    counts = {
        "scanned": 0,
        "resolved_5m": 0,
        "resolved_15m": 0,
        "resolved_1h": 0,
        "skipped_missing_entry": 0,
        "skipped_missing_mark": 0,
        "errors": 0,
    }
    now = _now()
    # Row is "ripe" if it's older than the earliest horizon (5m). No
    # point scanning fresh rows; they can't be resolved yet.
    max_ripe_ts = (now - timedelta(seconds=HORIZONS[0][2])).isoformat()

    try:
        cur = (
            db[LEARNING_EXPERIENCES]
            .find(
                {
                    "created_at": {"$lt": max_ripe_ts},
                    # At least one horizon field still unresolved.
                    "$or": [
                        {"outcome_5m_bps": None},
                        {"outcome_15m_bps": None},
                        {"outcome_1h_bps": None},
                    ],
                    # Cannot resolve without an entry anchor.
                    "entry_price": {"$exists": True, "$ne": None},
                },
                {
                    "_id": 0, "intent_id": 1, "symbol": 1, "lane": 1,
                    "action": 1, "entry_price": 1, "created_at": 1,
                    "outcome_5m_bps": 1, "outcome_15m_bps": 1,
                    "outcome_1h_bps": 1,
                },
            )
            .sort("created_at", 1)
            .limit(RESOLVER_BATCH_CAP)
        )
        rows: list[dict] = []
        async for row in cur:
            rows.append(row)
    except Exception as exc:  # noqa: BLE001
        logger.warning("outcome_resolver: query failed: %s", exc)
        return counts

    # Group rows by (lane, symbol) so we fetch each mark ONCE and
    # apply it to every ripe horizon for every row using that price.
    for row in rows:
        counts["scanned"] += 1
        entry = row.get("entry_price")
        if entry is None:
            counts["skipped_missing_entry"] += 1
            continue

        # Age of this row — determines which horizons are ripe.
        try:
            created = datetime.fromisoformat(
                row["created_at"].replace("Z", "+00:00")
            )
        except (KeyError, ValueError, AttributeError):
            counts["errors"] += 1
            continue
        age_s = (now - created).total_seconds()

        # Compute which horizon fields are ripe AND still empty.
        pending: list[tuple[str, str]] = []
        for field, resolved_at_field, horizon_s in HORIZONS:
            if age_s < horizon_s:
                break  # not ripe yet — every later horizon is even later
            if row.get(field) is not None:
                continue  # already resolved
            pending.append((field, resolved_at_field))

        if not pending:
            continue

        mark = await _fetch_mark_price(row.get("lane"), row.get("symbol"))
        if mark is None:
            counts["skipped_missing_mark"] += 1
            continue

        bps = _bps(float(entry), mark, row.get("action") or "BUY")
        # For a first-cut win/loss signal: use the 5m horizon as the
        # "did the move immediately go our way" flag. 15m/1h refine
        # later. When 5m resolves, stamp the `win` field too.
        set_payload: dict[str, Any] = {}
        stamp_ts = _now().isoformat()
        for field, resolved_at_field in pending:
            set_payload[field] = bps
            set_payload[resolved_at_field] = stamp_ts
            if field == "outcome_5m_bps":
                set_payload["win"] = bps > 0
                counts["resolved_5m"] += 1
            elif field == "outcome_15m_bps":
                counts["resolved_15m"] += 1
            elif field == "outcome_1h_bps":
                counts["resolved_1h"] += 1

        try:
            await db[LEARNING_EXPERIENCES].update_one(
                {"intent_id": row["intent_id"]},
                {"$set": set_payload},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "outcome_resolver: update failed intent_id=%s: %s",
                row["intent_id"], exc,
            )
            counts["errors"] += 1

    if counts["scanned"]:
        logger.info(
            "learning.outcome_resolver: scanned=%d resolved(5m=%d 15m=%d 1h=%d) "
            "skipped_entry=%d skipped_mark=%d errors=%d",
            counts["scanned"], counts["resolved_5m"], counts["resolved_15m"],
            counts["resolved_1h"], counts["skipped_missing_entry"],
            counts["skipped_missing_mark"], counts["errors"],
        )
    return counts

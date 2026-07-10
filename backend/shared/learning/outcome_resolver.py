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

Mark-price contract (Stage 2 finisher — operator directive):

    The resolver ONLY writes an outcome bps when the mark price is
    FRESH. A stale mark (e.g. Polygon previous-day close during
    intraday hours) can badly mislabel a 5-minute outcome, so the
    resolver leaves such rows pending for a future sweep and stamps
    the diagnostic details on the row for auditing.

    `_fetch_mark_quote(lane, symbol)` returns `MarkQuote(price, source,
    ts, is_stale)` where `is_stale=True` means the value is real but
    doesn't reflect the market at horizon-close time.

    Equity fallback chain:
        1. Webull v2 last-trade (fresh, source="webull_last_trade")
        2. `shared_ohlcv_bars` latest close, iff the bar's ts is
           within `BAR_FRESH_WINDOW_SEC` of now (fresh,
           source="ohlcv_bars_intraday")
        3. Polygon `/v2/aggs/ticker/{ticker}/prev` previous close —
           ALWAYS marked stale (source="polygon_prev_close").
           Included as a diagnostic-only tier so audit logs can
           still show *something* when both live tiers miss.
        4. None — nothing available at all.

    Crypto: Kraken public ticker — fresh, source="kraken_ticker".

    The horizon only resolves when `mark is not None AND is_stale
    is False`. Stale marks bump `skipped_stale_mark` instead of
    resolving the row.

    The resolver stamps `mark_price / mark_price_source /
    mark_price_ts` alongside the outcome so downstream analysis
    knows the provenance of each attribution.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, NamedTuple, Optional

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

# Freshness window for the `shared_ohlcv_bars` fallback. A bar older
# than this at query time is treated as STALE even if it's the most
# recent row we have. 30 minutes gives us enough slack to cover the
# 5m/15m horizons (bars_ts must be within ~half a horizon width of
# now) without letting yesterday's close leak into today's outcome.
BAR_FRESH_WINDOW_SEC = int(
    os.environ.get("LEARNING_BAR_FRESH_WINDOW_SEC", str(30 * 60))
)


class MarkQuote(NamedTuple):
    """A single mark-price observation with provenance.

    price:     the numeric mark. Always > 0.0 when non-None.
    source:    provenance tag (webull_last_trade / ohlcv_bars_intraday /
               polygon_prev_close / kraken_ticker).
    ts:        ISO-8601 UTC timestamp of the underlying market data
               point (NOT the query time — the actual bar/quote time).
    is_stale:  True iff the value is real but too old to reflect the
               current market. The resolver refuses to resolve a
               horizon on a stale mark.
    """

    price: float
    source: str
    ts: str
    is_stale: bool


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


# ═══════════════════════════════════════════════════════════════════
#  Equity tier 1 — Webull live last-trade
# ═══════════════════════════════════════════════════════════════════


async def _fetch_equity_mark_webull(symbol: str) -> Optional[MarkQuote]:
    """Primary equity mark source — Webull v2 `equity_snapshot`.

    Webull's snapshot dict shape (per `shared/broker/webull.py:414`):
        {"price": <last_trade>, "ask": <ask>, "bid": <bid>, ...}
    We prefer `price` (last trade) over top-of-book quote so the mark
    reflects execution, not indicative. Runs the sync SDK call off
    the event loop via `asyncio.to_thread`.

    Returned quote is marked fresh (is_stale=False) — Webull's
    snapshot IS the live tape during trading hours; if the operator
    is running the resolver outside market hours the answer is still
    the last trade Webull knows about, which for a 5m/15m/1h horizon
    on a just-captured intent (created intraday) is what we want.
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
        price = await asyncio.to_thread(_lookup)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "outcome_resolver: webull equity mark failed symbol=%s: %s",
            symbol, exc,
        )
        return None

    if price is None:
        return None
    return MarkQuote(
        price=price,
        source="webull_last_trade",
        ts=_now().isoformat(),
        is_stale=False,
    )


# ═══════════════════════════════════════════════════════════════════
#  Equity tier 2 — shared_ohlcv_bars intraday close (freshness gated)
# ═══════════════════════════════════════════════════════════════════


async def _fetch_equity_mark_bars(symbol: str) -> Optional[MarkQuote]:
    """Fallback equity mark — most recent `shared_ohlcv_bars` row.

    We accept the newest bar regardless of tf (5m/15m/1d) but compute
    STALENESS off its `ts`. A 5m bar from 10 minutes ago is fresh;
    a daily bar from yesterday's close is stale. The resolver refuses
    to resolve a horizon on the stale case.
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

    bar_ts_raw = row.get("ts")
    ts_iso: str
    is_stale: bool
    if isinstance(bar_ts_raw, str):
        try:
            bar_ts = datetime.fromisoformat(bar_ts_raw.replace("Z", "+00:00"))
        except ValueError:
            # Unparseable timestamp → treat as stale (safer than
            # accidentally resolving on garbage).
            return MarkQuote(
                price=fv, source="ohlcv_bars_unknown_ts",
                ts=bar_ts_raw, is_stale=True,
            )
        age_s = (_now() - bar_ts).total_seconds()
        is_stale = age_s > BAR_FRESH_WINDOW_SEC
        ts_iso = bar_ts_raw
    else:
        # ts missing or not a string — cannot verify freshness.
        return MarkQuote(
            price=fv, source="ohlcv_bars_unknown_ts",
            ts="", is_stale=True,
        )

    return MarkQuote(
        price=fv,
        source=(
            "ohlcv_bars_intraday" if not is_stale
            else "ohlcv_bars_stale"
        ),
        ts=ts_iso,
        is_stale=is_stale,
    )


# ═══════════════════════════════════════════════════════════════════
#  Equity tier 3 — Polygon /v2/aggs/ticker/{t}/prev (DIAGNOSTIC ONLY)
# ═══════════════════════════════════════════════════════════════════


_POLYGON_BASE_URL = "https://api.polygon.io"


async def _fetch_equity_mark_polygon_prev(symbol: str) -> Optional[MarkQuote]:
    """Polygon previous-day close — always marked stale.

    The Starter plan does NOT authorise real-time last-trade, so the
    freshest thing Polygon can give us is yesterday's close. That
    number is fine for a diagnostic breadcrumb ("Polygon says the
    stock closed at X yesterday") but MUST NOT be used to resolve a
    5m/15m/1h intraday outcome — operator directive 2026-02-19.

    Included in the fallback chain so audit logs and the mark-source
    histogram can distinguish "Webull was down AND we had no
    intraday bar" from "we never even tried Polygon".
    """
    api_key = (os.environ.get("POLYGON_API_KEY") or "").strip()
    if not api_key:
        return None

    try:
        import httpx  # noqa: WPS433
    except Exception as exc:  # noqa: BLE001
        logger.debug("outcome_resolver: httpx import failed: %s", exc)
        return None

    path = f"/v2/aggs/ticker/{symbol}/prev"
    try:
        async with httpx.AsyncClient(
            base_url=_POLYGON_BASE_URL,
            timeout=httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0),
        ) as client:
            resp = await client.get(
                path,
                params={"adjusted": "true", "apiKey": api_key},
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "outcome_resolver: polygon prev fetch failed symbol=%s: %s",
            symbol, exc,
        )
        return None

    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        return None
    results = data.get("results") or []
    if not results:
        return None
    row = results[0]
    close = row.get("c")
    ts_ms = row.get("t")
    try:
        fv = float(close) if close is not None else None
    except (TypeError, ValueError):
        return None
    if fv is None or fv <= 0.0:
        return None
    ts_iso = ""
    if isinstance(ts_ms, (int, float)):
        try:
            ts_iso = datetime.fromtimestamp(
                ts_ms / 1000.0, tz=timezone.utc,
            ).isoformat()
        except (ValueError, OSError):
            ts_iso = ""
    return MarkQuote(
        price=fv,
        source="polygon_prev_close",
        ts=ts_iso,
        is_stale=True,  # ALWAYS — plan-tier constraint
    )


# ═══════════════════════════════════════════════════════════════════
#  Crypto — Kraken public ticker
# ═══════════════════════════════════════════════════════════════════


async def _fetch_crypto_mark_kraken(symbol: str) -> Optional[MarkQuote]:
    """Crypto mark from Kraken public ticker. Real-time; always fresh."""
    try:
        from shared.crypto.broker_adapter import _ticker_price  # noqa: WPS433
        pair = symbol.replace("/", "").upper()
        if pair.startswith("BTC"):  # Kraken quirk — BTC → XBT
            pair = pair.replace("BTC", "XBT", 1)
        price = float(await _ticker_price(pair))
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "outcome_resolver: crypto mark fetch failed symbol=%s: %s",
            symbol, exc,
        )
        return None
    if price <= 0.0:
        return None
    return MarkQuote(
        price=price,
        source="kraken_ticker",
        ts=_now().isoformat(),
        is_stale=False,
    )


# ═══════════════════════════════════════════════════════════════════
#  Chain composer
# ═══════════════════════════════════════════════════════════════════


async def _fetch_mark_quote(lane: str, symbol: str) -> Optional[MarkQuote]:
    """Return the best available mark quote for `symbol` on `lane`.

    Walks the tier chain; the FIRST fresh quote wins. If no fresh
    quote exists, the LAST tier's result (which may be stale) is
    returned so the caller has a diagnostic breadcrumb.

    Callers must check `q.is_stale` before treating the price as
    authoritative for outcome resolution.
    """
    if not symbol:
        return None
    lane = (lane or "").lower()

    if lane == "crypto":
        return await _fetch_crypto_mark_kraken(symbol)

    if lane == "equity":
        sym = symbol.upper().strip()
        last_seen: Optional[MarkQuote] = None
        for tier in (
            _fetch_equity_mark_webull,
            _fetch_equity_mark_bars,
            _fetch_equity_mark_polygon_prev,
        ):
            q = await tier(sym)
            if q is None:
                continue
            if not q.is_stale:
                return q  # first fresh wins
            last_seen = q  # remember the most-recent stale tier
        return last_seen  # None or a stale diagnostic quote

    return None


# ═══════════════════════════════════════════════════════════════════
#  Legacy shim — preserves the pre-Stage-2 `_fetch_mark_price` shape
#  for callers/tests that only care about a fresh float.
# ═══════════════════════════════════════════════════════════════════


async def _fetch_mark_price(lane: str, symbol: str) -> Optional[float]:
    """Backward-compat shim. Returns the price ONLY when fresh, else None.

    New code should use `_fetch_mark_quote` for source/ts context.
    """
    q = await _fetch_mark_quote(lane, symbol)
    if q is None or q.is_stale:
        return None
    return q.price


# ═══════════════════════════════════════════════════════════════════
#  Main entry — resolve pending horizons
# ═══════════════════════════════════════════════════════════════════


async def resolve_pending_outcomes(db) -> dict:
    """Walk pending `learning_experiences` and populate any horizon
    field whose age is ripe AND whose mark price is fresh.

    Returns a counts dict for observability. Never raises.
    """
    counts = {
        "scanned": 0,
        "resolved_5m": 0,
        "resolved_15m": 0,
        "resolved_1h": 0,
        "skipped_missing_entry": 0,
        "skipped_missing_mark": 0,
        "skipped_stale_mark": 0,
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
                    "$or": [
                        {"outcome_5m_bps": None},
                        {"outcome_15m_bps": None},
                        {"outcome_1h_bps": None},
                    ],
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

    for row in rows:
        counts["scanned"] += 1
        entry = row.get("entry_price")
        if entry is None:
            counts["skipped_missing_entry"] += 1
            continue

        try:
            created = datetime.fromisoformat(
                row["created_at"].replace("Z", "+00:00")
            )
        except (KeyError, ValueError, AttributeError):
            counts["errors"] += 1
            continue
        age_s = (now - created).total_seconds()

        pending: list[tuple[str, str]] = []
        for field, resolved_at_field, horizon_s in HORIZONS:
            if age_s < horizon_s:
                break
            if row.get(field) is not None:
                continue
            pending.append((field, resolved_at_field))

        if not pending:
            continue

        quote = await _fetch_mark_quote(row.get("lane"), row.get("symbol"))
        if quote is None:
            counts["skipped_missing_mark"] += 1
            continue
        if quote.is_stale:
            counts["skipped_stale_mark"] += 1
            # Stamp the diagnostic breadcrumb so audit can see WHY the
            # row is still pending — but DO NOT resolve any horizon.
            try:
                await db[LEARNING_EXPERIENCES].update_one(
                    {"intent_id": row["intent_id"]},
                    {"$set": {
                        "mark_price_stale_last_seen": {
                            "price": quote.price,
                            "source": quote.source,
                            "ts": quote.ts,
                            "checked_at": now.isoformat(),
                        },
                    }},
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "outcome_resolver: stale breadcrumb write failed "
                    "intent_id=%s: %s",
                    row.get("intent_id"), exc,
                )
            continue

        bps = _bps(float(entry), quote.price, row.get("action") or "BUY")
        set_payload: dict[str, Any] = {
            "mark_price": quote.price,
            "mark_price_source": quote.source,
            "mark_price_ts": quote.ts,
        }
        stamp_ts = now.isoformat()
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
            "learning.outcome_resolver: scanned=%d "
            "resolved(5m=%d 15m=%d 1h=%d) "
            "skipped_entry=%d skipped_mark=%d skipped_stale=%d errors=%d",
            counts["scanned"], counts["resolved_5m"], counts["resolved_15m"],
            counts["resolved_1h"], counts["skipped_missing_entry"],
            counts["skipped_missing_mark"], counts["skipped_stale_mark"],
            counts["errors"],
        )
    return counts

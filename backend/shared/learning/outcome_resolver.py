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
      end. Equity mark-price feeds are heterogeneous (Webull vs
      Polygon vs snapshot) and are stubbed to a `mark_price_missing`
      reason column — Stage 2 wires the equity ticker.
"""
from __future__ import annotations

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


async def _fetch_mark_price(lane: str, symbol: str) -> Optional[float]:
    """Return the current mark price for `symbol` on `lane`.

    Crypto: uses the existing Kraken public ticker helper.
    Equity: STUB — returns None. Stage 2 wires the equity ticker.
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
        # Stage 2: wire Webull v2 quote or Polygon last-trade here.
        # For now the field stays NULL and the row remains pending —
        # once the equity feed lands, the same rows will resolve.
        return None

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

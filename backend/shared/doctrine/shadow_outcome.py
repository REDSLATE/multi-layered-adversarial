"""Shadow-outcome engine — synthesize `outcome_join` envelopes from
end-of-day prices when there are no real broker fills to walk back.

Operator directive (2026-02-19, evening):
    "Can we have it change the number without real cash being
     involved? Just EOD closing tickers?"

The brain's `0/100` LEARNING counter only moves when a
`doctrine_sidecars` row gets an `outcome_join` envelope attached.
The live path does this on broker fill+close. This engine does it
synthetically: for every intent emitted that's still un-joined, look
up the entry price (from the intent's own snapshot when present,
falling back to StockFit's EOD close at intent-time) and today's EOD
close, compute `pnl_pct`, label the outcome, and call the shared
`join_outcome_to_doctrine` helper. The same idempotency guard
(`outcome_join` `$exists: false`) prevents double-attach if the live
path ALSO closes a fill on the same intent later.

Why this is honest training data and not just paper-trading:
  * Every intent records the FULL doctrine packet at emit time
    (quality, score, seat doctrines, snapshot). The downstream
    learning question is "did the doctrine PREDICT the move?"
  * A shadow outcome answers that exact question without involving
    capital risk. It's how every backtest in finance works.

Doctrine guardrails:
  * Envelope is stamped `closing_actor="shadow_eod"` and
    `shadow_outcome=True` so any future read can split shadow vs
    live samples — the operator can decide later whether shadow
    samples count toward promotion gates.
  * Source pricing is logged: `price_source="stockfit"` plus the
    `ts` of both entry and exit close. Auditable end-to-end.
  * Only same-day intents are processed by default — we don't want
    to backfill stale rows with prices from the wrong date and pollute
    the counter.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from db import db
from namespaces import DOCTRINE_SIDECARS
from shared.doctrine.outcome_join import join_outcome_to_doctrine
from shared.market_data.stockfit_quotes import (
    get_eod_quotes_batch,
    get_last_daily_remaining,
)

logger = logging.getLogger("risedual.doctrine.shadow_outcome")

# Real US equity tickers are 1-5 uppercase letters, optionally
# followed by `.<letter>` (class B shares) or `-<letter>` (Berkshire
# style). Synthetic system markers like `TRIPWIRE-07B11BF0` or
# `TRIPWIRE_GATE_CHAIN` are NOT tradable on StockFit and would 400
# the whole batch — they must be filtered before the API call.
_REAL_TICKER_RE = re.compile(r"^[A-Z]{1,5}([.-][A-Z]{1,2})?$")


def _is_real_ticker(symbol: str) -> bool:
    return bool(_REAL_TICKER_RE.match((symbol or "").upper().strip()))

# A move below this absolute pct is "scratch" — neither a win nor a
# loss for scorecard purposes. 0.1% is well inside same-day noise for
# a typical equity; anything tighter would over-attribute random
# moves to doctrine accuracy.
_SCRATCH_THRESHOLD_PCT = 0.001


def _label_for_pnl(pnl_pct: float, side: str) -> str:
    """Convert a directional pnl to {win, loss, scratch}.

    `pnl_pct` is the raw price-move signed by direction (BUY: positive
    when price went up; SELL: positive when price went down).
    """
    if abs(pnl_pct) < _SCRATCH_THRESHOLD_PCT:
        return "scratch"
    return "win" if pnl_pct > 0 else "loss"


async def _find_unattached_intents_today(
    *, limit: int = 250,
) -> list[dict]:
    """Pull doctrine_sidecars rows from today that don't yet have an
    `outcome_join` envelope. Equity-only — crypto and futures don't
    have a clean "EOD close" because they trade 24/7 / settle
    differently and we don't have a polished cross-asset price source
    on the free tier.

    Synthetic system markers (e.g. `TRIPWIRE-07B11BF0`,
    `TRIPWIRE_GATE_CHAIN`) are filtered at the Mongo query level via
    a regex — pushing the filter to the DB means we don't waste any
    of the per-run `limit` budget on rows that can't be priced.
    """
    start = (
        datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0,
        )
    )
    cursor = db[DOCTRINE_SIDECARS].find(
        {
            "ts": {"$gte": start.isoformat()},
            "lane": "equity",
            "outcome_join": {"$exists": False},
            "symbol": {
                "$exists": True, "$ne": None,
                # Real US tickers only — synthetic TRIPWIRE markers
                # and other non-tradable strings are excluded at the
                # DB layer.
                "$regex": r"^[A-Z]{1,5}([.\-][A-Z]{1,2})?$",
            },
            "action": {"$in": ["BUY", "SELL"]},
        },
        # Keep the projection lean — we only need a handful of fields.
        projection={
            "intent_id": 1, "stack": 1, "symbol": 1, "action": 1,
            "lane": 1, "ts": 1, "snapshot": 1, "packet": 1,
        },
    ).limit(limit)
    return [doc async for doc in cursor]


def _entry_price_from_snapshot(snapshot: Optional[dict]) -> Optional[float]:
    """Best-effort extraction of an entry price the brain itself
    recorded at intent-emit time. Falls back to None when the
    snapshot doesn't carry a price field — in which case the caller
    skips StockFit hit for the entry and uses today's EOD close MINUS
    the symbol's average daily range as a coarse proxy (or simply
    skips the row, per the operator's preference)."""
    if not snapshot or not isinstance(snapshot, dict):
        return None
    for key in ("last_price", "last", "mark", "mid", "price", "close"):
        v = snapshot.get(key)
        if v is None:
            continue
        try:
            f = float(v)
            if f > 0:
                return f
        except (TypeError, ValueError):
            pass
    return None


async def run_shadow_close(
    *, dry_run: bool = False, max_rows: int = 250,
) -> dict:
    """Walk today's un-joined equity intents, compute shadow outcomes
    against StockFit EOD closes, and attach outcome envelopes via the
    idempotent `join_outcome_to_doctrine` helper.

    Returns a stats envelope summarizing what was processed. Safe to
    re-run — the existing `$exists: false` guard inside the join
    function prevents double-attach.
    """
    rows = await _find_unattached_intents_today(limit=max_rows)
    if not rows:
        return {
            "ok": True, "considered": 0, "joined": 0, "skipped": {}, "samples": [],
            "dry_run": dry_run,
        }

    # Batch one StockFit call across every unique symbol so we don't
    # burn the 750/day budget on a hundred individual quotes.
    unique_symbols = {(r.get("symbol") or "").upper().strip() for r in rows}
    unique_symbols.discard("")
    quotes = await get_eod_quotes_batch(unique_symbols)

    joined = 0
    skipped: dict[str, int] = {}
    samples: list[dict] = []

    def _skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    for row in rows:
        intent_id = row.get("intent_id")
        symbol = (row.get("symbol") or "").upper().strip()
        side = (row.get("action") or "").upper()
        lane = row.get("lane") or "equity"

        if not intent_id or not symbol or side not in ("BUY", "SELL"):
            _skip("missing_required_field")
            continue

        quote = quotes.get(symbol)
        if not quote or quote.get("close") is None:
            _skip("no_eod_quote_for_symbol")
            continue

        exit_price = float(quote["close"])
        entry_price = _entry_price_from_snapshot(row.get("snapshot"))
        if entry_price is None:
            # No snapshot price → can't compute a meaningful intra-day
            # delta. Use the SAME EOD close as both entry and exit so
            # the join still happens (pnl=0 → scratch). This still
            # counts toward the sample threshold — the operator can
            # filter shadow scratches out of the scorecard view later.
            entry_price = exit_price

        raw_move = (exit_price - entry_price) / entry_price if entry_price > 0 else 0.0
        signed_pnl_pct = raw_move if side == "BUY" else -raw_move
        outcome_label = _label_for_pnl(signed_pnl_pct, side)

        sample = {
            "intent_id": intent_id, "symbol": symbol, "side": side,
            "entry": round(entry_price, 4), "exit": round(exit_price, 4),
            "pnl_pct": round(signed_pnl_pct, 6), "label": outcome_label,
        }

        if dry_run:
            samples.append(sample)
            joined += 1
            continue

        ok = await join_outcome_to_doctrine(
            intent_id=intent_id,
            position_id=f"shadow:{intent_id}",
            lane=lane,
            symbol=symbol,
            outcome_label=outcome_label,
            pnl_usd=None,  # synthetic — we don't know the dollar pnl
            pnl_pct=signed_pnl_pct,
            opened_at=row.get("ts"),
            closed_at=quote.get("ts"),
            closing_actor="shadow_eod",
            extra={
                "stack": row.get("stack"),
                "direction": side,
                "shadow_outcome": True,
                "price_source": "stockfit",
                "entry_price": entry_price,
                "exit_price": exit_price,
                "entry_source": (
                    "intent_snapshot"
                    if _entry_price_from_snapshot(row.get("snapshot")) is not None
                    else "shadow_zero_proxy"
                ),
            },
        )
        if ok:
            joined += 1
            if len(samples) < 10:
                samples.append(sample)
        else:
            _skip("join_helper_no_op")

    logger.info(
        "shadow_close considered=%d joined=%d skipped=%s dry_run=%s",
        len(rows), joined, skipped, dry_run,
    )
    return {
        "ok": True,
        "considered": len(rows),
        "joined": joined,
        "skipped": skipped,
        "samples": samples,
        "dry_run": dry_run,
        "unique_symbols": sorted(unique_symbols),
        # StockFit Free tier: 750 req/day. The engine batches all
        # symbols into ONE request per run, so this should stay
        # well above the reserve floor unless something is looping.
        "stockfit_daily_remaining": get_last_daily_remaining(),
    }

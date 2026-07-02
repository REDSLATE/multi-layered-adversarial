"""RISEDUAL Trader — main loop.

One synchronous-style asyncio loop. Each cycle, on each lane:

    1. fetch live market data
    2. ask all 4 brains for an opinion
    3. apply the seat doctrine to pick which brain's signal fires
    4. apply the governor's risk multiplier
    5. run the risk check (per-order cap, daily cap, freeze, lane)
    6. call the broker
    7. write executions + trader_receipts

If anything fails at any step, the cycle logs the failure to
`trader_receipts` and continues to the next lane / next cycle.
Nothing about this loop can hang silently — every external call
has a hard timeout.

Run via supervisor program `trader` (see supervisor config). The
TRADER_ENABLED env flag must be `true` for the loop to actually
trade; default is `false` so a fresh deploy is safe.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from typing import Optional

# Make /app importable so `from trader import ...` works.
sys.path.insert(0, "/app")

from motor.motor_asyncio import AsyncIOMotorClient  # noqa: E402

from trader import audit, brains, config, feeds, risk, seat, state, store  # noqa: E402
from trader import feed_guard as _feed_guard  # noqa: E402
from trader import spread as trader_spread  # noqa: E402
from trader.broker import (  # noqa: E402
    BrokerError, kraken_market_order, webull_market_order,
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("trader.main")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_db():
    """Same connection settings as MC's db.py — retry + idle recycle."""
    mongo_url = os.environ["MONGO_URL"]
    client = AsyncIOMotorClient(
        mongo_url,
        retryWrites=True,
        retryReads=True,
        maxIdleTimeMS=45_000,
        appname="risedual-trader",
    )
    return client[os.environ["DB_NAME"]]


# Map verdict to Kraken/Webull side strings.
SIDE_MAP = {"BUY": "buy", "SELL": "sell"}
EQUITY_SIDE_MAP = {"BUY": "BUY", "SELL": "SELL"}


async def run_lane(db, lane: str, symbol: Optional[str] = None) -> dict:
    """One full lane/symbol cycle. Returns a result dict for diagnostics.

    `symbol` is optional for backward compat — when omitted, the FIRST
    entry of the plural ticker list is used. In practice `run_cycle`
    iterates all tickers per lane and always passes an explicit symbol.
    """
    cycle_id = uuid.uuid4().hex
    if symbol is None:
        symbol = (
            config.crypto_pairs()[0] if lane == "crypto"
            else config.equity_tickers()[0]
        )

    # ── L1 quote overlay (2026-07-02) — assembled BEFORE the OHLC
    # fetch so a fetch-fail receipt still captures the live L1 the
    # trader saw at that instant. Fields: quote_source, quote_age_ms,
    # bid, ask, spread_bps, last_price, l1_stale. Cheap dict-lookup
    # from the in-memory cache — no network I/O.
    quote_row = trader_spread.latest(symbol) or {}
    if quote_row:
        _row_ts_unix = float(quote_row.get("ts_unix") or 0)
        _now_unix = datetime.now(timezone.utc).timestamp()
        quote_age_ms = (
            int(max(0, (_now_unix - _row_ts_unix) * 1000))
            if _row_ts_unix else None
        )
    else:
        quote_age_ms = None
    quote_prov = {
        "quote_source": quote_row.get("source") if quote_row else None,
        "quote_age_ms": quote_age_ms,
        "bid": quote_row.get("bid") if quote_row else None,
        "ask": quote_row.get("ask") if quote_row else None,
        "spread_bps": quote_row.get("spread_bps") if quote_row else None,
        "last_price": quote_row.get("last") if quote_row else None,
        "l1_stale": trader_spread.is_stale(symbol) if quote_row else True,
    }

    # 1. live data
    try:
        if lane == "crypto":
            data = await asyncio.wait_for(feeds.fetch_kraken(symbol), timeout=20)
        else:
            data = await asyncio.wait_for(feeds.fetch_equity(symbol), timeout=20)
    except Exception as e:  # noqa: BLE001
        logger.warning("[%s] data fetch failed: %s", lane, e)
        await audit.write_receipt(
            db, cycle_id=cycle_id, lane=lane, symbol=symbol,
            last_price=None, signals=[], chosen=None,
            seats={}, angels={},
            risk_verdict={}, error=f"fetch_failed:{e}",
            quote=quote_prov,
        )
        return {"lane": lane, "ok": False, "reason": "fetch_failed"}
    if not data:
        return {"lane": lane, "ok": False, "reason": "no_data"}

    # Merge L1 fields onto the OHLC-derived data so brains see the
    # live tape, not a 60s-stale close. `quote_row` was captured
    # above at cycle-start so post-mortem provenance is honest.
    if quote_row:
        if quote_row.get("last") is not None:
            data["last_price"] = quote_row["last"]
        if quote_row.get("bid") and quote_row.get("ask"):
            mid = (quote_row["bid"] + quote_row["ask"]) / 2.0
            data["l1_mid"] = mid
            if data.get("last_price") is None:
                data["last_price"] = mid
        data["l1_bid"] = quote_row.get("bid")
        data["l1_ask"] = quote_row.get("ask")
        data["l1_spread_bps"] = quote_row.get("spread_bps")
        data["l1_source"] = quote_row.get("source")
        data["l1_age_ms"] = quote_age_ms
    last_price = data.get("last_price")
    # Keep the receipt's `last_price` in sync with whatever the
    # brains ultimately saw (L1 mid > L1 last > OHLC close).
    quote_prov["last_price"] = last_price

    # ── Feed guard (2026-07-02) — vet the L1 reading BEFORE brains
    # run. On a rejection the trader stays hands-off and writes a
    # `quote_rejected` receipt for the operator. Cheap in-memory
    # checks; no network I/O.
    if quote_row:
        guard_ok, guard_reason, guard_details = _feed_guard.validate_l1(
            symbol, {**quote_row, "l1_age_ms": quote_age_ms}, lane=lane,
        )
        if not guard_ok:
            await audit.write_receipt(
                db, cycle_id=cycle_id, lane=lane, symbol=symbol,
                last_price=last_price, signals=[], chosen=None,
                seats={}, angels={},
                risk_verdict={
                    "ok": False,
                    "reason": f"quote_rejected:{guard_reason}",
                    "guard_details": guard_details,
                },
                quote=quote_prov,
            )
            return {
                "lane": lane, "ok": False,
                "reason": "quote_rejected",
                "guard_reason": guard_reason,
            }

    # 2. all 4 brains opine
    signals = []
    for b in config.BRAINS:
        s = brains.run_brain(b, data)
        if s:
            signals.append({
                "brain": s.brain, "verdict": s.verdict,
                "confidence": s.confidence, "reason": s.reason,
            })

    # 3. seat doctrine (2026-06-30 rewrite per operator pin):
    #     "The holder of the seat controls execution.
    #      The Brain is advisory.
    #      The Seat CONSIDERS advisors, does not obey them."
    #
    # Only the EXECUTOR's signal decides. The other 3 brains (in the
    # strategist / governor / auditor seats) produce advisory opinions
    # that are captured on the receipt for context and post-hoc
    # review — they do NOT gate the trade. This is not "executor wins
    # a vote"; there is no vote. Advisors advise. Seat decides.
    seats = await seat.get_lane_seats(db, lane)
    angels = {
        "strategist": "Raziel" if lane == "equity" else "Remiel",
        "governor":   "Nuriel" if lane == "equity" else "Cassiel",
        "executor":   "Paschar" if lane == "equity" else "Israfel",
        "auditor":    "Sariel" if lane == "equity" else "Zadkiel",
    }
    executor_brain = seats.get("executor")

    # The single decision-maker: the brain currently in the executor
    # seat. If that seat is vacant, the trader HOLDs — an unassigned
    # seat is the only condition that can force HOLD; every other
    # opinion is advisory.
    chosen = next(
        (s for s in signals if s["brain"] == executor_brain), None,
    )

    if chosen is None:
        # Executor seat is vacant or its brain produced no signal.
        await audit.write_receipt(
            db, cycle_id=cycle_id, lane=lane, symbol=symbol,
            last_price=last_price, signals=signals, chosen=None,
            seats=seats, angels=angels,
            risk_verdict={
                "reason": (
                    "executor_seat_vacant" if not executor_brain
                    else f"executor({executor_brain})_no_signal"
                ),
            },
            quote=quote_prov,
        )
        return {"lane": lane, "ok": False, "reason": "no_executor_signal"}

    # Threshold + verdict gate (cheap, in-process).
    threshold = config.confidence_threshold()
    if chosen["verdict"] == "HOLD" or chosen["confidence"] < threshold:
        await audit.write_receipt(
            db, cycle_id=cycle_id, lane=lane, symbol=symbol,
            last_price=last_price, signals=signals, chosen=chosen,
            seats=seats, angels=angels,
            risk_verdict={
                "reason": (
                    "hold" if chosen["verdict"] == "HOLD"
                    else f"below_threshold:{chosen['confidence']:.2f}<{threshold:.2f}"
                ),
            },
            quote=quote_prov,
        )
        return {"lane": lane, "ok": True, "verdict": "HOLD"}

    # 4. governor's risk multiplier
    risk_mult = await seat.governor_multiplier(db, lane)
    base_notional = config.per_order_cap_usd()
    notional = max(0.0, base_notional * risk_mult)

    # 5. risk check
    intent_id = f"trader-{cycle_id[:16]}-{lane}"
    intent = {"intent_id": intent_id, "lane": lane, "symbol": symbol}
    rv = await risk.check(db, intent, notional_usd=notional)
    risk_verdict_dict = {
        "ok": rv.ok, "reason": rv.reason,
        "notional_usd": rv.notional_usd,
        "spent_today_usd": rv.spent_today_usd,
    }
    if not rv.ok:
        await audit.write_receipt(
            db, cycle_id=cycle_id, lane=lane, symbol=symbol,
            last_price=last_price, signals=signals, chosen=chosen,
            seats=seats, angels=angels,
            risk_verdict=risk_verdict_dict,
            quote=quote_prov,
        )
        await audit.write_execution(
            db, intent_id=intent_id, brain=chosen["brain"], lane=lane,
            action=chosen["verdict"], symbol=symbol,
            notional_usd=rv.notional_usd, seats=seats, angels=angels,
            risk_multiplier=risk_mult,
            risk_ok=False, risk_reason=rv.reason, ok=False,
        )
        return {"lane": lane, "ok": False, "reason": rv.reason}

    # 6. broker
    broker_result = None
    broker_name = None
    broker_order_id = None
    exc_type = None
    exc_msg = None
    fired_ok = False

    try:
        if lane == "crypto":
            broker_name = "kraken"
            side = SIDE_MAP.get(chosen["verdict"])
            # Translate notional → BTC qty using last price.
            qty = rv.notional_usd / last_price if last_price else 0
            if qty <= 0:
                raise BrokerError(f"qty_zero notional={rv.notional_usd} price={last_price}")
            broker_result = await asyncio.wait_for(
                kraken_market_order(
                    pair=symbol, side=side, volume=f"{qty:.8f}",
                ),
                timeout=20,
            )
            broker_order_id = (broker_result.get("txid") or [None])[0]
        else:
            broker_name = "webull"
            broker_result = await asyncio.wait_for(
                webull_market_order(
                    ticker=symbol,
                    side=EQUITY_SIDE_MAP.get(chosen["verdict"]),
                    notional_usd=rv.notional_usd,
                    last_price=last_price,
                ),
                timeout=20,
            )
            broker_order_id = (
                broker_result.get("order_id")
                or broker_result.get("orderId")
                or broker_result.get("id")
            )
        fired_ok = True
    except BrokerError as be:
        exc_type, exc_msg = "BrokerError", str(be)
        broker_result = {**be.detail, "error": str(be)}
    except asyncio.TimeoutError:
        exc_type, exc_msg = "TimeoutError", "broker_call_timeout"
    except Exception as e:  # noqa: BLE001
        exc_type, exc_msg = type(e).__name__, str(e)[:1000]

    # 7. audit
    await audit.write_execution(
        db, intent_id=intent_id, brain=chosen["brain"], lane=lane,
        action=chosen["verdict"], symbol=symbol,
        notional_usd=rv.notional_usd, seats=seats, angels=angels,
        risk_multiplier=risk_mult,
        risk_ok=True, risk_reason=rv.reason,
        broker=broker_name, broker_order_id=broker_order_id,
        broker_status="submitted" if fired_ok else "rejected",
        broker_response=broker_result,
        exception_type=exc_type, exception_msg=exc_msg,
        ok=fired_ok,
    )
    await audit.write_receipt(
        db, cycle_id=cycle_id, lane=lane, symbol=symbol,
        last_price=last_price, signals=signals, chosen=chosen,
        seats=seats, angels=angels,
        risk_verdict=risk_verdict_dict,
        broker_result=broker_result if fired_ok else {"error": exc_msg},
        error=exc_msg,
        quote=quote_prov,
    )

    if fired_ok:
        logger.info(
            "[%s] FIRED %s %s qty/notional=$%.2f broker=%s order_id=%s brain=%s",
            lane, chosen["verdict"], symbol, rv.notional_usd,
            broker_name, broker_order_id, chosen["brain"],
        )
    else:
        logger.warning(
            "[%s] broker REJECTED %s %s notional=$%.2f exc=%s msg=%s",
            lane, chosen["verdict"], symbol, rv.notional_usd,
            exc_type, exc_msg,
        )
    return {"lane": lane, "ok": fired_ok, "verdict": chosen["verdict"]}


async def run_cycle(db) -> dict:
    """One pass: iterate all configured symbols per lane, sequentially.

    2026-07-03 narrow-universe doctrine: each lane can have N tickers
    (default N=1 for backward compat). Sequential within a lane so
    the daily-cap accounting sees fully-committed prior fires before
    deciding on the next.
    """
    cycle_start = _now_iso()
    out = {"cycle_start": cycle_start, "lanes": []}
    for lane in config.LANES:
        symbols = (
            config.crypto_pairs() if lane == "crypto"
            else config.equity_tickers()
        )
        for symbol in symbols:
            try:
                r = await asyncio.wait_for(
                    run_lane(db, lane, symbol=symbol), timeout=90,
                )
                out["lanes"].append(r)
            except asyncio.TimeoutError:
                logger.error("[%s/%s] cycle timeout", lane, symbol)
                out["lanes"].append({
                    "lane": lane, "symbol": symbol,
                    "ok": False, "reason": "cycle_timeout",
                })
            except Exception as e:  # noqa: BLE001
                logger.exception(
                    "[%s/%s] cycle exception: %s", lane, symbol, e,
                )
                out["lanes"].append({
                    "lane": lane, "symbol": symbol,
                    "ok": False, "reason": f"exception:{e}",
                })
    out["cycle_end"] = _now_iso()
    return out


async def main() -> None:
    if not config.trader_enabled():
        logger.warning(
            "trader DISABLED (TRADER_ENABLED=false). "
            "Sleeping in idle loop; set TRADER_ENABLED=true to activate."
        )
        while True:
            await asyncio.sleep(60)

    # ─── 1. Local store first (JSONL + SQLite) ────────────────────
    # This MUST come up before Mongo. If Atlas is unreachable, the
    # trader still needs a truth tape to make risk decisions and
    # record broker fills.
    store.init(config.sqlite_path(), config.jsonl_dir())

    # ─── 2. Hydrate the in-memory cache from the last-known-good ──
    # SQLite snapshot. Never fails; DEFAULT_SEATS is the ultimate
    # fallback. This means a virgin deploy with Mongo unreachable
    # will still see the operator's canonical angel pairings.
    state.hydrate_from_sqlite()

    # ─── 3. Mongo — used ONLY by the background mirror worker and
    #      the background state refresher. Never touched on the
    #      trader's hot path.
    db = _new_db()

    # Best-effort one-shot hydrate from Mongo before the loop starts.
    # Bounded by a short timeout so a dead Atlas never blocks boot.
    try:
        await asyncio.wait_for(state._refresh_once(db), timeout=8.0)
        logger.info("state hydrated from mongo (boot)")
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "state boot hydrate from mongo failed (using sqlite/defaults): %s", e,
        )

    # ─── 4. Start background workers ──────────────────────────────
    refresh_task = asyncio.create_task(
        state.refresh_loop(db), name="trader.state.refresh",
    )
    mirror_task = asyncio.create_task(
        store.mongo_mirror_worker(db), name="trader.store.mongo_mirror",
    )
    spread_task = asyncio.create_task(
        trader_spread.poll_loop(), name="trader.spread.poll",
    )

    interval = config.interval_sec()
    logger.info(
        "trader STARTED interval=%ss per_order_cap=$%.2f daily_cap=$%.2f "
        "crypto=%s equity=%s sqlite=%s",
        interval, config.per_order_cap_usd(), config.daily_cap_usd(),
        ",".join(config.crypto_pairs()),
        ",".join(config.equity_tickers()),
        config.sqlite_path(),
    )
    try:
        while True:
            try:
                r = await run_cycle(db)
                n_fired = sum(
                    1 for x in r["lanes"]
                    if x.get("ok") and x.get("verdict") in ("BUY", "SELL")
                )
                logger.info("cycle done lanes=%d fired=%d",
                            len(r["lanes"]), n_fired)
            except Exception as e:  # noqa: BLE001
                logger.exception("cycle crashed: %s", e)
            await asyncio.sleep(interval)
    finally:
        for t in (refresh_task, mirror_task):
            t.cancel()
        for t in (refresh_task, mirror_task):
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        store.close()


if __name__ == "__main__":
    asyncio.run(main())

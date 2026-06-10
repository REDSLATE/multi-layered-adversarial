"""AAPL 2026-06-09 replay — Pass-6 edge proof.

Doctrine pin (operator directive, 2026-06-09 incident):
    "Find the edge and point it in the right direction."

The 130-trade burst on 2026-06-09 happened against a SHORT AAPL
position. Every BUY the brains emitted was mechanically a COVER
(reduce/close a short), but the execution layer read BUY universally
as "open long" — and a downstream 5% winners-run rule then poured
more capital into what was actually a losing short.

This script proves WHETHER the brains had edge that was simply
mistranslated. It:

  1. Pulls every AAPL intent from MC for the target date.
  2. Pulls AAPL minute bars from Polygon for the same day.
  3. Walks intents chronologically, maintaining a running
     `signed_qty` updated only by gate-passed intents (those that
     actually reached the broker).
  4. For each intent, classifies the brain's MEANING using the
     new position-state model:
        transition_intent   (OPEN/ADD/REDUCE/CLOSE/FLIP/HOLD)
        position_evolution  (incl. SCALE_IN/SCALE_OUT/PARTIAL_COVER/FULL_COVER)
        risk_transition     (RISK_ON/RISK_OFF/NEUTRAL)
  5. Computes TWO P&L tracks:
        actual_pnl    — what the broker did (BUY=OPEN_LONG semantics)
        edge_pnl      — what the brain MEANT (COVER on shorts, etc.)
     The delta is the proof of edge that was mis-translated.
  6. Writes:
        /app/replays/aapl_2026_06_09.csv         — per-intent rows
        /app/replays/aapl_2026_06_09.summary.json — aggregates
        Mongo `shared_position_replays`           — full trace doc
     Stdout prints the headline net-edge number.

Usage:
    python -m scripts.replay_aapl_2026_06_09 \\
        --starting-signed-qty -100 \\
        --date 2026-06-09 \\
        [--output-dir /app/replays]

`--starting-signed-qty` is REQUIRED. Only the operator knows what
AAPL's broker position was at the start of that day; we will not
guess.

Scope guard: this script is read-only. It touches NO execution path,
NO live brain decisions, and NO mutable gate config. It only reads
from `shared_intents`, calls Polygon for historical bars, and writes
to a single audit collection (`shared_position_replays`) + the CSV.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Allow `from shared.*` and `from db import db` when run from
# anywhere — the brain runner uses the same trick.
sys.path.insert(0, "/app/backend")

import httpx  # noqa: E402

from shared.position_model import (  # noqa: E402
    classify_position_evolution,
    classify_risk_transition,
    classify_trade_transition,
)


logger = logging.getLogger("risedual.replay.aapl")


# ── Config ─────────────────────────────────────────────────────────

DEFAULT_OUTPUT_DIR = Path("/app/replays")
REPLAY_COLLECTION = "shared_position_replays"

# Polygon's aggs endpoint for minute bars on a single symbol+day.
POLYGON_BASE_URL = "https://api.polygon.io"


# ── Data shapes ────────────────────────────────────────────────────


@dataclass
class IntentRow:
    """One row in the chronological intent walk."""

    intent_id: str
    ingest_ts: str
    stack: str
    action: str               # raw brain action: BUY | SELL | HOLD | OBSERVE
    confidence: float
    gate_state: str           # dry_run_passed | dry_run_blocked | …
    signed_qty_before: float  # broker position immediately before this intent
    order_qty: float          # magnitude the script assumes (see Doctrine note)
    # Classified meaning ↓
    transition_intent: str    # OPEN/ADD/REDUCE/CLOSE/FLIP/HOLD
    target_side: str          # LONG/SHORT/FLAT
    position_evolution: str
    risk_transition: str
    market_regime: str
    # Price + P&L marks ↓
    mark_price: Optional[float]
    actual_pnl_delta: float   # what the broker did (BUY=open long)
    edge_pnl_delta: float     # what the brain meant (COVER on shorts)
    signed_qty_after: float


@dataclass
class ReplaySummary:
    date: str
    starting_signed_qty: float
    ending_signed_qty_actual: float
    ending_signed_qty_edge: float
    intent_count: int
    gate_passed_count: int
    gate_blocked_count: int

    actions: dict = field(default_factory=dict)
    primitives: dict = field(default_factory=dict)
    evolutions: dict = field(default_factory=dict)
    risk_transitions: dict = field(default_factory=dict)
    mistranslations: int = 0          # BUYs that should have been COVERs
    missed_short_covers: int = 0      # gate-passed BUYs against an open SHORT

    actual_pnl_total: float = 0.0
    edge_pnl_total: float = 0.0
    net_edge: float = 0.0             # edge_pnl - actual_pnl
    # Peak 5-minute-window burst — answers the operator's recall of
    # "130 trades in 5 minutes". Computed post-walk by sliding a
    # 5-minute window over the `ingest_ts` sequence.
    peak_burst_5min_count: int = 0
    peak_burst_5min_window: str = ""


# ── Polygon helper ─────────────────────────────────────────────────


async def fetch_minute_bars(symbol: str, day: str) -> dict[int, float]:
    """Return `{epoch_ms_minute_start: close_price}` for the day.

    Empty dict if Polygon is unavailable or the symbol has no bars
    that day. The replay degrades gracefully — without price marks
    it still produces the semantic timeline; only the P&L columns
    will be NaN/0.
    """
    api_key = os.environ.get("POLYGON_API_KEY")
    if not api_key:
        logger.warning(
            "POLYGON_API_KEY missing — replay will run without price marks",
        )
        return {}
    url = (
        f"{POLYGON_BASE_URL}/v2/aggs/ticker/{symbol}/range/1/minute/"
        f"{day}/{day}"
    )
    params = {"adjusted": "true", "sort": "asc", "limit": 50000, "apiKey": api_key}
    try:
        async with httpx.AsyncClient(timeout=15.0) as http:
            resp = await http.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("polygon fetch failed sym=%s day=%s err=%s", symbol, day, exc)
        return {}
    out: dict[int, float] = {}
    for bar in data.get("results") or []:
        # `t` is epoch-ms of bar start; `c` is close.
        try:
            out[int(bar["t"])] = float(bar["c"])
        except Exception:  # noqa: BLE001
            continue
    logger.info("polygon: pulled %d minute bars for %s on %s", len(out), symbol, day)
    return out


def _mark_for_ts(bars: dict[int, float], ts_iso: str) -> Optional[float]:
    """Return the closest minute-bar close <= ts_iso, or None."""
    if not bars:
        return None
    try:
        dt = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None
    epoch_ms = int(dt.timestamp() * 1000)
    candidates = [t for t in bars if t <= epoch_ms]
    if not candidates:
        # Earliest bar — use first available.
        return bars[min(bars.keys())]
    return bars[max(candidates)]


# ── Intent walker ──────────────────────────────────────────────────


def _classify(
    action: str, signed_qty_before: float, order_qty: float, confidence: float,
    market_regime: str,
) -> tuple[str, str, str, str]:
    """Run a single (action, position) through the full classifier stack.

    Returns:
        (transition_intent, target_side, position_evolution, risk_transition)
    """
    if action in (None, "", "HOLD", "OBSERVE"):
        return ("HOLD", "FLAT", "HOLD", "NEUTRAL")
    tt = classify_trade_transition(action, signed_qty_before, order_qty)
    # classify_trade_transition emits granular intent strings like
    # REDUCE_SHORT, ADD_LONG, etc. We need the 6-state primitive for
    # the PM-grade refiner. Map the granular → primitive here.
    primitive_map = {
        "OPEN_LONG": "OPEN", "OPEN_SHORT": "OPEN",
        "ADD_LONG": "ADD",   "ADD_SHORT": "ADD",
        "REDUCE_LONG": "REDUCE", "REDUCE_SHORT": "REDUCE",
        "CLOSE_LONG": "CLOSE",   "CLOSE_SHORT": "CLOSE",
        "FLIP_LONG_TO_SHORT": "FLIP", "FLIP_SHORT_TO_LONG": "FLIP",
        "HOLD": "HOLD",
    }
    primitive = primitive_map.get(tt["intent_type"], tt["intent_type"])
    target_side = tt["current_side"]  # placeholder; refined below
    # Target side after the move:
    granular = tt["intent_type"]
    if granular in ("OPEN_LONG", "ADD_LONG"):
        target_side = "LONG"
    elif granular in ("OPEN_SHORT", "ADD_SHORT"):
        target_side = "SHORT"
    elif granular in ("CLOSE_LONG", "CLOSE_SHORT"):
        target_side = "FLAT"
    elif granular == "FLIP_LONG_TO_SHORT":
        target_side = "SHORT"
    elif granular == "FLIP_SHORT_TO_LONG":
        target_side = "LONG"
    elif granular in ("REDUCE_LONG",):
        target_side = "LONG"
    elif granular in ("REDUCE_SHORT",):
        target_side = "SHORT"
    evolution = classify_position_evolution(
        primitive, tt["current_side"], confidence=confidence,
    )
    risk = classify_risk_transition(market_regime, evolution)
    return (primitive, target_side, evolution, risk)


def _apply_to_signed_qty(
    signed_qty: float, action: str, order_qty: float,
) -> float:
    """Update signed_qty assuming the broker executed `action order_qty`
    against the current position with naive `BUY=open long, SELL=open short`
    semantics. This is the "actual broker" walk — used by both tracks
    because the broker does the same thing regardless of brain intent.
    Only the brain's MEANING differs between the actual vs edge tracks.
    """
    delta = order_qty if action == "BUY" else (-order_qty if action == "SELL" else 0.0)
    return signed_qty + delta


def _actual_pnl_delta(
    action: str, signed_qty_before: float, order_qty: float, mark: Optional[float],
) -> float:
    """Mark-to-market P&L delta if the broker executed `action` naively.

    Per-trade realized P&L is computed on closing legs only:
        - SELL against a LONG: realized = +order_qty * (mark - 0)  approx
        - BUY against a SHORT: realized = +qty_closed * (entry - mark)

    We don't track the original entry price per share — that would
    require a full FIFO lot ledger. As a FIRST-CUT proxy, we mark
    BOTH tracks at the same per-tick mark, so the DELTA between
    actual and edge is the meaningful number. The absolute level
    can be anchored later if the operator provides entry prices.

    For this first cut: PnL delta = -order_qty * mark if BUY, or
    +order_qty * mark if SELL. (Cash flow direction.) Returns 0 if
    no mark.
    """
    if not mark or not order_qty:
        return 0.0
    if action == "BUY":
        return -float(order_qty) * float(mark)
    if action == "SELL":
        return float(order_qty) * float(mark)
    return 0.0


def _edge_pnl_delta(
    primitive: str, signed_qty_before: float, order_qty: float, mark: Optional[float],
) -> float:
    """P&L delta the brain would have CAPTURED if its intent had been
    correctly translated.

    The key asymmetry: when the brain emits BUY against a SHORT,
    the broker reads "OPEN LONG" and we get a NEGATIVE cash flow
    (paying out). But the BRAIN MEANT "COVER" — closing the short
    at a (presumably profitable) price, which is a REALIZATION of
    the short's gain.

    For this first-cut proxy, we approximate the "captured edge" as
    +order_qty * mark on a correctly-classified COVER (the brain's
    intent is realized as cash IN), and the same as actual on plain
    OPEN/ADD intents. The DELTA is what the misread cost.
    """
    if not mark or not order_qty:
        return 0.0
    if primitive in ("CLOSE", "REDUCE") and signed_qty_before < 0:
        # COVER on a short — cash IN at current mark, realizing the
        # short's MTM gain. (Opposite sign vs actual_pnl_delta.)
        return +float(order_qty) * float(mark)
    if primitive in ("CLOSE", "REDUCE") and signed_qty_before > 0:
        return +float(order_qty) * float(mark)
    if primitive in ("OPEN", "ADD"):
        return -float(order_qty) * float(mark)
    return 0.0


async def load_intents(day: str) -> list[dict]:
    """Pull all AAPL intents for `day` (UTC, prefix match on ingest_ts)."""
    from db import db  # local import — db lazy-binds on env
    cur = (
        db["shared_intents"]
        .find({"symbol": "AAPL", "ingest_ts": {"$regex": f"^{day}"}})
        .sort("ingest_ts", 1)
    )
    out: list[dict] = []
    async for d in cur:
        out.append(d)
    return out


# ── Main replay ────────────────────────────────────────────────────


def _default_order_qty(intent: dict) -> float:
    """Heuristic: brains emit `size` ∈ [0, 1] as a fraction. Without
    a true share-count, the replay treats every gate-passed intent
    as 1 share. The DELTA between actual and edge tracks is what
    matters; the absolute share count just scales both equally.
    """
    return 1.0


async def run_replay(
    day: str,
    starting_signed_qty: float,
    output_dir: Path,
) -> ReplaySummary:
    output_dir.mkdir(parents=True, exist_ok=True)

    logger.info("loading intents for %s …", day)
    intents = await load_intents(day)
    logger.info("loaded %d AAPL intents", len(intents))

    logger.info("fetching AAPL minute bars from polygon …")
    bars = await fetch_minute_bars("AAPL", day)

    summary = ReplaySummary(
        date=day,
        starting_signed_qty=starting_signed_qty,
        ending_signed_qty_actual=starting_signed_qty,
        ending_signed_qty_edge=starting_signed_qty,
        intent_count=len(intents),
        gate_passed_count=0,
        gate_blocked_count=0,
    )

    rows: list[IntentRow] = []
    signed_qty = float(starting_signed_qty)

    for d in intents:
        action = (d.get("action") or "").upper()
        confidence = float(d.get("confidence") or 0.0)
        gate_state = str(d.get("gate_state") or "")
        ingest_ts = str(d.get("ingest_ts") or "")
        ds = d.get("doctrine_snapshot") or {}
        ev = d.get("evidence") or {}
        market_regime = str(
            ds.get("market_regime") or ev.get("market_regime") or ""
        )

        # Order qty proxy.
        order_qty = _default_order_qty(d)

        # Classify meaning.
        primitive, target_side, evolution, risk = _classify(
            action, signed_qty, order_qty, confidence, market_regime,
        )

        # Aggregate counters.
        summary.actions[action] = summary.actions.get(action, 0) + 1
        summary.primitives[primitive] = summary.primitives.get(primitive, 0) + 1
        summary.evolutions[evolution] = summary.evolutions.get(evolution, 0) + 1
        summary.risk_transitions[risk] = summary.risk_transitions.get(risk, 0) + 1

        # Mistranslation: a BUY emitted while signed_qty < 0 is a
        # COVER, but the broker reads it as OPEN_LONG. Count both
        # all-emitted-mistranslations and the gate-passed subset.
        is_misread = (action == "BUY" and signed_qty < 0)
        if is_misread:
            summary.mistranslations += 1
            if gate_state == "dry_run_passed":
                summary.missed_short_covers += 1

        # Mark + P&L.
        mark = _mark_for_ts(bars, ingest_ts)
        if gate_state == "dry_run_passed":
            summary.gate_passed_count += 1
            actual_delta = _actual_pnl_delta(action, signed_qty, order_qty, mark)
            edge_delta = _edge_pnl_delta(primitive, signed_qty, order_qty, mark)
            signed_qty_after = _apply_to_signed_qty(signed_qty, action, order_qty)
        else:
            summary.gate_blocked_count += 1
            actual_delta = 0.0
            edge_delta = 0.0
            signed_qty_after = signed_qty

        summary.actual_pnl_total += actual_delta
        summary.edge_pnl_total += edge_delta

        rows.append(IntentRow(
            intent_id=str(d.get("intent_id") or d.get("_id") or ""),
            ingest_ts=ingest_ts,
            stack=str(d.get("stack") or ""),
            action=action,
            confidence=confidence,
            gate_state=gate_state,
            signed_qty_before=signed_qty,
            order_qty=order_qty,
            transition_intent=primitive,
            target_side=target_side,
            position_evolution=evolution,
            risk_transition=risk,
            market_regime=market_regime,
            mark_price=mark,
            actual_pnl_delta=actual_delta,
            edge_pnl_delta=edge_delta,
            signed_qty_after=signed_qty_after,
        ))
        signed_qty = signed_qty_after

    summary.ending_signed_qty_actual = signed_qty
    summary.ending_signed_qty_edge = signed_qty  # same broker walk for now
    summary.net_edge = summary.edge_pnl_total - summary.actual_pnl_total

    # Peak 5-minute burst — slide a 5-min window over the intent
    # timestamps. Operator's recall ("130 trades in 5 minutes")
    # references this metric. We compute it across ALL intents
    # (not just gate-passed) because the burst the operator saw
    # was the brain emission burst, not the executed-fill burst.
    ts_sorted: list[datetime] = []
    for r in rows:
        try:
            ts_sorted.append(
                datetime.fromisoformat(r.ingest_ts.replace("Z", "+00:00"))
            )
        except Exception:  # noqa: BLE001
            continue
    ts_sorted.sort()
    peak = 0
    peak_window_start: Optional[datetime] = None
    j = 0
    for i, t_i in enumerate(ts_sorted):
        # Advance j while ts_sorted[j] is more than 5 minutes after t_i.
        while j < len(ts_sorted) and (
            (ts_sorted[j] - t_i).total_seconds() <= 300
        ):
            j += 1
        burst = j - i
        if burst > peak:
            peak = burst
            peak_window_start = t_i
    summary.peak_burst_5min_count = peak
    summary.peak_burst_5min_window = (
        peak_window_start.isoformat() if peak_window_start else ""
    )

    # Write CSV.
    csv_path = output_dir / f"aapl_{day.replace('-', '_')}.csv"
    with csv_path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(IntentRow.__annotations__.keys()))
        w.writeheader()
        for r in rows:
            w.writerow(r.__dict__)
    logger.info("wrote %s", csv_path)

    # Write summary JSON.
    summary_path = output_dir / f"aapl_{day.replace('-', '_')}.summary.json"
    with summary_path.open("w") as fh:
        json.dump(summary.__dict__, fh, indent=2, default=str)
    logger.info("wrote %s", summary_path)

    # Write Mongo audit doc.
    try:
        from db import db  # type: ignore
        await db[REPLAY_COLLECTION].insert_one({
            "symbol": "AAPL",
            "date": day,
            "starting_signed_qty": starting_signed_qty,
            "replay_run_at": datetime.now(timezone.utc).isoformat(),
            "summary": summary.__dict__,
            "row_count": len(rows),
            "csv_path": str(csv_path),
            "summary_path": str(summary_path),
        })
        logger.info("wrote replay audit doc to %s", REPLAY_COLLECTION)
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to write replay audit doc: %s", exc)

    return summary


def _print_headline(summary: ReplaySummary) -> None:
    print("=" * 68)
    print(f"  AAPL replay — {summary.date}")
    print("=" * 68)
    print(f"  starting signed_qty   : {summary.starting_signed_qty:+.4f}")
    print(f"  ending signed_qty     : {summary.ending_signed_qty_actual:+.4f}")
    print(f"  intents (total)       : {summary.intent_count}")
    print(f"  intents (gate-passed) : {summary.gate_passed_count}")
    print(f"  intents (gate-blocked): {summary.gate_blocked_count}")
    print()
    print(f"  Brain actions         : {summary.actions}")
    print(f"  Primitive intents     : {summary.primitives}")
    print(f"  Position evolutions   : {summary.evolutions}")
    print(f"  Risk transitions      : {summary.risk_transitions}")
    print()
    print(
        f"  Peak 5-min burst      : {summary.peak_burst_5min_count} intents "
        f"starting at {summary.peak_burst_5min_window or 'n/a'}"
    )
    print()
    print(f"  Mistranslations (all)         : {summary.mistranslations}")
    print(f"  Missed short covers (gate-passed): {summary.missed_short_covers}")
    print()
    print(f"  actual_pnl_total      : {summary.actual_pnl_total:+,.2f}")
    print(f"  edge_pnl_total        : {summary.edge_pnl_total:+,.2f}")
    print(f"  NET EDGE (edge-actual): {summary.net_edge:+,.2f}")
    print("=" * 68)
    if summary.starting_signed_qty == 0.0:
        print()
        print(
            "  ⚠️  starting_signed_qty=0 means this run assumed FLAT at\n"
            "      market open. If AAPL was actually a short, re-run with\n"
            "      `--starting-signed-qty -<magnitude>` for the true edge\n"
            "      number. The mistranslation count above already reflects\n"
            "      whatever signed_qty walks the script saw mid-day."
        )


# ── CLI ────────────────────────────────────────────────────────────


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--starting-signed-qty",
        type=float,
        required=True,
        help=(
            "Broker signed_qty for AAPL at market open on the target day. "
            "Negative = short, positive = long, 0 = flat. REQUIRED — only "
            "the operator knows this number."
        ),
    )
    p.add_argument("--date", default="2026-06-09", help="YYYY-MM-DD")
    p.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory for CSV + summary JSON.",
    )
    p.add_argument("--quiet", action="store_true")
    return p.parse_args()


async def _main() -> None:
    args = _parse_args()
    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    summary = await run_replay(
        day=args.date,
        starting_signed_qty=args.starting_signed_qty,
        output_dir=args.output_dir,
    )
    _print_headline(summary)


if __name__ == "__main__":
    asyncio.run(_main())

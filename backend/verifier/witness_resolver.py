"""Witness W/L resolver — the missing verifier piece.

For over a week (since 2026-06-28) the polygon news witness worker has
been landing rows in `external_signals` as DEFAULT-HOSTILE (UNTRUSTED,
influence_allowed=False). The credibility ledger schema was designed
2026-02-23 with a promotion doctrine baked in:

    Phase 1 → 2  (UNTRUSTED → WATCHLIST):
        samples ≥ 50 AND orthogonal_win_rate > 0.50

    Phase 2 → 3  (WATCHLIST → TRUSTED):
        samples ≥ 200 AND verified_alpha > +0.02

But the code that turns 741 accumulated rows into `samples`, `wins`,
`losses`, `verified_alpha` was never written. That's this module.

## Doctrine

The resolver runs OUT-OF-BAND — never in the hot path, never inside a
gate decision. It reads facts (witness stances + subsequent price
movement) and writes audit conclusions (per-source credibility). The
Governor is the only party that reads the ledger to decide whether a
witness's `influence_allowed` gets to matter.

## Scope of this MVP

Included:
  * Deterministic classification of a witness row against a resolved
    price return (BUY/SELL/HOLD win-or-loss rules).
  * Aggregation into the per-source `external_source_credibility` row.
  * Auto-promotion (and auto-demotion) based on the pinned thresholds.
  * Admin-triggered force-run for manual verifier passes.
  * Dependency injection of the price fetch so unit tests are
    hermetic and the operator can wire real Webull/Kraken price
    history in a follow-up without touching resolver logic.

Explicitly out of scope for MVP (documented as follow-up):
  * Orthogonality tracking — for MVP `orthogonal_win_rate` is set to
    the raw win rate, on the assumption that early samples all count.
    Full doctrine would only credit a witness on calls no brain
    independently made.
  * Regime-conditional scoring, drawdown per stance, manipulation
    flag integration with RoadGuard.
  * Scheduled/nightly execution — for MVP the resolver is trigger-
    only via admin endpoint. Add a background loop after the first
    manual pass produces sane numbers.
  * Real price-history integration — the resolver takes a
    `price_fetcher` callable so the caller supplies it. See
    `_WEBULL_KRAKEN_TODO` below for the wire-in shape.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List, Literal, Optional

from db import db
from namespaces import EXTERNAL_SIGNALS, EXTERNAL_SOURCE_CREDIBILITY

logger = logging.getLogger(__name__)

# ─────────────────────────── Configuration ───────────────────────────
# All tunables live here so an operator can adjust doctrine without
# hunting through the resolver body.

RESOLUTION_HORIZON_HOURS = 24
# A witness stance is scored against the price move from
# `bar_close_ts` to `bar_close_ts + horizon_hours`. Longer horizons
# credit slower-burning news; shorter horizons credit reaction speed.
# 24h is a defensible starting point — the news cycle for most single-
# name headlines is same-day.

MIN_MOVE_BPS_FOR_DIRECTIONAL_WIN = 50
# A BUY stance "wins" if the symbol moved +50 bps (0.5%) or more; a
# SELL "wins" if it moved -50 bps or more. Below this threshold the
# move is treated as noise (see HOLD rules).

HOLD_WINDOW_BPS = 50
# A HOLD stance "wins" if the symbol moved by less than ±50 bps —
# i.e., HOLD is correct when the market stays quiet.

# Promotion thresholds — pinned by the credibility model docstring.
# Kept here as constants so tests and the resolver read the same numbers.
UNTRUSTED_TO_WATCHLIST_MIN_SAMPLES = 50
UNTRUSTED_TO_WATCHLIST_MIN_WIN_RATE = 0.50
WATCHLIST_TO_TRUSTED_MIN_SAMPLES = 200
WATCHLIST_TO_TRUSTED_MIN_ALPHA = 0.02


Side = Literal["BUY", "SELL", "HOLD"]
Outcome = Literal["win", "loss", "undetermined"]

# Type alias for the injected price fetcher. Takes (symbol, ts_iso)
# and returns the mid price at that timestamp, or None if unavailable.
# _WEBULL_KRAKEN_TODO: production wire-in should route equity symbols
# to Webull bar history and crypto pairs to Kraken OHLC. See
# `shared/market_data/webull_quotes.py` and (future) a Kraken OHLC
# feeder. The signature stays this simple; the routing is the impl.
PriceFetcher = Callable[[str, str], Awaitable[Optional[float]]]


# ─────────────────────────── Classification ───────────────────────────


def classify_outcome(
    side: Side,
    return_bps: float,
    *,
    directional_threshold_bps: float = MIN_MOVE_BPS_FOR_DIRECTIONAL_WIN,
    hold_window_bps: float = HOLD_WINDOW_BPS,
) -> Outcome:
    """Pure classification of a witness stance against a realized return.

    Deterministic, no side effects, no I/O. This is the arithmetic
    core of the resolver — everything else exists to feed inputs
    into this function and persist its output.
    """
    if side == "BUY":
        return "win" if return_bps >= directional_threshold_bps else "loss"
    if side == "SELL":
        return "win" if return_bps <= -directional_threshold_bps else "loss"
    if side == "HOLD":
        return "win" if abs(return_bps) < hold_window_bps else "loss"
    # Unknown side — refuse to classify. Row will be re-tried on next
    # pass; if it's still unrecognized, the operator has a schema drift.
    return "undetermined"


# ─────────────────────────── Aggregation ───────────────────────────


@dataclass
class SourceAggregate:
    """In-memory rollup for one source before we write it back."""
    source: str
    samples: int = 0
    wins: int = 0
    losses: int = 0
    total_return_bps: float = 0.0

    def add(self, outcome: Outcome, return_bps: float) -> None:
        if outcome == "undetermined":
            return
        self.samples += 1
        if outcome == "win":
            self.wins += 1
        else:
            self.losses += 1
        self.total_return_bps += return_bps

    @property
    def orthogonal_win_rate(self) -> float:
        # MVP shortcut: raw win rate. Full doctrine tracks
        # orthogonality (calls no brain independently made). Documented
        # in module header as follow-up.
        if self.samples == 0:
            return 0.0
        return self.wins / self.samples

    @property
    def avg_return_bps(self) -> float:
        if self.samples == 0:
            return 0.0
        return self.total_return_bps / self.samples

    @property
    def verified_alpha(self) -> float:
        # verified_alpha is the aggregate directional edge. For MVP:
        # net average return per stance, expressed as a fraction
        # (bps ÷ 10_000). Refined attribution vs baseline is a
        # follow-up.
        return self.avg_return_bps / 10_000.0


# ─────────────────────────── Promotion ───────────────────────────


def next_status(
    current_status: str,
    samples: int,
    orthogonal_win_rate: float,
    verified_alpha: float,
) -> str:
    """Determine the appropriate status given aggregated metrics.

    Monotonic in the promotion direction; also handles demotion when
    a previously-trusted source falls out of threshold.
    """
    if current_status == "UNTRUSTED":
        if (
            samples >= UNTRUSTED_TO_WATCHLIST_MIN_SAMPLES
            and orthogonal_win_rate > UNTRUSTED_TO_WATCHLIST_MIN_WIN_RATE
        ):
            return "WATCHLIST"
        return "UNTRUSTED"

    if current_status == "WATCHLIST":
        if (
            samples >= WATCHLIST_TO_TRUSTED_MIN_SAMPLES
            and verified_alpha > WATCHLIST_TO_TRUSTED_MIN_ALPHA
        ):
            return "TRUSTED"
        # Demotion: if the WATCHLIST source's win rate has fallen
        # back below the promotion floor, drop it. This is the same
        # threshold, deliberately — no hysteresis in MVP. Add a
        # separate demote threshold if the ledger starts flapping.
        if (
            samples >= UNTRUSTED_TO_WATCHLIST_MIN_SAMPLES
            and orthogonal_win_rate <= UNTRUSTED_TO_WATCHLIST_MIN_WIN_RATE
        ):
            return "UNTRUSTED"
        return "WATCHLIST"

    if current_status == "TRUSTED":
        # Demote to WATCHLIST if verified_alpha turns negative on a
        # meaningful sample. Full doctrine has a 30-day rolling
        # window; MVP uses lifetime alpha. Refine after first
        # observed demotion event.
        if samples >= WATCHLIST_TO_TRUSTED_MIN_SAMPLES and verified_alpha < 0:
            return "WATCHLIST"
        return "TRUSTED"

    # Unknown status — do not touch. The verifier does not invent
    # new phase names.
    return current_status


# ─────────────────────────── Main resolver ───────────────────────────


@dataclass
class ResolverSummary:
    """What one resolver pass did. Returned to the caller and logged."""
    source: str
    rows_examined: int
    rows_resolved: int
    rows_undetermined: int
    rows_skipped_price_missing: int
    rows_skipped_too_recent: int
    aggregate_before: Dict[str, Any]
    aggregate_after: Dict[str, Any]
    status_before: str
    status_after: str
    status_changed: bool


async def resolve_source(
    source: str,
    price_fetcher: PriceFetcher,
    *,
    horizon_hours: int = RESOLUTION_HORIZON_HOURS,
    now: Optional[datetime] = None,
    limit: int = 5000,
) -> ResolverSummary:
    """Resolve all unresolved witness rows for one source, update the
    credibility ledger, and (if thresholds hit) promote or demote.

    Idempotent: rows already marked with `resolution_outcome` are
    skipped. Safe to re-run.

    Args:
        source: witness source ("polygon", "pine", "twitter", etc.)
        price_fetcher: async callable(symbol, ts_iso) → mid price or None.
            Injected so the resolver core stays independent of the
            broker-specific price history integration.
        horizon_hours: how long after `bar_close_ts` to look for the
            resolved price. Default 24h.
        now: injectable clock for testing. Defaults to UTC now.
        limit: max rows to process in one pass (defensive cap).
    """
    _now = now or datetime.now(timezone.utc)
    horizon_cutoff = _now - timedelta(hours=horizon_hours)

    # Load current credibility row (or set a hostile default so we
    # never write a row that skipped the UNTRUSTED default).
    cred_before = await db[EXTERNAL_SOURCE_CREDIBILITY].find_one(
        {"source": source}, {"_id": 0},
    ) or {
        "source": source, "status": "UNTRUSTED",
        "samples": 0, "wins": 0, "losses": 0,
        "verified_alpha": 0.0, "orthogonal_win_rate": 0.0,
        "avg_return_bps": 0.0,
    }
    status_before = str(cred_before.get("status", "UNTRUSTED"))

    # Seed the aggregate with what's already in the ledger — we're
    # additive; we don't recompute from scratch. If the operator
    # wants a full recompute, they zero the ledger row and re-run.
    agg = SourceAggregate(
        source=source,
        samples=int(cred_before.get("samples") or 0),
        wins=int(cred_before.get("wins") or 0),
        losses=int(cred_before.get("losses") or 0),
        total_return_bps=(
            float(cred_before.get("avg_return_bps") or 0.0)
            * float(cred_before.get("samples") or 0)
        ),
    )

    rows_examined = 0
    rows_resolved = 0
    rows_undetermined = 0
    rows_skipped_price_missing = 0
    rows_skipped_too_recent = 0

    query = {
        "source": source,
        "resolution_outcome": {"$exists": False},
    }
    cursor = db[EXTERNAL_SIGNALS].find(query, {"_id": 0}).limit(limit)
    async for row in cursor:
        rows_examined += 1

        bar_close_ts = row.get("bar_close_ts")
        symbol = row.get("symbol")
        side = row.get("side")
        if not bar_close_ts or not symbol or side not in ("BUY", "SELL", "HOLD"):
            # Malformed row. Do not classify. Do not silently write
            # a fake outcome. The operator will see it in
            # `rows_undetermined` and can inspect.
            rows_undetermined += 1
            continue

        try:
            bar_close_dt = datetime.fromisoformat(
                str(bar_close_ts).replace("Z", "+00:00"),
            )
        except (TypeError, ValueError):
            rows_undetermined += 1
            continue

        # Guard: skip rows that haven't had time to resolve yet.
        if bar_close_dt > horizon_cutoff:
            rows_skipped_too_recent += 1
            continue

        resolved_ts = (bar_close_dt + timedelta(hours=horizon_hours)).isoformat()
        p0 = await price_fetcher(symbol, bar_close_dt.isoformat())
        p1 = await price_fetcher(symbol, resolved_ts)

        if p0 is None or p1 is None or p0 <= 0:
            rows_skipped_price_missing += 1
            continue

        return_bps = ((p1 - p0) / p0) * 10_000.0
        outcome = classify_outcome(side, return_bps)

        if outcome == "undetermined":
            rows_undetermined += 1
            continue

        agg.add(outcome, return_bps)
        rows_resolved += 1

        # Persist per-row resolution so we don't re-classify the same
        # row on the next pass. Fields are additive-only; the
        # webhook contract that lets the witness $setOnInsert stays
        # intact because we're mutating fields the witness never
        # writes.
        await db[EXTERNAL_SIGNALS].update_one(
            {"id": row.get("id")},
            {"$set": {
                "resolution_outcome": outcome,
                "resolution_return_bps": return_bps,
                "resolution_p0": p0,
                "resolution_p1": p1,
                "resolution_horizon_hours": horizon_hours,
                "resolved_at": _now.isoformat(),
            }},
        )

    # Decide next status from the fresh aggregate.
    status_after = next_status(
        status_before,
        agg.samples,
        agg.orthogonal_win_rate,
        agg.verified_alpha,
    )

    # Write the updated credibility ledger row.
    new_doc: Dict[str, Any] = {
        "source": source,
        "status": status_after,
        "samples": agg.samples,
        "wins": agg.wins,
        "losses": agg.losses,
        "verified_alpha": agg.verified_alpha,
        "orthogonal_win_rate": agg.orthogonal_win_rate,
        "avg_return_bps": agg.avg_return_bps,
        "updated_at": _now.isoformat(),
    }
    if status_after != status_before:
        if _phase_rank(status_after) > _phase_rank(status_before):
            new_doc["last_promoted_at"] = _now.isoformat()
        else:
            new_doc["last_demoted_at"] = _now.isoformat()

    await db[EXTERNAL_SOURCE_CREDIBILITY].update_one(
        {"source": source},
        {"$set": new_doc},
        upsert=True,
    )

    # When a source is promoted past UNTRUSTED, flip
    # `influence_allowed` on every row from that source. The Governor
    # reads this per-row (not the ledger) at decision time, so
    # forgetting this step would silently keep the witness voiceless
    # even after promotion.
    if status_after != "UNTRUSTED" and status_before == "UNTRUSTED":
        await db[EXTERNAL_SIGNALS].update_many(
            {"source": source},
            {"$set": {"influence_allowed": True, "verifier_status": status_after}},
        )
    elif status_after == "UNTRUSTED" and status_before != "UNTRUSTED":
        # Demotion — quarantine the source's rows again.
        await db[EXTERNAL_SIGNALS].update_many(
            {"source": source},
            {"$set": {"influence_allowed": False, "verifier_status": "UNTRUSTED"}},
        )

    summary = ResolverSummary(
        source=source,
        rows_examined=rows_examined,
        rows_resolved=rows_resolved,
        rows_undetermined=rows_undetermined,
        rows_skipped_price_missing=rows_skipped_price_missing,
        rows_skipped_too_recent=rows_skipped_too_recent,
        aggregate_before={k: cred_before.get(k) for k in (
            "samples", "wins", "losses", "orthogonal_win_rate", "verified_alpha",
        )},
        aggregate_after={
            "samples": agg.samples,
            "wins": agg.wins,
            "losses": agg.losses,
            "orthogonal_win_rate": agg.orthogonal_win_rate,
            "verified_alpha": agg.verified_alpha,
        },
        status_before=status_before,
        status_after=status_after,
        status_changed=status_before != status_after,
    )
    logger.info(
        "witness_resolver source=%s examined=%d resolved=%d "
        "status=%s->%s samples=%d wins=%d losses=%d",
        source, rows_examined, rows_resolved,
        status_before, status_after,
        agg.samples, agg.wins, agg.losses,
    )
    return summary


def _phase_rank(status: str) -> int:
    return {"UNTRUSTED": 1, "WATCHLIST": 2, "TRUSTED": 3}.get(status, 0)

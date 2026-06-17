"""Fractional-share sizing — seat-layer execution helper.

Doctrine pin (operator, 2026-02-20):

    "Fractional does not make the signal better.
     Fractional makes the risk smaller."

This module owns the *seat-level* conversion from a notional-USD
request to a fractional-share quantity that the broker can fill.
It is consumed by the broker_router AFTER the brain has emitted an
intent and AFTER the gate chain has approved it. By keeping the
conversion at the seat layer (rather than the brain layer), the
doctrine boundary is preserved:

  * Brain says: "BUY NVDA, 0.74 conviction"
  * Seat says : "NVDA $180; $10 notional; fractional supported;
                 RTH open; symbol eligible → qty = 0.0555 shares"
  * Broker    : Webull v2 + entrust_type=QTY (or AMOUNT fallback)

Webull constraints (per their public API + help docs, 2026-02):
  * Fractional MARKET orders only (no LIMIT)
  * Quantity in (0, 1) for "less than a share" semantics
  * Regular session only (no extended hours)
  * Minimum order value $5
  * Symbol must be on Webull's fractional-eligible US equity/ETF
    universe — we treat this as opt-out via env (default: assume
    eligible for any US equity ticker; operator can pin a
    whitelist if they hit a denied symbol).

Kraken: every USD pair supports fractional natively. No eligibility
check needed.
"""
from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass
from typing import Optional

from shared.market_hours import is_equity_rth


logger = logging.getLogger("risedual.fractional_sizing")


# Minimum order value per Webull's fractional API docs.
WEBULL_FRACTIONAL_MIN_USD = 5.00

# Above this price, a whole-share order at the per-order budget is
# typically impossible — fractional becomes the only path. Tuned
# conservatively; the seat consults this only as a hint, not a hard
# cutoff (the per-order budget vs price is the actual constraint).
DEFAULT_MAX_WHOLE_SHARE_PRICE = 20.00

# Webull fractional precision: 5 decimal places (per API docs).
WEBULL_FRACTIONAL_PRECISION = 5

# Kraken fractional precision varies per pair (8 dp for BTC, less for
# others). Conservative default of 8 keeps math safe; the broker
# will reject if the venue requires fewer.
KRAKEN_FRACTIONAL_PRECISION = 8


@dataclass(frozen=True)
class FractionalSizingDecision:
    """Return type for `size_for_fractional`. Carries the chosen
    submission mode + computed quantity + audit trail of the math
    so the post-mortem panel can render "$10 ÷ $180 = 0.0555 shares"
    inline instead of forcing the operator to recompute."""
    eligible: bool
    reason: str
    quantity: Optional[float]
    notional_usd: float
    last_price: Optional[float]
    submission_mode: str   # "QTY" | "AMOUNT" | "WHOLE_SHARE" | "REJECT"
    broker: str


def _is_us_equity_fractional_eligible(symbol: str) -> bool:
    """Webull supports fractional for a curated US equity/ETF set.

    Operator-tunable via the `WEBULL_FRACTIONAL_INELIGIBLE_SYMBOLS`
    env var (comma-separated list — symbols on the blacklist are
    refused). Default: assume any US-equity ticker is eligible,
    fail to whole-share on a rejection from Webull. Less surprising
    than maintaining a whitelist that goes stale.
    """
    blacklist = os.environ.get("WEBULL_FRACTIONAL_INELIGIBLE_SYMBOLS", "")
    blacklisted = {
        s.strip().upper() for s in blacklist.split(",") if s.strip()
    }
    return symbol.upper() not in blacklisted


def _floor_to_precision(value: float, decimals: int) -> float:
    """Truncate (not round) to the broker's precision. Truncation
    avoids the case where rounding pushes the order to $5.01 when
    the operator budgeted exactly $5.00, which can fail Webull's
    min-order check."""
    scale = 10 ** decimals
    return math.floor(value * scale) / scale


def size_for_fractional(
    *,
    broker: str,
    symbol: str,
    notional_usd: float,
    last_price: Optional[float],
    lane: str,
) -> FractionalSizingDecision:
    """Compute the broker-ready fractional quantity for `notional_usd`.

    Decision tree (in order):

      1. `last_price` known and notional ≥ price       → WHOLE_SHARE
         (operator's budget covers ≥ 1 share; no need for fractional;
         broker can fill normally — fewer moving parts).
      2. broker == "kraken"                            → AMOUNT
         (Kraken native fractional via `volume` param; no eligibility
         dance; sub-share fills are first-class).
      3. broker == "webull" and lane == "equity"       → see below
      4. anything else                                 → REJECT
         (caller falls back to whole-share path; downstream caps
         catch the "but the share is more expensive than the budget"
         case with the existing WEBULL_NOTIONAL_ABOVE_CAP message).

    Webull equity sub-tree:

      a. RTH closed              → REJECT
         (fractional is regular-session only per Webull docs).
      b. symbol blacklisted      → REJECT (op-tunable).
      c. notional < $5           → REJECT (below Webull min).
      d. last_price missing      → AMOUNT
         (let Webull's $/notional path compute the qty server-side;
         we lose audit precision but get the order through).
      e. last_price known, in band → QTY
         (compute qty = floor(notional / price, 5dp); submit via
         v2 QTY mode for full audit of the share count).
    """
    if last_price is not None and last_price > 0 and notional_usd >= last_price:
        # Whole-share path covers it; skip fractional machinery.
        whole_qty = math.floor(notional_usd / last_price)
        return FractionalSizingDecision(
            eligible=False,
            reason=(
                f"notional ${notional_usd:.2f} ≥ price ${last_price:.2f}; "
                f"whole-share order ({whole_qty} sh) is sufficient"
            ),
            quantity=float(whole_qty),
            notional_usd=notional_usd,
            last_price=last_price,
            submission_mode="WHOLE_SHARE",
            broker=broker,
        )

    if broker == "kraken":
        # Kraken: AMOUNT-mode by convention but the volume= param is
        # already fractional. We tag as AMOUNT here so the adapter
        # picks the dollar-denominated path; if last_price is known
        # we still pre-compute qty for audit.
        qty = None
        if last_price is not None and last_price > 0:
            qty = _floor_to_precision(
                notional_usd / last_price, KRAKEN_FRACTIONAL_PRECISION,
            )
        return FractionalSizingDecision(
            eligible=True,
            reason="kraken_native_fractional",
            quantity=qty,
            notional_usd=notional_usd,
            last_price=last_price,
            submission_mode="AMOUNT",
            broker=broker,
        )

    if broker == "webull" and lane == "equity":
        # RTH gate — Webull rejects fractional outside regular session.
        if not is_equity_rth():
            return FractionalSizingDecision(
                eligible=False,
                reason="webull_fractional_rth_only: regular session required",
                quantity=None,
                notional_usd=notional_usd,
                last_price=last_price,
                submission_mode="REJECT",
                broker=broker,
            )
        if not _is_us_equity_fractional_eligible(symbol):
            return FractionalSizingDecision(
                eligible=False,
                reason=(
                    f"webull_fractional_blacklisted: {symbol} on "
                    f"WEBULL_FRACTIONAL_INELIGIBLE_SYMBOLS"
                ),
                quantity=None,
                notional_usd=notional_usd,
                last_price=last_price,
                submission_mode="REJECT",
                broker=broker,
            )
        if notional_usd < WEBULL_FRACTIONAL_MIN_USD:
            return FractionalSizingDecision(
                eligible=False,
                reason=(
                    f"webull_fractional_below_min: ${notional_usd:.2f} "
                    f"< ${WEBULL_FRACTIONAL_MIN_USD:.2f} minimum order"
                ),
                quantity=None,
                notional_usd=notional_usd,
                last_price=last_price,
                submission_mode="REJECT",
                broker=broker,
            )
        # last_price known → compute fractional qty + submit via QTY mode
        # so the audit trail records exactly how many shares were
        # purchased. Last_price unknown → AMOUNT mode (Webull computes
        # qty server-side; less precise audit but still fills).
        if last_price is not None and last_price > 0:
            qty = _floor_to_precision(
                notional_usd / last_price, WEBULL_FRACTIONAL_PRECISION,
            )
            return FractionalSizingDecision(
                eligible=True,
                reason=(
                    f"webull_fractional_qty: ${notional_usd:.2f} / "
                    f"${last_price:.2f} = {qty} shares"
                ),
                quantity=qty,
                notional_usd=notional_usd,
                last_price=last_price,
                submission_mode="QTY",
                broker=broker,
            )
        return FractionalSizingDecision(
            eligible=True,
            reason="webull_fractional_amount: last_price unknown; server-side qty resolution",
            quantity=None,
            notional_usd=notional_usd,
            last_price=last_price,
            submission_mode="AMOUNT",
            broker=broker,
        )

    return FractionalSizingDecision(
        eligible=False,
        reason=f"fractional_not_supported: broker={broker!r} lane={lane!r}",
        quantity=None,
        notional_usd=notional_usd,
        last_price=last_price,
        submission_mode="REJECT",
        broker=broker,
    )

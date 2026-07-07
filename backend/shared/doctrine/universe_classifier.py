"""Universe classifier — pure symbol → universe-class routing.

Doctrine pin (2026-02-19, operator directive):
    Before doctrine can score a snapshot it needs to know WHICH
    doctrine to apply. Historically this was tangled inside
    `lane_doctrine_router.py` as a chain of `strategy` /
    `market_cap_band` string checks — reproducible but hard to
    extend when new instrument classes land (ETF, futures).

    This module owns the classification only. No scoring, no seat
    logic. Callers hand it a snapshot; it returns one of five
    universe classes:

        CRYPTO             — snapshot.lane == "crypto"
        SMALL_CAP_MOMENTUM — explicit small-cap band OR gap/pullback
                             strategy hint (Warrior Trading rubric)
        LARGE_CAP          — mega/large band, or listed on the pinned
                             mega-cap roster (fail-open default for
                             our curated large-cap watchlist)
        ETF                — pinned ETF roster (SPY/QQQ/IWM/DIA/...)
        UNKNOWN            — no lane or contradictory hints; caller
                             falls back to a REJECT packet

    Explicit hints beat pinned rosters. The pinned mega-cap set is
    only consulted when `market_cap_band` is missing / "unknown".
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Dict


class UniverseClass(str, Enum):
    CRYPTO = "CRYPTO"
    SMALL_CAP_MOMENTUM = "SMALL_CAP_MOMENTUM"
    LARGE_CAP = "LARGE_CAP"
    ETF = "ETF"
    UNKNOWN = "UNKNOWN"


# Pinned mega/large-cap roster — mirrors
# `snapshot_enrich/equity_doctrine.py::_MEGA_CAP_SYMBOLS`. Kept as a
# separate constant here so the classifier can be imported without
# pulling the Webull enricher dependency chain (used by tests + the
# admin API layer, which has no Webull entitlement).
LARGE_CAP_SYMBOLS = frozenset({
    "AAPL", "MSFT", "GOOGL", "GOOG", "AMZN", "NVDA", "META", "TSLA",
    "BRK.A", "BRK.B", "LLY", "AVGO", "JPM", "V", "WMT", "XOM", "JNJ",
    "MA", "PG", "ORCL", "HD", "BAC", "ABBV", "COST", "NFLX", "KO",
    "ADBE", "CRM", "CSCO", "AMD", "MCD", "PEP", "TMO", "ABT", "QCOM",
    "BABA", "TSM", "ASML", "AXP", "DIS", "INTC", "IBM", "BA",
    "MSTR", "ABNB", "UBER", "PLTR", "COIN", "SHOP",
})

# Broad-market / sector ETFs — behave like large-caps liquidity-wise
# but are worth flagging separately so downstream Patent J can grade
# ETF momentum independently of single-name mega-caps.
ETF_SYMBOLS = frozenset({
    "SPY", "QQQ", "IWM", "DIA", "VOO", "VTI", "XLF", "XLK", "XLE",
    "XLV", "XLY", "XLP", "XLI", "XLU", "XLB", "XLRE", "XLC",
    "SMH", "SOXX", "ARKK", "TQQQ", "SQQQ", "TLT", "GLD", "SLV",
    "UVXY", "VXX",
})


def classify_universe(snapshot: Dict[str, Any]) -> UniverseClass:
    """Classify a snapshot into a `UniverseClass`.

    Precedence (highest first):
        1. `lane == "crypto"` → CRYPTO
        2. explicit small-cap band OR small-cap-strategy hint
           → SMALL_CAP_MOMENTUM
        3. explicit large/mega band → LARGE_CAP
        4. pinned ETF roster → ETF
        5. pinned mega-cap roster → LARGE_CAP
        6. no hint left → UNKNOWN (loud — never silently defaults
           into a scored doctrine; caller must short-circuit to
           NO_DATA/REJECT rather than fall through to a builder)
    """
    if not isinstance(snapshot, dict):
        return UniverseClass.UNKNOWN

    lane = str(snapshot.get("lane") or "").lower()
    if lane == "crypto":
        return UniverseClass.CRYPTO

    strategy = str(snapshot.get("strategy") or "").lower()
    band = str(snapshot.get("market_cap_band") or "").lower()
    symbol = str(snapshot.get("symbol") or "").upper()

    # (2) explicit small-cap opt-in beats everything else
    if band in ("small", "micro", "nano"):
        return UniverseClass.SMALL_CAP_MOMENTUM
    if strategy in ("gap_and_go", "micro_pullback"):
        return UniverseClass.SMALL_CAP_MOMENTUM

    # (3) explicit large-cap band
    if band in ("large", "mega"):
        return UniverseClass.LARGE_CAP

    # (4) pinned ETF roster — checked before mega-cap roster so
    # SPY/QQQ don't collide with a stray large-cap membership
    if symbol in ETF_SYMBOLS:
        return UniverseClass.ETF

    # (5) pinned mega-cap roster
    if symbol in LARGE_CAP_SYMBOLS:
        return UniverseClass.LARGE_CAP

    # (6) No lane-default fallback. Equity with zero classification
    # hint = UNKNOWN, by design.
    #
    # Doctrine pin (2026-02-19, operator directive after review):
    # This function must NEVER silently default into a scored
    # doctrine. If a symbol reached the classifier with no pinned
    # roster hit, no `market_cap_band`, and no strategy hint, the
    # correct answer is UNKNOWN — the registry will short-circuit to
    # a NO_DATA packet so the operator sees the classification gap
    # in the funnel instead of it silently scoring under whichever
    # doctrine happened to be "the default." The earlier `has_news
    # = False` / `float_millions = 999999` silent-default class of
    # bugs is the reason this branch fails loud.
    #
    # If you WANT "curated watchlist = large-cap by default," that
    # decision belongs in the ROSTER (add the symbol to
    # LARGE_CAP_SYMBOLS or stamp `market_cap_band` upstream in the
    # enricher). It does NOT belong as an implicit fallback here.
    return UniverseClass.UNKNOWN

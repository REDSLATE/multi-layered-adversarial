"""NYSE trading-day calendar helper.

Doctrine:
  Pure date math. No external API calls, no dynamic holiday lookup
  service. NYSE holidays are pinned for the years the operator
  expects this codebase to run; adding 2027+ is a one-line update.

Used by the daily-snapshot worker to:
  - Decide whether to capture today (skip weekends + holidays).
  - Compute the "last N trading days" wipe-cutoff for retention.

Times:
  All times in US/Eastern. NYSE regular session: 09:30-16:00 ET.
  Early-close days (1pm close, e.g., July 3 day-before, day after
  Thanksgiving, Christmas Eve) are still trading days; the worker
  fires its 12:30 + 16:05 captures as normal — the 16:05 just
  reads stale bars from the 13:00 close. Acceptable for this v1.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Iterable

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover — Python < 3.9
    from backports.zoneinfo import ZoneInfo  # type: ignore

NYSE_TZ = ZoneInfo("America/New_York")

# Pinned NYSE full-day closures. When extending: append the year, do
# NOT remove past years (tests use historical dates).
NYSE_HOLIDAYS: frozenset[date] = frozenset({
    # 2025
    date(2025, 1, 1),    # New Year's Day
    date(2025, 1, 20),   # MLK Day
    date(2025, 2, 17),   # Presidents' Day
    date(2025, 4, 18),   # Good Friday
    date(2025, 5, 26),   # Memorial Day
    date(2025, 6, 19),   # Juneteenth
    date(2025, 7, 4),    # Independence Day
    date(2025, 9, 1),    # Labor Day
    date(2025, 11, 27),  # Thanksgiving
    date(2025, 12, 25),  # Christmas
    # 2026
    date(2026, 1, 1),
    date(2026, 1, 19),
    date(2026, 2, 16),
    date(2026, 4, 3),
    date(2026, 5, 25),
    date(2026, 6, 19),
    date(2026, 7, 3),    # observed (July 4 is Saturday)
    date(2026, 9, 7),
    date(2026, 11, 26),
    date(2026, 12, 25),
    # 2027
    date(2027, 1, 1),
    date(2027, 1, 18),
    date(2027, 2, 15),
    date(2027, 3, 26),
    date(2027, 5, 31),
    date(2027, 6, 18),   # observed (June 19 is Saturday)
    date(2027, 7, 5),    # observed (July 4 is Sunday)
    date(2027, 9, 6),
    date(2027, 11, 25),
    date(2027, 12, 24),  # observed (Dec 25 is Saturday)
})


def is_trading_day(d: date) -> bool:
    """True if `d` is a regular NYSE session day (no holiday, weekday)."""
    if d.weekday() >= 5:  # 5=Sat, 6=Sun
        return False
    return d not in NYSE_HOLIDAYS


def now_eastern() -> datetime:
    """Current wall-clock time in US/Eastern."""
    return datetime.now(tz=NYSE_TZ)


def previous_n_trading_days(anchor: date, n: int) -> list[date]:
    """Return the N most-recent NYSE trading days ending at `anchor`
    (inclusive if `anchor` is itself a trading day).

    Used by the wipe pass: "delete snapshots whose market_day is
    older than the Nth-most-recent trading day."
    """
    out: list[date] = []
    cursor = anchor
    while len(out) < n:
        if is_trading_day(cursor):
            out.append(cursor)
        cursor -= timedelta(days=1)
        # Hard stop — protect against runaway loops if NYSE_HOLIDAYS
        # somehow swallows an entire month.
        if (anchor - cursor).days > 60:  # pragma: no cover
            break
    return out


def market_day_today() -> date:
    """The NYSE market day this clock is on.

    Calling this at 23:59 ET on a trading day returns that day's
    date. Calling it at 02:00 ET on a weekend returns the previous
    Friday. Calling it at 08:00 ET on a holiday returns the next
    trading day's preceding session.

    Convention: a snapshot row's `market_day` is the trading day it
    captures evidence FOR (open/midday/close all share the same
    market_day on a given session).
    """
    today = now_eastern().date()
    cursor = today
    while not is_trading_day(cursor):
        cursor -= timedelta(days=1)
    return cursor


__all__ = (
    "NYSE_TZ",
    "NYSE_HOLIDAYS",
    "is_trading_day",
    "now_eastern",
    "previous_n_trading_days",
    "market_day_today",
)

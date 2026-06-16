"""US equity market hours gate.

Doctrine pin (operator, 2026-02-20):

    Webull's equity order endpoint returns HTTP 417
    `INVALID_PARAMETER — The time you sent is not supported` for any
    equity order placed outside Regular Trading Hours (RTH) M-F
    9:30–16:00 ET. The auto-submitter was firing those orders blindly
    and the gate chain only learned the order was DOA *after* the
    broker rejected it. That:

      * Burned an MC receipt slot per refused order.
      * Hammered the Webull API rate budget.
      * Cluttered the post-mortem panel with "broker error" rows that
        weren't really broker errors — they were operator-time errors.

    This module gives the auto-submitter a CHEAP local check so equity
    intents are quietly held until the next RTH window. Crypto is
    untouched (Kraken trades 24/7).

Implementation:
    * Uses `zoneinfo` (stdlib) so DST flips automatically.
    * US federal & exchange holidays hardcoded for 2026-2027. Refresh
      annually. Operator can override via env if needed.
    * Half-day closes (Black Friday, Christmas Eve) treated as full
      RTH for now — losing the last 3 hours of trading on those days
      is acceptable; spurious 417s in those windows aren't.

Public API:
    is_equity_rth(now_utc=None) -> bool
        True iff current US/Eastern time is M-F 09:30–16:00 AND not
        a market holiday.

    next_rth_open_iso(now_utc=None) -> str
        ISO-8601 timestamp of the next RTH window open. Used for
        operator-facing "held until X" messaging.
"""
from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo


_ET = ZoneInfo("America/New_York")

# US equity market full-day closures. Update annually each December
# when NYSE publishes the next year's calendar.
#
# Half-day closes (Black Friday, Christmas Eve when it falls on a
# weekday) are NOT in this list — we accept losing those tail hours
# rather than maintain a second calendar.
_MARKET_HOLIDAYS: frozenset[date] = frozenset({
    # 2026
    date(2026, 1, 1),   # New Year's Day
    date(2026, 1, 19),  # MLK Day
    date(2026, 2, 16),  # Presidents' Day
    date(2026, 4, 3),   # Good Friday
    date(2026, 5, 25),  # Memorial Day
    date(2026, 6, 19),  # Juneteenth
    date(2026, 7, 3),   # Independence Day observed (July 4 = Sat)
    date(2026, 9, 7),   # Labor Day
    date(2026, 11, 26), # Thanksgiving
    date(2026, 12, 25), # Christmas
    # 2027
    date(2027, 1, 1),   # New Year's Day
    date(2027, 1, 18),  # MLK Day
    date(2027, 2, 15),  # Presidents' Day
    date(2027, 3, 26),  # Good Friday
    date(2027, 5, 31),  # Memorial Day
    date(2027, 6, 18),  # Juneteenth observed
    date(2027, 7, 5),   # Independence Day observed
    date(2027, 9, 6),   # Labor Day
    date(2027, 11, 25), # Thanksgiving
    date(2027, 12, 24), # Christmas observed
})


_RTH_OPEN = time(9, 30)
_RTH_CLOSE = time(16, 0)


def _to_et(now_utc: Optional[datetime] = None) -> datetime:
    """Convert UTC (or naive-as-UTC) to America/New_York."""
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    elif now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    return now_utc.astimezone(_ET)


def _is_business_day(d: date) -> bool:
    """Weekday and not a hardcoded full-day market closure."""
    if d.weekday() >= 5:  # Sat/Sun
        return False
    if d in _MARKET_HOLIDAYS:
        return False
    return True


def is_equity_rth(now_utc: Optional[datetime] = None) -> bool:
    """True iff `now_utc` falls inside the US equity Regular Trading
    Hours window: M-F 09:30–16:00 ET, excluding market holidays.

    Operator override:
        `RISEDUAL_BYPASS_MARKET_HOURS=true` forces this to return True
        regardless of clock. Use for backtests against a live SDK or
        when you genuinely need to fire a market order outside RTH
        and accept the 417. Default OFF — fail-closed-to-RTH.
    """
    if (os.environ.get("RISEDUAL_BYPASS_MARKET_HOURS") or "").strip().lower() in {
        "true", "1", "yes", "on",
    }:
        return True
    et = _to_et(now_utc)
    if not _is_business_day(et.date()):
        return False
    return _RTH_OPEN <= et.time() < _RTH_CLOSE


def next_rth_open_iso(now_utc: Optional[datetime] = None) -> str:
    """ISO-8601 (UTC) timestamp of the next RTH open from `now_utc`.

    Used by the auto-submitter to tell the operator when a held
    intent will be retried. Looks up to 14 days ahead — covers the
    longest plausible holiday gap (e.g., Christmas/NYE bridges).
    Returns empty string if no RTH open found in the lookahead
    window (should never happen in practice).
    """
    et = _to_et(now_utc)
    for offset in range(0, 14):
        candidate_date = (et.date() + timedelta(days=offset))
        if not _is_business_day(candidate_date):
            continue
        candidate_et = datetime.combine(candidate_date, _RTH_OPEN, tzinfo=_ET)
        if candidate_et > et:
            return candidate_et.astimezone(timezone.utc).isoformat()
    return ""


def market_hours_reason(now_utc: Optional[datetime] = None) -> str:
    """Human-readable reason string for the audit log when the
    market-hours gate blocks an equity intent."""
    et = _to_et(now_utc)
    if et.date() in _MARKET_HOLIDAYS:
        return (
            f"equity_after_hours: US market closed for holiday "
            f"({et.date().isoformat()}); next open "
            f"{next_rth_open_iso(now_utc)}"
        )
    if et.weekday() >= 5:
        return (
            f"equity_after_hours: weekend ({et.strftime('%A')}); "
            f"next open {next_rth_open_iso(now_utc)}"
        )
    return (
        f"equity_after_hours: outside RTH "
        f"(ET {et.strftime('%H:%M')}); RTH=09:30-16:00; "
        f"next open {next_rth_open_iso(now_utc)}"
    )

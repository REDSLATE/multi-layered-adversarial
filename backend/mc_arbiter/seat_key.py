"""Seat key canonicalization.

A seat is `(lane, symbol, 5-min UTC bucket)`. The key format is:

    f"{lane}:{symbol}:{bucket_iso}"

where `bucket_iso` is UTC time floored to the 5-minute boundary,
formatted as `YYYY-MM-DDTHH:MM:00Z` (no microseconds, `Z` suffix).

Rationale (design freeze §2):
    * Bounded cardinality — ~288 seats/symbol/day.
    * 5-min bucket matches the 15m grade horizon: brains get one
      full bucket to submit opinions before arbitration closes.
    * Canonical string is deterministic — two brains emitting at
      the same tick produce the SAME `seat_key`, so `mc_seats`
      compound index `(seat_key, brain)` naturally groups the
      competition.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

BUCKET_MINUTES = 5

LANES = frozenset({"equity", "crypto"})


def bucket_iso(dt: Optional[datetime] = None, minutes: int = BUCKET_MINUTES) -> str:
    """Floor `dt` (default: now UTC) to the nearest `minutes`
    boundary and return ISO-8601 with `Z` suffix.

    Example (at 14:33:47 UTC, minutes=5):
        >>> bucket_iso(datetime(2026, 7, 11, 14, 33, 47, tzinfo=timezone.utc))
        '2026-07-11T14:30:00Z'

    Naive datetimes are treated as UTC — no silent local-time
    interpretation (that's the exact class of bug we've been
    stamping out; see 3-clock write-health work).
    """
    if dt is None:
        dt = datetime.now(timezone.utc)
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)

    floored_minute = (dt.minute // minutes) * minutes
    floored = dt.replace(minute=floored_minute, second=0, microsecond=0)
    return floored.strftime("%Y-%m-%dT%H:%M:%SZ")


def build_seat_key(lane: str, symbol: str, dt: Optional[datetime] = None) -> str:
    """Canonical seat key from lane + symbol + optional timestamp.

    Raises ValueError on unknown lane or empty symbol — arbitration
    downstream MUST see a valid seat_key or refuse to arbitrate.
    """
    lane_lc = (lane or "").strip().lower()
    if lane_lc not in LANES:
        raise ValueError(f"lane must be one of {sorted(LANES)}, got {lane!r}")
    sym = (symbol or "").strip().upper()
    if not sym:
        raise ValueError("symbol is required")
    return f"{lane_lc}:{sym}:{bucket_iso(dt)}"


def parse_seat_key(key: str) -> tuple[str, str, str]:
    """Return `(lane, symbol, bucket_iso)` from a canonical seat key.

    Uses `rsplit` on the last two colons so a symbol like `ETH/USD`
    (which itself contains no colons — but might in future) survives
    unambiguous parsing without a schema change.
    """
    if not key or key.count(":") < 2:
        raise ValueError(f"malformed seat_key: {key!r}")
    # bucket_iso contains two colons (HH:MM:SS). So split from left:
    # take the first two colon-separated fields, everything after is
    # the bucket iso.
    lane, remainder = key.split(":", 1)
    # remainder looks like "SYMBOL:2026-07-11T14:30:00Z"
    # symbol is up to the first colon that comes before the ISO date.
    symbol, bucket = remainder.split(":", 1)
    return lane, symbol, bucket


def next_bucket_iso(key_or_iso: str, minutes: int = BUCKET_MINUTES) -> str:
    """Advance a seat's bucket by one interval. Used by the grader
    to compute the next arbitration boundary.

    Accepts either a full `seat_key` (`equity:NVDA:...Z`) or a bare
    bucket ISO (`2026-07-11T14:30:00Z`). Disambiguates by prefix —
    a seat_key starts with a known lane token.
    """
    starts_with_lane = any(
        key_or_iso.startswith(f"{lane}:") for lane in LANES
    )
    if starts_with_lane:
        _, _, iso = parse_seat_key(key_or_iso)
    else:
        iso = key_or_iso
    dt = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return bucket_iso(dt + timedelta(minutes=minutes), minutes=minutes)

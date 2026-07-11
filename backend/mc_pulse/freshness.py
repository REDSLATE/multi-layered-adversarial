"""Snapshot freshness contract.

2026-07 iter-27 doctrine (operator directive):

    > No fresh market event → no brain opinion.

The stale-feeder incident on 2026-07-11 (472 identical Camino/NVDA
`BUY conf=0.75` intents over 6h from a 20-hour-old bar) proved
that a starved brain will still emit convictions. That's an
initiating-fault + compound-failure pattern — the feeder started
it, but the system compounded it. The compounding stops here.

Contract:
    Every `MarketSnapshot` carries a `SnapshotHealth`. Any status
    other than `fresh` MUST prevent brain evaluation entirely —
    the brain doesn't get a shot. This distinguishes:
        * "brain looked at fresh data and chose HOLD" (a real
          opinion; goes through consensus)
        * "we had no fresh market event to look at" (operational
          abstention; skipped, never touches consensus /
          personality stats / execution metrics)

Session awareness:
    A 20-hour-old NVDA bar is unequivocally stale during RTH but
    unavoidable at 3am Saturday. Freshness is evaluated against
    the most-recent EXPECTED completed session close — for
    equities, that's the last NYSE 5m bar boundary during RTH
    plus a small settlement grace period. Crypto is 24/7 so any
    bar older than `MAX_BAR_AGE_SECONDS[tf]` is stale.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from typing import Literal, Optional

# Tolerances chosen to allow normal feeder delay but not
# yesterday's bar. Matches operator directive:
#   1m  → 3 minutes
#   5m  → 15 minutes
#   15m → 45 minutes
#   1h  → 2.5 hours
#   1d  → 40 hours (allows Fri-close → Mon-open weekend)
MAX_BAR_AGE_SECONDS: dict[str, float] = {
    "1m": 180,
    "5m": 900,
    "15m": 2700,
    "1h": 9000,
    "1d": 144000,
}

# NYSE regular trading hours in UTC. DST-naive on purpose:
# during US DST (Mar–Nov), 13:30–20:00 UTC is 09:30–16:00 ET;
# outside DST it's 14:30–21:00 UTC. We use the wider superset
# (13:30–21:00) so we NEVER falsely mark a real-session bar as
# stale — a bar minted 15 min before close during standard time
# must still evaluate correctly. Weekends handled explicitly.
_NYSE_OPEN_UTC = time(13, 30, tzinfo=timezone.utc)
_NYSE_CLOSE_UTC = time(21, 0, tzinfo=timezone.utc)
# Grace period AFTER close during which the previous session's
# final bar is still considered fresh. Covers feeder settlement
# and post-close reconciliation.
_POST_CLOSE_GRACE_SECONDS = 15 * 60


@dataclass(frozen=True)
class SnapshotHealth:
    """Freshness verdict on a symbol's most recent bar.

    Attached to every `MarketSnapshot`. Brains never see this;
    the pulse orchestrator gates on it BEFORE calling
    `brain.evaluate` — a stale snapshot is skipped entirely,
    producing NO opinion (not even a HOLD).
    """
    status: Literal["fresh", "stale", "missing", "invalid"]
    latest_bar_at: Optional[datetime]
    age_seconds: Optional[float]
    max_age_seconds: float
    reason_codes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_fresh(self) -> bool:
        return self.status == "fresh"


def _is_equity_rth(now: datetime) -> bool:
    """Is `now` inside NYSE regular trading hours? Uses the wider
    DST-superset so we never falsely mark a real-session bar as
    stale. Weekends are always False."""
    now_utc = now.astimezone(timezone.utc)
    if now_utc.weekday() >= 5:      # Sat / Sun
        return False
    t = now_utc.timetz()
    return _NYSE_OPEN_UTC <= t < _NYSE_CLOSE_UTC


def evaluate_snapshot_health(
    *,
    lane: str,
    tf: str,
    latest_bar_at: Optional[datetime],
    now: Optional[datetime] = None,
) -> SnapshotHealth:
    """Return a `SnapshotHealth` for a symbol given the latest
    bar's timestamp.

    Session-aware for equities: if we're outside RTH+grace, the
    "latest expected bar" is Friday's/last-session's final 5m
    close, so an old-but-most-recent bar is FRESH. During RTH,
    same-day 5m staleness applies.

    Crypto is 24/7 — a flat `MAX_BAR_AGE_SECONDS[tf]` window is
    all we need.
    """
    now = now or datetime.now(timezone.utc)
    if latest_bar_at is None:
        return SnapshotHealth(
            status="missing",
            latest_bar_at=None,
            age_seconds=None,
            max_age_seconds=MAX_BAR_AGE_SECONDS.get(tf, 900),
            reason_codes=("NO_BAR_AVAILABLE",),
        )
    # Ensure UTC-aware.
    lb = latest_bar_at
    if lb.tzinfo is None:
        lb = lb.replace(tzinfo=timezone.utc)
    else:
        lb = lb.astimezone(timezone.utc)
    age = (now - lb).total_seconds()
    if age < 0:
        return SnapshotHealth(
            status="invalid",
            latest_bar_at=lb,
            age_seconds=age,
            max_age_seconds=MAX_BAR_AGE_SECONDS.get(tf, 900),
            reason_codes=("BAR_FROM_FUTURE",),
        )
    max_age = MAX_BAR_AGE_SECONDS.get(tf, 900)
    if lane == "equity":
        if not _is_equity_rth(now):
            # Outside RTH — the "latest expected bar" is the last
            # session's final bar. As long as we have SOME bar
            # from within the last ~4 days (covers a Fri-close →
            # Tue-morning holiday gap), we're fresh. Feeders may
            # legitimately have nothing newer.
            weekend_max_age = 4 * 86400
            if age <= weekend_max_age:
                return SnapshotHealth(
                    status="fresh",
                    latest_bar_at=lb,
                    age_seconds=age,
                    max_age_seconds=weekend_max_age,
                    reason_codes=("OUTSIDE_RTH_LAST_SESSION_BAR",),
                )
            return SnapshotHealth(
                status="stale",
                latest_bar_at=lb,
                age_seconds=age,
                max_age_seconds=weekend_max_age,
                reason_codes=("STALE_BEYOND_LAST_SESSION",),
            )
        # During RTH: apply the tf freshness cap.
        # Small grace so a bar minted 30s ago still counts as
        # fresh for tf=1m (allows normal feeder latency).
        if age <= max_age + _POST_CLOSE_GRACE_SECONDS:
            return SnapshotHealth(
                status="fresh",
                latest_bar_at=lb,
                age_seconds=age,
                max_age_seconds=max_age,
            )
        return SnapshotHealth(
            status="stale",
            latest_bar_at=lb,
            age_seconds=age,
            max_age_seconds=max_age,
            reason_codes=("STALE_MARKET_DATA",),
        )
    # Crypto — 24/7. Flat cap.
    if age <= max_age:
        return SnapshotHealth(
            status="fresh",
            latest_bar_at=lb,
            age_seconds=age,
            max_age_seconds=max_age,
        )
    return SnapshotHealth(
        status="stale",
        latest_bar_at=lb,
        age_seconds=age,
        max_age_seconds=max_age,
        reason_codes=("STALE_MARKET_DATA",),
    )

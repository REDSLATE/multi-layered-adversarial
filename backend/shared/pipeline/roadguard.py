"""RoadGuard — binary safety stops AFTER the seat decides ALLOW.

Doctrine: RoadGuard is the LAST internal layer that can refuse the
order. It is BINARY — passed or not — and the reason is a one-line
canonical string.

Stops checked, in order:
  trading_controls_disabled   — operator-flipped Mongo kill switch
                                (highest precedence; fail-CLOSED)
  zero_notional               — sizing collapsed to $0
  market_closed               — equity lane outside RTH (crypto skips)
  insufficient_buying_power   — broker BP < final_notional
  duplicate_order             — same (brain, lane, symbol, side) in flight
"""
from __future__ import annotations

from typing import Any

from .models import BrainOpinion, RoadGuardVerdict


class RoadGuard:
    """Stateless. Reads opinion.evidence + the live arguments."""

    async def check(
        self,
        opinion: BrainOpinion,
        notional_usd: float,
    ) -> RoadGuardVerdict:
        # Operator kill switch (2026-06-18, ported from the legacy
        # auto-router chain before its deletion). Reads a Mongo
        # singleton flipped via /api/admin/trading/enable|disable.
        # Highest precedence so an operator halt beats every other
        # safety check; fail-CLOSED so a Mongo blip refuses orders.
        from routes.trading_controls import is_trading_enabled  # noqa: WPS433
        if not await is_trading_enabled():
            return RoadGuardVerdict(False, "trading_controls_disabled")

        if notional_usd <= 0:
            return RoadGuardVerdict(False, "zero_notional")

        # Market-hours check — equity only. Crypto is 24/7.
        if opinion.lane == "equity":
            # Operator may have flipped on the extended-hours override
            # via the Intents page (Mongo flag, no redeploy needed).
            # When ON, RoadGuard accepts equity intents during Webull's
            # full 4 AM – 8 PM ET window M-F (still excludes weekends
            # and market holidays).
            from routes.equity_extended_hours_admin import (  # noqa: WPS433
                get_equity_extended_hours_enabled,
            )
            from shared.market_hours import is_equity_extended_hours  # noqa: WPS433
            extended = await get_equity_extended_hours_enabled()
            market_open = self._is_market_open(opinion.evidence)
            if extended:
                if not is_equity_extended_hours():
                    return RoadGuardVerdict(False, "market_closed_extended_hours_window")
            elif market_open is False:  # explicit False, not "missing"
                return RoadGuardVerdict(False, "market_closed")

            # 2026-02-20: Webull CORE-session MARKET-order close buffer.
            # Webull rejects MARKET orders submitted within the final
            # N seconds before regular close with HTTP 417 "The time
            # you sent is not supported." Block these UPSTREAM so the
            # post-mortem sees a clean RoadGuard verdict instead of
            # 337 submit_raised broker rejects.
            #
            # Doctrine pin (operator 2026-02-20):
            #     "Better to miss one late $1 trade than throw
            #      hundreds of broker rejects."
            # Default 90s — configurable via env. Extended-hours mode
            # skips the buffer because LIMIT orders submitted outside
            # CORE don't trip this clock-check.
            if not extended and self._within_webull_core_close_buffer():
                return RoadGuardVerdict(
                    False, "WEBULL_CORE_MARKET_ORDER_CLOSE_BUFFER",
                )

        bp = opinion.evidence.get("buying_power")
        if bp is not None:
            try:
                if float(bp) < notional_usd:
                    return RoadGuardVerdict(False, "insufficient_buying_power")
            except (TypeError, ValueError):
                pass  # ignore unparseable evidence; do not block on it.

        if bool(opinion.evidence.get("duplicate_order")):
            return RoadGuardVerdict(False, "duplicate_order")

        return RoadGuardVerdict(True, "roadguard_passed")

    @staticmethod
    def _is_market_open(evidence: dict[str, Any]) -> bool | None:
        """Returns:
          True   — evidence explicitly says market is open
          False  — evidence explicitly says market is closed
          None   — no opinion; RoadGuard falls back to the local check
        """
        explicit = evidence.get("market_open")
        if explicit is True:
            return True
        if explicit is False:
            return False
        # Fall back to the canonical RTH check.
        try:
            from shared.market_hours import is_equity_rth
            return is_equity_rth()
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _within_webull_core_close_buffer(now=None) -> bool:
        """True if the wall clock is within
        `WEBULL_CLOSE_BUFFER_SECONDS` (default 90) of the regular
        equity close. Outside RTH → False (the prior `market_closed`
        check already covered that).

        Tunable via env so the operator can dial wider (120s when
        Webull's clock-check gets racier) or narrower (60s for tight
        markets) without a redeploy of this module.
        """
        import os
        from datetime import datetime, timedelta, timezone
        try:
            buf = int(os.environ.get("WEBULL_CLOSE_BUFFER_SECONDS", "90"))
        except (TypeError, ValueError):
            buf = 90
        if buf <= 0:
            return False
        now = now or datetime.now(timezone.utc)
        try:
            from shared.market_hours import is_equity_rth
        except Exception:  # noqa: BLE001
            return False
        # If we're not in RTH at all, the close-buffer concept doesn't
        # apply (other checks block us). Returning False keeps the
        # downstream `market_closed` verdict intact.
        if not is_equity_rth(now):
            return False
        # If we're in RTH NOW but won't be `buf` seconds from now,
        # we're inside the close buffer.
        return not is_equity_rth(now + timedelta(seconds=buf))

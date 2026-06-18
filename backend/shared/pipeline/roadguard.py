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
            market_open = self._is_market_open(opinion.evidence)
            if market_open is False:  # explicit False, not "missing"
                return RoadGuardVerdict(False, "market_closed")

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

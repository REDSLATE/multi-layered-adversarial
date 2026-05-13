"""Broker adapter package.

Doctrine:
  * MC owns every broker connection. No brain ever holds broker keys.
  * Adapters expose ONE interface (`BrokerAdapter`), so the gate chain
    and execution router don't care which broker is wired in.
  * Defaults to a paper-trading adapter. Live adapters land behind a
    separate `LIVE_TRADING_ENABLED` flag + dual-sign promotion.
"""
from shared.broker.base import BrokerAdapter, BrokerOrder, BrokerPosition, BrokerAccount

__all__ = ["BrokerAdapter", "BrokerOrder", "BrokerPosition", "BrokerAccount"]

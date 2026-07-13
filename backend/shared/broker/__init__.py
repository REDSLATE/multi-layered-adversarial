"""Broker adapter package.

Doctrine (2026-02-19 operator-locked):
  * MC owns every broker connection. No brain ever holds broker keys.
  * Adapters expose ONE interface (`BrokerAdapter`), so the gate chain
    and execution router don't care which broker is wired in.
  * LIVE TRADES ONLY. No paper adapter, no shadow adapter. Every
    broker in this package (Webull for equity, Kraken for crypto)
    places real orders on real money accounts. `is_paper=False` on
    every adapter is a static guarantee, not a flag.
"""
from shared.broker.base import BrokerAdapter, BrokerOrder, BrokerPosition, BrokerAccount

__all__ = ["BrokerAdapter", "BrokerOrder", "BrokerPosition", "BrokerAccount"]

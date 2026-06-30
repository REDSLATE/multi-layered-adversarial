"""RISEDUAL Trader — sidecar (Path 2, 2026-06-30).

Doctrine pin:
    MC = eyes only (AUTO_ROUTER_ENABLED=false, BROKER_DISABLED=true)
    Trader = authority (this process)

Architecture (the one the operator drew):

    Market Data → Brain/Signal → Risk cap → Broker → executions

That's it. Five steps. No gate maze, no council, no dry-run, no
unified pipeline, no auto-submit policy. The trader runs a single
synchronous-style asyncio loop. Each cycle:

    1. fetch live market data (Kraken for crypto, Yahoo for equity)
    2. run the brain(s) — 4 personalities, scoped by which seat
       currently holds 'strategist' / 'executor' for the lane
    3. risk check — per-order cap + daily cap + freeze + idempotency
    4. broker — Kraken or Webull adapter, one market order
    5. write `executions` + `trader_receipts` to Mongo so MC can
       display the truth without having authority

Storage in MC's existing Mongo (same MONGO_URL):
    executions       — single audit row per (intent → broker) attempt
    trader_receipts  — per-cycle log: signal + brain + risk + result
    seat_registry    — READ by trader to honor operator's seat doctrine
    runtime_flags    — READ: master_trading_switch, lane_enabled

The trader does NOT touch `shared_intents`, `pipeline_receipts`,
`shared_gate_results`, or any of the MC legacy collections. Its
truth lives in `executions` + `trader_receipts`.
"""
__all__ = ["main"]

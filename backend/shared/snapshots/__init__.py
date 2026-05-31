"""Daily market snapshot subsystem.

Captures three snapshots of the full S&P-500 equity universe each
trading day (09:35 / 12:30 / 16:05 ET) and persists them so brains
can retrieve a frozen, point-in-time view of the market on demand.

Doctrine:
  DERIVED EVIDENCE ONLY. The snapshot worker pulls last-bar data
  from `shared_ohlcv_bars` (federated bar store). It NEVER hits a
  broker quote endpoint and NEVER returns broker keys. The retrieval
  API is dual-auth (operator JWT OR brain `X-Runtime-Token`) and
  read-only.

Lifetime:
  Snapshots persist for `DAILY_SNAPSHOT_RETENTION_TRADING_DAYS`
  (default 5 NYSE trading days) so brains can compare today's open
  to last week's. The wipe pass runs at the start of every new
  trading day (the `open` snapshot's first action).
"""

"""Data Stack Phase 1 (2026-05-27) — market-data feeders subpackage.

Doctrine pin: Feeders carry EVIDENCE only. No feeder may modify
execution authority. The OHLCV ingest schema rejects any `may_execute`
field at the route layer; this subpackage keeps the same boundary —
all writes go through the existing `/api/ingest/ohlcv*` endpoints.
"""

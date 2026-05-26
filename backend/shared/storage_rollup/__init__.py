"""Storage rollup — 60d compaction with movement+event labels.

Doctrine (2026-05-26, operator-locked):
    Past `ROLLUP_WINDOW_DAYS`, MC compresses verbose telemetry into
    a slim rollup row carrying movement (long/short/flat/blocked/
    rejected) + event (executed_win/blocked_<gate>/...) labels.
    Original verbose body lives `ROLLUP_DELETE_HOLD_DAYS` longer
    (reversible window), then purged.

    Never compresses Shellys, brain_memories, quarantine labels, or
    executed real-money trades. See `config.PROTECTED_*`.
"""

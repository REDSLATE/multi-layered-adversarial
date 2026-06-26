"""Barracuda native runtime — doctrine code (mean-reversion, equity).

This package is the IN-PROCESS replacement for the external Barracuda
sidecar. Plumbing (scheduler, emit path, seat policy, broker adapters)
lives in MC; only Barracuda's doctrinal interpretation belongs here.

Layout:
    strategy.py — pure compute: indicators → decision
    runner.py   — single-tick: universe + snapshots → submit_intent_in_process

Wiring:
    `shared/runtime/barracuda_runtime.py` runs `runner.tick_once(db)` on
    an asyncio loop. Started from `server_modules/lifespan.py` and
    flag-gated by `BARRACUDA_NATIVE_RUNTIME_ENABLED` (default False).
"""

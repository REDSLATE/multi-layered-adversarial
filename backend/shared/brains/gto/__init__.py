"""GTO native runtime — doctrine code (momentum, adversarial threat hunter).

Mirrors `shared/brains/barracuda/` exactly so the operator can read
one and know the other. Only the doctrinal interpretation differs.

Layout:
    strategy.py — pure compute: indicators → decision (momentum)
    runner.py   — single-tick: universe + snapshots → submit_intent_in_process

Wiring lives in `shared/runtime/gto_runtime.py`.
Flag-gated by `GTO_NATIVE_RUNTIME_ENABLED` (default False).
"""

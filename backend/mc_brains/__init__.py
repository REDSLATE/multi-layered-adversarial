"""MC-native brain implementations.

Each file here is one brain, implementing the `Brain` protocol
from `mc_pulse.protocols`. Brains own interpretation ONLY —
orchestration, snapshot construction, arbitration, persistence,
and execution routing are MC's responsibilities.

Layout (post-P7, 2026-07-12):

  mc_brains/
    __init__.py          — this docstring
    _pulse_base.py       — shared orchestration (should_evaluate,
                           personality clamp, ModelOpinion wrap,
                           manifest hint) — no per-brain logic
    personality.py       — per-brain confidence multipliers
                           (alpha/redeye/camaro/chevelle)
    camino.py            — trend-follower brain (P7a strategy)
    gto.py               — momentum-confirmation brain
    barracuda.py         — mean-reversion brain
    hellcat.py           — execution-safety brain
    strategies/
      __init__.py        — `Strategy` protocol + StrategyResult
      trend_following.py       — Camino's strategy
      momentum_confirmation.py — GTO's strategy
      mean_reversion.py        — Barracuda's strategy
      execution_safety.py      — Hellcat's strategy

Legacy dead: `/app/external/brains/` (runners) and
`mc_brains/_legacy/` (NeutralAdversarialBrain wrapper) were
deleted in P3 + P7d respectively. The current stack is 100%
pulse + strategy — no more shared cognitive core.
"""

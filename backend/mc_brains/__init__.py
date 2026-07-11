"""MC-native brain implementations.

Each file here is one brain, implementing the `Brain` protocol
from `mc_pulse.protocols`. Brains own interpretation ONLY —
orchestration, snapshot construction, arbitration, persistence,
and execution routing are MC's responsibilities.

Migration doctrine: brains defined here run in-process off the
MC pulse. Runner processes in `/app/external/brains/` are being
retired (see `/app/memory/MC_PULSE.md` §10 + per-brain audit docs
in `/app/memory/*_RUNNER_AUDIT.md`).
"""

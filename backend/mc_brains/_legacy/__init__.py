"""Legacy artefacts kept around because the pulse brains still use them.

`brain_core.py` — `NeutralAdversarialBrain` strategy core. Wrapped
by every subclass in `mc_brains/_pulse_base.py`. When the four
brains eventually diverge into distinct strategies (currently
personality is a confidence-multiplier only), each brain's own
strategy module will replace this file.

`personality.py` — `BRAIN_PERSONALITIES` + `apply_personality_confidence`.
Referenced by `_pulse_base.py` for the personality-clamp step of
every evaluation.

Relocated 2026-07-12 (iter-28e, P3 step 3) from `/app/external/brains/`
before `rm -rf`. The pulse brains cannot be sole owners of the
strategy math AND depend on it via an external path; the
relocation makes the dependency in-repo.

Do NOT add new files here. If a pulse brain needs new behavior,
extend `_pulse_base.py` or add a new module under `mc_brains/`.
The `_legacy` namespace is a graveyard, not a growth area.
"""

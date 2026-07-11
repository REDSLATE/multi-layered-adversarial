"""MC Seat Arbiter — Phase 1 scaffold.

This module is the SINGLE decision point that turns brain-level
opinions into ONE broker-bound intent per seat. It replaces the
per-brain sizing math + duplicate gates that lived across the four
brains.

Design freeze: `/app/memory/MC_SEAT_ARBITER.md`

Runtime modes:
    - DISARMED (default): opinions collected, arbitration runs,
      DAWE grades update, NO intent emitted to trader.
    - LIVE: intent IS emitted. Kill switch + mechanical validators
      still gate the emission.

No PAPER. No SHADOW. No "would-have-picked" side channel.
"""

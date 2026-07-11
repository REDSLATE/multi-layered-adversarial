"""MC Pulse — one loop, one snapshot, four brains, one heartbeat.

Design freeze: `/app/memory/MC_PULSE.md`.
Companion module: `/app/backend/mc_arbiter/` (arbitration, DAWE, grading).

End-state architecture (runner-free):

    Mission Control
      └── one pulse loop
            ├── one market snapshot
            ├── Camino.evaluate(...) · GTO · Barracuda · Hellcat
            ├── arbiter / seat routing
            └── persistence + pulse heartbeat

Migration is safety-only. The destination is unambiguously runner-free.
"""

"""Paradox v2 — seat-owned execution doctrine.

Doctrine (locked 2026-02-19, see /app/memory/PRD.md):

    Brain owns doctrine. Seat owns execution. Governor owns modifiers.
    RoadGuard owns binary stops. Verifier owns promotion.

Each module here owns exactly one layer. The IP boundary between layers
is enforced by import discipline (see models.py docstrings) and by the
pipeline orchestration in evaluator.py. No layer crosses the boundary.

Stand-alone deployment (2026-02-19): this module is intentionally NOT
wired into the live intent flow. Operator drives it via /api/v2/evaluate
until the seat-policy concept is validated against ≥50 manual test
intents. The existing auto_submit_policy chain continues to handle live
trading in parallel.
"""
from __future__ import annotations

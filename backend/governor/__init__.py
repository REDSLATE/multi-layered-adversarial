"""Governor-layer code (Paradox v2).

Layer doctrine: governor owns structured modifiers. It NEVER blocks
(that's RoadGuard's job). It NEVER vetoes (that's the auditor's job).
It outputs size adjustments and a vote_required flag. See
backend/shared/paradox_v2/models.py::GovernorModifierRule.
"""

"""Intent Firewall — security namespace.

2026-06-22 — Skeleton for the v3 Intent Firewall doctrine. The
firewall itself (`intent_firewall.py`) will be implemented in a
dedicated session; this module currently houses ONLY the
context-aware pattern matcher, which addresses the false-positive
risk identified in the v3 spec review.

See: `firewall_patterns.py` for the pattern engine, and
`/app/memory/PRD.md` for the doctrine that governs it.
"""

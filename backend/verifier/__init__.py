"""Verifier-layer code (Paradox v2).

Layer doctrine: the verifier reads facts (votes, governor outputs,
fills, P&L) and writes audit conclusions (failure attribution, brain
calibration updates, autonomy promotion candidates). It NEVER decides
on a live trade. It runs out-of-band, never in the hot path.
"""

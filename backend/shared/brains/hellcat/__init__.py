"""Hellcat native runtime — doctrine code (breakout, risk governor).

Mirrors `shared/brains/barracuda/`. Breakout: BUY on confirmed upper-
band break + RSI confirmation, SHORT on confirmed lower-band break
(env-gated). Highest doctrine confidence floor (0.48) per
`DOCTRINES["hellcat"]` — Hellcat is the cautious "final agreement"
voice.
"""

"""Cross-cutting risk guards.

This subpackage is lane-neutral by design. Each module here is pure
deterministic math (no LLM, no DB, no async) so it can be reasoned
about, unit-tested, and applied identically to equity and crypto
positions.

Lane-specific *wiring* lives in `shared/equity/` and `shared/crypto/`
per the lane-isolation doctrine. This package is the one place where
shared safety logic legitimately sits between them.
"""

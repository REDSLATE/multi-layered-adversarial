"""strategy_id field on emitted intents — step 1/5 of evidence-layer prep.

Doctrine pin (2026-07-04): every persisted intent must carry a
`strategy_id` field so the future `strategy_evidence` collection can
join against it without a schema migration. Derivation rules:

  1. Explicit `body.strategy_id` wins if provided.
  2. Else `f"{evidence.doctrine}_v1"` if `evidence.doctrine` is set.
  3. Else `"unknown_strategy"` (never None, never missing).

These tests exercise the model-level default and the derivation logic
on the doc-build path used in `_post_intent_impl`.
"""
from __future__ import annotations

import sys

import pytest

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/backend")


def test_intentin_model_accepts_strategy_id():
    """The IntentIn model must accept an explicit strategy_id."""
    from shared.intents import IntentIn
    body = IntentIn(
        stack="barracuda", action="BUY", symbol="NVDA", lane="equity",
        confidence=0.65, rationale="test",
        strategy_id="large_cap_momentum_v1",
    )
    assert body.strategy_id == "large_cap_momentum_v1"


def test_intentin_model_defaults_strategy_id_to_none():
    """When brain doesn't supply, model default is None — derivation
    happens in _post_intent_impl, not in the model."""
    from shared.intents import IntentIn
    body = IntentIn(
        stack="barracuda", action="BUY", symbol="NVDA", lane="equity",
        confidence=0.65, rationale="test",
    )
    assert body.strategy_id is None


def test_intentin_model_rejects_oversized_strategy_id():
    """Bounded field. Pydantic rejects strings > 64 chars."""
    from shared.intents import IntentIn
    with pytest.raises(Exception):  # ValidationError from pydantic
        IntentIn(
            stack="barracuda", action="BUY", symbol="NVDA", lane="equity",
            confidence=0.65, rationale="test",
            strategy_id="x" * 65,
        )


# --- Derivation logic tests (pure — no DB, no HTTP, no MC gates) ---

def _derive(explicit: str | None, evidence: dict) -> str:
    """Mirror of the derivation logic in _post_intent_impl. Kept as a
    standalone helper so the doctrine can be tested without spinning
    up the full HTTP + Mongo stack. If this test's logic diverges
    from the real code, that's a bug in one of the two places."""
    return (
        explicit
        or (
            f"{evidence.get('doctrine')}_v1"
            if isinstance(evidence, dict) and evidence.get("doctrine")
            else "unknown_strategy"
        )
    )


def test_derive_explicit_wins_over_doctrine():
    """Brain-provided value takes precedence over evidence.doctrine."""
    result = _derive("explicit_strategy_v3", {"doctrine": "some_other"})
    assert result == "explicit_strategy_v3"


def test_derive_from_evidence_doctrine():
    """Missing explicit → derive from evidence.doctrine + _v1."""
    result = _derive(None, {"doctrine": "large_cap_momentum"})
    assert result == "large_cap_momentum_v1"


def test_derive_falls_back_to_unknown_strategy():
    """Missing explicit AND missing evidence.doctrine → sentinel value.
    Never None, never absent — the field must always be a string so
    the future strategy_evidence join always has a key."""
    result = _derive(None, {})
    assert result == "unknown_strategy"


def test_derive_handles_none_evidence():
    """Evidence isn't always a dict in the wild (some legacy paths).
    Must not raise."""
    result = _derive(None, None)  # type: ignore[arg-type]
    assert result == "unknown_strategy"


def test_derive_handles_evidence_with_none_doctrine():
    """evidence={'doctrine': None} → fall back to unknown_strategy,
    not 'None_v1'."""
    result = _derive(None, {"doctrine": None})
    assert result == "unknown_strategy"


def test_derive_preserves_versioned_doctrine_names():
    """Some doctrines already include a version suffix in their name
    (e.g. 'gap_and_go_v2'). Suffixing with `_v1` produces double-
    versioning — that's intended for now (`gap_and_go_v2_v1`) so the
    strategy_id remains lossless and reversible to the source doctrine.
    Downstream evidence-worker will decide how to bucket variants."""
    result = _derive(None, {"doctrine": "gap_and_go_v2"})
    assert result == "gap_and_go_v2_v1"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

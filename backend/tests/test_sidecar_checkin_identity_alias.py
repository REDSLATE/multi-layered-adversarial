"""Regression test for the {stamp:...} ↔ {identity:...} body alias.

Operator pin (2026-05-31): brains may POST sidecar-checkin with EITHER
top-level body key. Chevelle / RedEye were silently 422'd before this
alias landed because they adopted the v1 spec name (`identity`) before
MC's POST endpoint accepted it.
"""
from __future__ import annotations

from shared.runtime.sidecar_checkin import CheckinRequest


def test_stamp_key_accepted_legacy_shape():
    """Original / legacy clients sending `{"stamp": {...}}` still work."""
    req = CheckinRequest.model_validate(
        {"stamp": {"app_name": "alpha", "env_name": "prod"}},
    )
    assert req.stamp == {"app_name": "alpha", "env_name": "prod"}


def test_identity_key_accepted_v1_shape():
    """v1 sidecars sending `{"identity": {...}}` resolve into the same
    `stamp` attribute MC's validator already consumes."""
    req = CheckinRequest.model_validate(
        {"identity": {"app_name": "chevelle", "env_name": "prod"}},
    )
    assert req.stamp == {"app_name": "chevelle", "env_name": "prod"}


def test_empty_body_raises_validation_error():
    """Neither key present → Pydantic 422 ('field required' since the
    stamp field has no default)."""
    import pytest
    from pydantic import ValidationError
    with pytest.raises(ValidationError) as e:
        CheckinRequest.model_validate({})
    msg = str(e.value)
    # Pydantic mentions the canonical field name (stamp) in the error.
    assert "stamp" in msg or "Field required" in msg


def test_body_with_unrelated_key_raises_validation_error():
    """A sidecar that posts the wrong field name should get a 422 —
    not a confusing 'missing 12 required fields' from RuntimeStamp's
    downstream validator."""
    import pytest
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        CheckinRequest.model_validate({"runtime_stamp": {"app_name": "x"}})

"""Tripwire tests for forward-compatible RuntimeStamp validation.

Doctrine pin (2026-02-18):
    Brain sidecars may add new optional fields to their RuntimeStamp
    BEFORE MC's dataclass learns about them. Alpha's `pip_fingerprint`
    rollout is the trigger: it shipped first to the brain pods and
    flipped Alpha + Camaro to verdict=INVALID against the old MC.

    MC MUST tolerate unknown keys: filter to known fields before
    constructing the dataclass, validate the typed object, and persist
    the FULL raw stamp (including the unknown extras) so the data is
    preserved without forcing a lockstep redeploy.

    The wrong fix is to add the new field to MC's dataclass — that
    couples brain rollouts to MC rollouts. The doctrinal fix is
    forward-compat tolerance.
"""
from __future__ import annotations

import pytest

from shared.runtime.sidecar_checkin import _validate_stamp_dict


# ─── Forward-compat: unknown keys must NOT flip verdict to INVALID ───


@pytest.mark.tripwire
def test_validate_stamp_tolerates_unknown_keys():
    """The exact prod failure from 2026-02-18: Alpha posted a stamp
    containing `pip_fingerprint` (a forward-compat field). MC's old
    dataclass crashed with `TypeError: __init__() got an unexpected
    keyword argument 'pip_fingerprint'` and flipped verdict to
    INVALID. After the fix, this MUST pass."""
    stamp = {
        "app_name": "risedual",
        "env_name": "prod",
        "git_sha": "alpha-r1",
        "platform": "emergent",
        "mc_url": "https://mission.risedual.ai",
        "db_name": "risedual_db",
        "broker_mode": "paper",
        "sidecar_room": "alpha",
        "sidecar_version": "1.0.0",
        "policy_hash": "abc123",
        "local_execution_authority": False,
        "timestamp_ms": 1_700_000_000_000,
        # NEW FIELD from Alpha's pip_fingerprint rollout — MC must
        # tolerate it instead of crashing.
        "pip_fingerprint": {
            "pip_freeze_sha256": "cf4c3e7f99ca8fa1abc",
            "package_count": 231,
            "sample": ["anthropic==0.95.0", "fastapi==0.110.1"],
        },
    }
    result = _validate_stamp_dict(stamp)
    # The known-field validation must succeed. STAMP_SHAPE_INVALID
    # must NOT appear in the errors list.
    err_str = " ".join(result.get("errors", []))
    assert "STAMP_SHAPE_INVALID" not in err_str, (
        f"forward-compat field crashed validation: {err_str!r}"
    )
    assert "pip_fingerprint" not in err_str
    # The validator should treat the prod-shaped stamp as ok.
    assert result["ok"] is True, (
        f"valid prod stamp + unknown field should validate ok; got {result!r}"
    )


@pytest.mark.tripwire
def test_validate_stamp_persists_unknown_fields_to_full_stamp():
    """Forward-compat data must SURVIVE. The persisted `stamp` echo
    must include the unknown fields so the operator UI can render
    `pip_fingerprint` (and future fields) without an MC redeploy."""
    stamp = {
        "app_name": "risedual",
        "env_name": "prod",
        "git_sha": "alpha-r1",
        "platform": "emergent",
        "mc_url": "https://mission.risedual.ai",
        "db_name": "risedual_db",
        "broker_mode": "paper",
        "sidecar_room": "alpha",
        "sidecar_version": "1.0.0",
        "policy_hash": "abc123",
        "local_execution_authority": False,
        "timestamp_ms": 1_700_000_000_000,
        "pip_fingerprint": {"pip_freeze_sha256": "cf4c3e7f99"},
        "future_field_x": "something_new",
    }
    result = _validate_stamp_dict(stamp)
    persisted = result.get("stamp") or {}
    assert "pip_fingerprint" in persisted, (
        "unknown field `pip_fingerprint` was dropped from the persisted stamp"
    )
    assert "future_field_x" in persisted, (
        "any future unknown field must round-trip through MC unchanged"
    )


@pytest.mark.tripwire
def test_validate_stamp_surfaces_unknown_keys_list():
    """The diagnostic surface must tell the operator which unknown
    keys arrived. This is the soft-signal: 'brain shipped a new field,
    MC tolerated it, here's the inventory.'"""
    stamp = {
        "app_name": "risedual",
        "env_name": "prod",
        "git_sha": "x",
        "platform": "emergent",
        "mc_url": "https://mission.risedual.ai",
        "db_name": "risedual_db",
        "broker_mode": "paper",
        "sidecar_room": "alpha",
        "sidecar_version": "1.0.0",
        "policy_hash": "abc",
        "local_execution_authority": False,
        "timestamp_ms": 1,
        "pip_fingerprint": {},
        "another_new_field": 42,
    }
    result = _validate_stamp_dict(stamp)
    unknown = result.get("unknown_keys", [])
    assert "pip_fingerprint" in unknown
    assert "another_new_field" in unknown


# ─── Hard-failure modes still hard-fail ─────────────────────────────


@pytest.mark.tripwire
def test_validate_stamp_still_rejects_missing_required_fields():
    """Forward-compat MUST NOT loosen REQUIRED-field validation. A
    stamp missing a known doctrine field still fails fast."""
    stamp = {
        # missing: app_name, git_sha, platform, etc.
        "env_name": "prod",
        "mc_url": "https://mission.risedual.ai",
    }
    result = _validate_stamp_dict(stamp)
    assert result["ok"] is False
    err_str = " ".join(result.get("errors", []))
    assert "STAMP_SHAPE_INVALID" in err_str, (
        f"missing required field MUST still fail; got {result!r}"
    )


@pytest.mark.tripwire
def test_validate_stamp_still_flags_wrong_env_name():
    """Forward-compat doesn't loosen `env_name` doctrine. A stamp
    that says `env_name=preview` must still flag ENV_NOT_PROD."""
    stamp = {
        "app_name": "risedual",
        "env_name": "preview",  # <- the failure under test
        "git_sha": "x",
        "platform": "emergent",
        "mc_url": "https://mission.risedual.ai",
        "db_name": "risedual_db",
        "broker_mode": "paper",
        "sidecar_room": "alpha",
        "sidecar_version": "1.0.0",
        "policy_hash": "abc",
        "local_execution_authority": False,
        "timestamp_ms": 1,
        "pip_fingerprint": {},  # tolerate the extra field
    }
    result = _validate_stamp_dict(stamp)
    assert "ENV_NOT_PROD" in result.get("errors", [])


@pytest.mark.tripwire
def test_validate_stamp_still_flags_wrong_mc_url():
    stamp = {
        "app_name": "risedual",
        "env_name": "prod",
        "git_sha": "x",
        "platform": "emergent",
        "mc_url": "https://preview.example.com",  # <- failure under test
        "db_name": "risedual_db",
        "broker_mode": "paper",
        "sidecar_room": "alpha",
        "sidecar_version": "1.0.0",
        "policy_hash": "abc",
        "local_execution_authority": False,
        "timestamp_ms": 1,
        "pip_fingerprint": {},
    }
    result = _validate_stamp_dict(stamp)
    assert "MC_URL_NOT_PROD" in result.get("errors", [])

"""Tests for the neutral-brain identity stamp.

Doctrine pin (2026-02-XX): the in-process brain's check-in stamp
MUST match MC's `validate_for_prod_sidecar` contract. The previous
implementation hard-coded `policy_hash="neutral-template"` and
defaulted `env_name="prod"` / `mc_url="https://mission.risedual.ai"`
which:
  * burned a HASH_MISMATCH on EVERY check-in (the literal string
    never matches MC's SHA256), AND
  * falsely advertised preview pods as prod when the env var was
    missing.

These tests lock in the fixed behavior: identity sourced from
canonical RISEDUAL_* env vars (with safe legacy fallbacks), policy
hash computed by the SAME function MC validates against, defaults
fail closed.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


from external.brains.runner import _checkin_stamp  # noqa: E402
from shared.runtime.platform_survival import policy_hash  # noqa: E402


def _reset_env(monkeypatch):
    """Strip every env var the stamp might pick up so each test
    asserts against a clean slate."""
    for key in (
        "RISEDUAL_ENV", "ENV", "BRAIN_ENV_NAME",
        "RISEDUAL_MC_URL", "BRAIN_ADVERTISED_MC_URL",
        "RISEDUAL_GIT_SHA", "GIT_SHA",
        "VERCEL_GIT_COMMIT_SHA", "RAILWAY_GIT_COMMIT_SHA",
        "BRAIN_GIT_SHA",
        "RISEDUAL_PLATFORM", "PLATFORM",
        "RISEDUAL_DB_NAME", "DB_NAME",
        "RISEDUAL_BROKER_MODE",
        "RISEDUAL_APP_NAME",
        "RISEDUAL_SIDECAR_VERSION", "BRAIN_SIDECAR_VERSION",
    ):
        monkeypatch.delenv(key, raising=False)


def test_policy_hash_matches_mc_canonical(monkeypatch):
    """The stamp's policy_hash MUST be the SAME SHA256 MC computes —
    no more 'neutral-template' literal that ALWAYS mismatches."""
    _reset_env(monkeypatch)
    payload = _checkin_stamp("alpha", "Camino")
    assert payload["stamp"]["policy_hash"] == policy_hash()


def test_env_name_sources_from_canonical_var(monkeypatch):
    _reset_env(monkeypatch)
    monkeypatch.setenv("RISEDUAL_ENV", "prod")
    assert _checkin_stamp("alpha", "Camino")["stamp"]["env_name"] == "prod"


def test_env_name_falls_back_to_generic_env(monkeypatch):
    """`ENV` is honored when `RISEDUAL_ENV` isn't set — supports
    deploys whose platform sets only the generic var."""
    _reset_env(monkeypatch)
    monkeypatch.setenv("ENV", "preview")
    assert _checkin_stamp("alpha", "Camino")["stamp"]["env_name"] == "preview"


def test_env_name_falls_back_to_legacy_brain_var(monkeypatch):
    """Backward compat: existing deploys set BRAIN_ENV_NAME."""
    _reset_env(monkeypatch)
    monkeypatch.setenv("BRAIN_ENV_NAME", "preview")
    assert _checkin_stamp("alpha", "Camino")["stamp"]["env_name"] == "preview"


def test_env_name_fails_closed_to_unknown(monkeypatch):
    """No env var = `unknown`, NEVER `prod`. The previous default
    falsely advertised every brain with a missing env var as prod —
    catastrophic for the prod-readiness gate."""
    _reset_env(monkeypatch)
    stamp = _checkin_stamp("alpha", "Camino")["stamp"]
    assert stamp["env_name"] == "unknown"
    assert stamp["env_name"] != "prod"


def test_mc_url_fails_closed_to_empty(monkeypatch):
    """No MC URL = empty string (which MC validates as
    MC_URL_NOT_PROD). The previous default 'https://mission.risedual.ai'
    falsely advertised preview check-ins as legitimate prod URLs."""
    _reset_env(monkeypatch)
    stamp = _checkin_stamp("alpha", "Camino")["stamp"]
    assert stamp["mc_url"] == ""


def test_mc_url_sources_from_canonical_first(monkeypatch):
    _reset_env(monkeypatch)
    monkeypatch.setenv("RISEDUAL_MC_URL", "https://mission.risedual.ai")
    monkeypatch.setenv(
        "BRAIN_ADVERTISED_MC_URL", "https://preview.example.com",
    )
    stamp = _checkin_stamp("alpha", "Camino")["stamp"]
    assert stamp["mc_url"] == "https://mission.risedual.ai"


def test_mc_url_legacy_fallback(monkeypatch):
    _reset_env(monkeypatch)
    monkeypatch.setenv("BRAIN_ADVERTISED_MC_URL", "https://preview.example.com")
    stamp = _checkin_stamp("alpha", "Camino")["stamp"]
    assert stamp["mc_url"] == "https://preview.example.com"


def test_db_name_sources_from_canonical_var(monkeypatch):
    _reset_env(monkeypatch)
    monkeypatch.setenv("RISEDUAL_DB_NAME", "risedual_prod")
    stamp = _checkin_stamp("alpha", "Camino")["stamp"]
    assert stamp["db_name"] == "risedual_prod"


def test_db_name_legacy_fallback(monkeypatch):
    _reset_env(monkeypatch)
    monkeypatch.setenv("DB_NAME", "multi-brain-backbone-test_database")
    stamp = _checkin_stamp("alpha", "Camino")["stamp"]
    assert stamp["db_name"] == "multi-brain-backbone-test_database"


def test_broker_mode_clamps_to_legal_values(monkeypatch):
    """Anything outside {paper, live, dry_run} silently clamps to
    `paper` so MC's BAD_BROKER_MODE gate never fires from a typo."""
    _reset_env(monkeypatch)
    monkeypatch.setenv("RISEDUAL_BROKER_MODE", "garbage")
    assert _checkin_stamp("alpha", "Camino")["stamp"]["broker_mode"] == "paper"
    monkeypatch.setenv("RISEDUAL_BROKER_MODE", "live")
    assert _checkin_stamp("alpha", "Camino")["stamp"]["broker_mode"] == "live"
    monkeypatch.setenv("RISEDUAL_BROKER_MODE", "dry_run")
    assert _checkin_stamp("alpha", "Camino")["stamp"]["broker_mode"] == "dry_run"


def test_full_prod_stamp_passes_validation(monkeypatch):
    """End-to-end: with the canonical RISEDUAL_* vars set the way a
    prod deploy should set them, the stamp passes MC's
    `validate_for_prod_sidecar` with zero errors."""
    from shared.runtime.platform_survival import RuntimeStamp

    _reset_env(monkeypatch)
    monkeypatch.setenv("RISEDUAL_ENV", "prod")
    monkeypatch.setenv("RISEDUAL_MC_URL", "https://mission.risedual.ai")
    monkeypatch.setenv("RISEDUAL_DB_NAME", "risedual_prod")
    monkeypatch.setenv("RISEDUAL_GIT_SHA", "abc1234")
    monkeypatch.setenv("RISEDUAL_BROKER_MODE", "paper")
    monkeypatch.setenv("RISEDUAL_PLATFORM", "emergent")
    monkeypatch.setenv("RISEDUAL_APP_NAME", "risedual")
    monkeypatch.setenv("RISEDUAL_SIDECAR_VERSION", "neutral-camino-v1")

    stamp_dict = _checkin_stamp("alpha", "Camino")["stamp"]
    # Drop the brain-only `display_name` field MC's RuntimeStamp
    # dataclass doesn't have (filtered at the validator boundary).
    filtered = {
        k: v for k, v in stamp_dict.items()
        if k in RuntimeStamp.__dataclass_fields__
    }
    stamp = RuntimeStamp(**filtered)
    result = stamp.validate_for_prod_sidecar()
    assert result["ok"] is True, f"validator errors: {result['errors']}"


def test_default_stamp_fails_validation(monkeypatch):
    """The fail-closed defaults must explicitly FAIL MC's prod
    validator — proof that an unconfigured pod cannot silently
    pose as prod."""
    from shared.runtime.platform_survival import RuntimeStamp

    _reset_env(monkeypatch)
    stamp_dict = _checkin_stamp("alpha", "Camino")["stamp"]
    filtered = {
        k: v for k, v in stamp_dict.items()
        if k in RuntimeStamp.__dataclass_fields__
    }
    stamp = RuntimeStamp(**filtered)
    result = stamp.validate_for_prod_sidecar()
    assert result["ok"] is False
    assert "ENV_NOT_PROD" in result["errors"]
    assert "MC_URL_NOT_PROD" in result["errors"]

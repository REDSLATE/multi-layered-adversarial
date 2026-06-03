"""Feeder auth error-message contract (2026-02-20).

Doctrine:
  The 401 returned by `/api/ingest/ohlcv*` must name the `env_key`
  the token is being compared against. This is PUBLIC info
  (derivable from the `FEEDERS` dict in `shared/technicals.py`) and
  saves the caller a multi-round debugging cycle. The token VALUE
  itself is NEVER echoed back — that's still secret.

What this pins:
  1. 400 when `source` is unrecognized — clear message + lists allowed.
  2. 401 (missing token) names the expected env_key.
  3. 401 (wrong token) names the expected env_key.
  4. 503 (env var unset on MC) names the expected env_key.
  5. None of those errors echo the actual TOKEN value back.
"""
from __future__ import annotations

import os

import pytest
from fastapi import HTTPException

from shared.technicals import _verify_feeder


def test_unknown_source_gets_400_with_allowed_list():
    with pytest.raises(HTTPException) as ei:
        _verify_feeder("bogus", "anything")
    assert ei.value.status_code == 400
    msg = ei.value.detail
    assert "source must be one of" in msg
    assert "kraken_pro" in msg  # known value listed


def test_missing_token_names_env_key(monkeypatch):
    monkeypatch.setenv("KRAKEN_FEEDER_TOKEN", "actual-secret-value")
    with pytest.raises(HTTPException) as ei:
        _verify_feeder("kraken_pro", None)
    assert ei.value.status_code == 401
    msg = ei.value.detail
    assert "missing X-Feeder-Token" in msg
    assert "KRAKEN_FEEDER_TOKEN" in msg, (
        "401 should name the env_key so caller knows where to look"
    )
    assert "actual-secret-value" not in msg, (
        "the ACTUAL token value must NEVER be echoed back to the caller"
    )


def test_wrong_token_names_env_key(monkeypatch):
    monkeypatch.setenv("KRAKEN_FEEDER_TOKEN", "real-secret")
    with pytest.raises(HTTPException) as ei:
        _verify_feeder("kraken_pro", "wrong-secret")
    assert ei.value.status_code == 401
    msg = ei.value.detail
    assert "invalid feeder token" in msg
    assert "KRAKEN_FEEDER_TOKEN" in msg, (
        "401 should name the env_key the token is compared against"
    )
    assert "real-secret" not in msg
    assert "wrong-secret" not in msg, (
        "the CALLER'S token must also never be echoed (could end up "
        "in logs / metrics dashboards)"
    )


def test_unconfigured_env_returns_503_with_env_key(monkeypatch):
    monkeypatch.delenv("KRAKEN_FEEDER_TOKEN", raising=False)
    with pytest.raises(HTTPException) as ei:
        _verify_feeder("kraken_pro", "any-token")
    assert ei.value.status_code == 503
    msg = ei.value.detail
    assert "not configured" in msg
    assert "KRAKEN_FEEDER_TOKEN" in msg


def test_correct_token_passes(monkeypatch):
    monkeypatch.setenv("KRAKEN_FEEDER_TOKEN", "correct-secret-xyz")
    # No exception raised.
    _verify_feeder("kraken_pro", "correct-secret-xyz")

"""Contract tests for the brain-side `mc_identity_v1` drop-in module.

These are RUN BY MC to validate the spec stays stable. Any brain team
copying `mc_identity_v1.py` into their repo should drop a copy of these
tests next to it — same assertions, same field names, no drift.

Pin (2026-05-30):
  - Field names in identity{} are frozen at v1. Renaming a key is a
    breaking change and requires bumping to v2 + dual-publishing both
    shapes during a deprecation window.
  - Lifecycle log line format ("STARTED — …" / "NOT STARTED — missing env vars: …")
    is the operator's grep contract. Don't change it without the grep
    smoke test on the operator's runbook also being updated.
"""
from __future__ import annotations

import importlib
import logging
import sys
import threading
from pathlib import Path

import pytest


# Load the drop-in directly from /app/memory so the test pins what
# we'd actually paste to the brain teams.
_SPEC_PATH = Path("/app/memory/mc_identity_v1.py")


@pytest.fixture
def mod(monkeypatch):
    """Fresh import of the module with a CLEAR env, and reset the
    one-shot lifecycle flag so each test gets a clean lifecycle log."""
    # Strip all four env vars at the start; individual tests opt in.
    for k in ("MC_URL", "MC_INGEST_TOKEN", "MC_BASE_URL", "HEARTBEAT_TOKEN",
              "ENV_NAME", "GIT_SHA", "BROKER_MODE"):
        monkeypatch.delenv(k, raising=False)
    # Fresh import each test so the `_LIFECYCLE_LOGGED` Event resets.
    if "mc_identity_v1" in sys.modules:
        del sys.modules["mc_identity_v1"]
    sys.path.insert(0, str(_SPEC_PATH.parent))
    try:
        m = importlib.import_module("mc_identity_v1")
        yield m
    finally:
        sys.path.remove(str(_SPEC_PATH.parent))


def test_field_names_are_frozen(mod):
    """The set of identity{} keys is a v1 contract. Reordering is
    fine; renaming or removing is breaking."""
    iden = mod.build_identity_block(app_name="alpha", sidecar_version="1.0.0")
    assert set(iden.keys()) == {
        "app_name", "env_name", "git_sha", "broker_mode", "sidecar_version",
        "mc_url_set", "ingest_token_set",
        "mc_base_url_set", "heartbeat_token_set",
        "checkin_worker_eligible",
    }


def test_eligible_iff_all_four_booleans_true(mod, monkeypatch):
    """Composite must be the AND of all four env booleans. Truth table."""
    base = {"MC_URL": "u", "MC_INGEST_TOKEN": "t",
            "MC_BASE_URL": "b", "HEARTBEAT_TOKEN": "h"}
    # All set → eligible
    for k, v in base.items():
        monkeypatch.setenv(k, v)
    iden = mod.build_identity_block(app_name="a", sidecar_version="0")
    assert iden["checkin_worker_eligible"] is True

    # Drop ANY one → not eligible
    for missing in base:
        monkeypatch.delenv(missing)
        iden = mod.build_identity_block(app_name="a", sidecar_version="0")
        assert iden["checkin_worker_eligible"] is False, (
            f"composite must be False when {missing} is unset"
        )
        monkeypatch.setenv(missing, base[missing])


def test_empty_string_counts_as_unset(mod, monkeypatch):
    """Common deploy bug: env var is present but empty. Must be False."""
    monkeypatch.setenv("MC_URL", "")  # empty string
    monkeypatch.setenv("MC_INGEST_TOKEN", "t")
    monkeypatch.setenv("MC_BASE_URL", "b")
    monkeypatch.setenv("HEARTBEAT_TOKEN", "h")
    iden = mod.build_identity_block(app_name="a", sidecar_version="0")
    assert iden["mc_url_set"] is False
    assert iden["checkin_worker_eligible"] is False


def test_whitespace_only_counts_as_unset(mod, monkeypatch):
    """Variant of the empty-string bug — whitespace from a misconfigured
    YAML interpolation should also be treated as unset."""
    monkeypatch.setenv("MC_URL", "   \t  ")
    monkeypatch.setenv("MC_INGEST_TOKEN", "t")
    monkeypatch.setenv("MC_BASE_URL", "b")
    monkeypatch.setenv("HEARTBEAT_TOKEN", "h")
    iden = mod.build_identity_block(app_name="a", sidecar_version="0")
    assert iden["mc_url_set"] is False


def test_lifecycle_log_started_branch_names_all_four_vars(mod, monkeypatch, caplog):
    """STARTED branch must include all four env-var names in the
    'set' clause — operator's grep relies on this format."""
    for k, v in {"MC_URL": "u", "MC_INGEST_TOKEN": "t",
                 "MC_BASE_URL": "b", "HEARTBEAT_TOKEN": "h"}.items():
        monkeypatch.setenv(k, v)
    iden = mod.build_identity_block(app_name="a", sidecar_version="0")
    with caplog.at_level(logging.INFO, logger="brain.mc_identity"):
        mod.log_lifecycle(iden)
    msgs = [r.getMessage() for r in caplog.records]
    assert any("STARTED" in m for m in msgs)
    msg = next(m for m in msgs if "STARTED" in m)
    for name in ("MC_URL", "MC_INGEST_TOKEN", "MC_BASE_URL", "HEARTBEAT_TOKEN"):
        assert name in msg, f"STARTED log must mention {name}"


def test_lifecycle_log_not_started_branch_names_missing_vars(mod, monkeypatch, caplog):
    """NOT STARTED branch must name exactly which env vars are missing.
    This is the operator's <30s diagnostic."""
    monkeypatch.setenv("MC_URL", "u")
    monkeypatch.setenv("MC_BASE_URL", "b")
    # leave MC_INGEST_TOKEN and HEARTBEAT_TOKEN unset
    iden = mod.build_identity_block(app_name="a", sidecar_version="0")
    with caplog.at_level(logging.WARNING, logger="brain.mc_identity"):
        mod.log_lifecycle(iden)
    msgs = [r.getMessage() for r in caplog.records]
    msg = next(m for m in msgs if "NOT STARTED" in m)
    assert "MC_INGEST_TOKEN" in msg
    assert "HEARTBEAT_TOKEN" in msg
    assert "MC_URL" not in msg  # this one IS set — must NOT be reported missing
    assert "MC_BASE_URL" not in msg


def test_lifecycle_log_fires_only_once_per_process(mod, monkeypatch, caplog):
    """Re-imports / reloads can call log_lifecycle multiple times; the
    operator should only see ONE line per process boot."""
    for k, v in {"MC_URL": "u", "MC_INGEST_TOKEN": "t",
                 "MC_BASE_URL": "b", "HEARTBEAT_TOKEN": "h"}.items():
        monkeypatch.setenv(k, v)
    iden = mod.build_identity_block(app_name="a", sidecar_version="0")
    with caplog.at_level(logging.INFO, logger="brain.mc_identity"):
        mod.log_lifecycle(iden)
        mod.log_lifecycle(iden)
        mod.log_lifecycle(iden)
    started_lines = [r for r in caplog.records if "STARTED" in r.getMessage()]
    assert len(started_lines) == 1


def test_start_checkin_worker_skips_when_ineligible(mod, monkeypatch):
    """If eligibility is False, the worker must NOT spawn a thread."""
    # leave all env vars unset → ineligible
    before = threading.active_count()
    mod.start_checkin_worker(interval_s=999, on_tick=lambda: None)
    # No thread should have spawned.
    assert threading.active_count() == before

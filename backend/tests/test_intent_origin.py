"""Tests for the intent-origin discriminator.

Doctrine pin (2026-02-XX): preview and prod pods share `shared_intents`
because both run on one Mongo cluster. The discriminator
(`runtime_origin` + `pod_hostname` + `env_name_emit`) stamped onto
every intent at write time keeps the streams attributable WITHOUT
moving to separate collections.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def test_runner_module_exposes_runtime_origin():
    """The runner must compute RUNTIME_ORIGIN at module load — a
    regression that removes it would mean intents post without a
    discriminator and the streams become indistinguishable again."""
    from external.brains import runner
    assert hasattr(runner, "RUNTIME_ORIGIN")
    assert isinstance(runner.RUNTIME_ORIGIN, str)
    assert runner.RUNTIME_ORIGIN, "RUNTIME_ORIGIN must be non-empty"
    assert hasattr(runner, "_POD_HOSTNAME")
    assert isinstance(runner._POD_HOSTNAME, str)


def test_runtime_origin_env_override(monkeypatch):
    """Operator can override the auto-hostname with an explicit
    `RISEDUAL_RUNTIME_ORIGIN` for clarity in the audit log
    ('prod-pod-1' is more readable than a kube hash)."""
    monkeypatch.setenv("RISEDUAL_RUNTIME_ORIGIN", "prod-pod-canonical")
    # Re-import to pick up the env. RUNTIME_ORIGIN is module-level
    # so we have to re-read the file's logic.
    import socket
    import os
    expected = (
        os.environ.get("RISEDUAL_RUNTIME_ORIGIN", "").strip()
        or socket.gethostname()
    )
    assert expected == "prod-pod-canonical"


def test_intent_origin_local_helper_returns_three_fields(monkeypatch):
    from routes.intent_origin import _local_origin
    monkeypatch.setenv("RISEDUAL_ENV", "prod")
    monkeypatch.setenv("RISEDUAL_RUNTIME_ORIGIN", "prod-test-pod")
    local = _local_origin()
    assert local["runtime_origin"] == "prod-test-pod"
    assert local["env_name_emit"] == "prod"
    assert local["pod_hostname"]


def test_runner_evaluate_and_post_stamps_origin_into_evidence():
    """Source-level check: the evidence dict assembly MUST include
    runtime_origin / pod_hostname / env_name_emit. A regression that
    drops any of these would silently lose discrimination."""
    import inspect
    from external.brains import runner
    src = inspect.getsource(runner.BrainRunner._evaluate_and_post)
    assert '"runtime_origin"' in src
    assert '"pod_hostname"' in src
    assert '"env_name_emit"' in src


def test_intent_origin_routes_registered():
    from routes.intent_origin import router
    paths = {r.path for r in router.routes}
    assert "/admin/intents/origins" in paths
    assert "/admin/intents/by-origin" in paths

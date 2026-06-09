"""Tests for the imposter-scan env filter (2026-02-XX).

Doctrine: preview and prod share Mongo, so both pods' check-ins
land in `sidecar_checkin_audit`. The default `env=prod` view on the
prod dashboard isolates the prod-side stream so legitimate preview
check-ins don't flag as imposters.
"""
from __future__ import annotations

import inspect

import pytest

from routes import sidecar_imposter_scan as sis


pytestmark = [pytest.mark.tripwire]


def test_endpoint_accepts_env_query_param():
    """The route signature MUST include an `env` query param so the
    frontend can pass `?env=prod` and `?env=all`."""
    sig = inspect.signature(sis.imposter_scan)
    assert "env" in sig.parameters, (
        "imposter_scan endpoint must accept an `env` query param"
    )


def test_default_env_filter_is_all():
    """Default value preserves legacy behavior (no surprises for
    callers that don't pass `env`)."""
    sig = inspect.signature(sis.imposter_scan)
    default = sig.parameters["env"].default
    # FastAPI Query objects carry the default in `.default`.
    raw = getattr(default, "default", default)
    assert raw == "all"


def test_endpoint_filters_match_by_stamp_env_name():
    """Source-level — the Mongo `$match` must filter on
    `stamp_env_name` (NOT `evidence.env_name` or some other field).
    A regression here would silently bypass the filter."""
    src = inspect.getsource(sis.imposter_scan)
    assert "stamp_env_name" in src
    # And the `all` sentinel must skip adding the filter.
    assert 'env_normalized != "all"' in src or "env_normalized != 'all'" in src


def test_response_includes_env_filter_field():
    """The response carries `env_filter` so the UI knows which mode
    is active."""
    src = inspect.getsource(sis.imposter_scan)
    assert '"env_filter": env_normalized' in src


def test_endpoint_normalizes_env_input():
    """Whitespace and casing must be trimmed/lowercased to avoid
    `Prod` and ` prod ` silently mismatching documents."""
    src = inspect.getsource(sis.imposter_scan)
    assert ".strip().lower()" in src

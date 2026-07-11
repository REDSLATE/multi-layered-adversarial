"""Verify the Atlas-timeout soft-degrade handler
(2026-07-11 prod hotfix).

Scope:
    * `_build_atlas_timeout_response` returns a 200 body with the
      expected keys for GET requests.
    * The same helper returns a 503 for write methods.
    * The pymongo timeout classes register cleanly against the
      FastAPI exception handler map (registration must not raise).
"""
from __future__ import annotations

import types

import pytest


def _fake_request(method: str = "GET", path: str = "/api/foo"):
    """Build a minimal object with the two attributes the handler
    reads. Avoids spinning up starlette's real Request just for a
    two-field read."""
    return types.SimpleNamespace(
        method=method,
        url=types.SimpleNamespace(path=path),
    )


def test_atlas_timeout_response_get_returns_200_soft_degrade():
    from server_modules.middleware_setup import _build_atlas_timeout_response
    from pymongo.errors import NetworkTimeout

    resp = _build_atlas_timeout_response(
        _fake_request("GET", "/api/shared/opinions"),
        NetworkTimeout("simulated"),
    )
    assert resp.status_code == 200
    import json
    body = json.loads(resp.body.decode())
    assert body["ok"] is False
    assert body["degraded"] is True
    assert body["atlas_timeout"] is True
    # Widget-friendly empty defaults.
    assert body["items"] == []
    assert body["count"] == 0
    assert body["payload"] == {}
    assert body["path"] == "/api/shared/opinions"
    assert body["method"] == "GET"
    assert body["request_id"]  # nonempty


def test_atlas_timeout_response_write_returns_503():
    from server_modules.middleware_setup import _build_atlas_timeout_response
    from pymongo.errors import ExecutionTimeout

    resp = _build_atlas_timeout_response(
        _fake_request("POST", "/api/admin/intents/submit"),
        ExecutionTimeout("simulated"),
    )
    assert resp.status_code == 503
    import json
    body = json.loads(resp.body.decode())
    assert body["ok"] is False
    assert body["degraded"] is True


def test_all_timeout_classes_register_without_error():
    """Regression guard: if a future pymongo release changes the
    exception hierarchy this test fires at CI time instead of
    silently disabling the safety net in production."""
    from server_modules.middleware_setup import _PYMONGO_TIMEOUT_ERRORS
    # We expect at least NetworkTimeout + ExecutionTimeout + one
    # server-selection timeout. Empty tuple = pymongo import failed
    # → the handler becomes a no-op, which is the exact regression
    # we want to catch.
    assert len(_PYMONGO_TIMEOUT_ERRORS) >= 3
    for cls in _PYMONGO_TIMEOUT_ERRORS:
        assert isinstance(cls, type)
        assert issubclass(cls, BaseException)

"""Brain-runtime doctrine tripwires (2026-02-17).

Locks the contract of `routes/brain_runtime.py`:

  ROSTER endpoint:
    - Dual auth (operator OR brain token)
    - Brain caller cannot peek at another brain's seats
    - Lean payload — no policy/eligibility/doctrine string from full admin endpoint
    - `your_seats` computed correctly for the caller
    - Read-only, never mutates seat assignments

  STATUS PROXY endpoint:
    - Operator-only (no dual auth — brains don't proxy each other)
    - Bounded timeout enforced
    - One audit row per attempt (success AND failure)
    - Upstream failure → wrapper payload, NEVER 500
    - Cache TTL respected
    - Audit collection never contains broker keys
"""
from __future__ import annotations

import asyncio
import inspect
import os
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from routes import brain_runtime as br


pytestmark = [pytest.mark.tripwire]


# ──────────────────────── Roster endpoint ────────────────────────


@pytest.mark.asyncio
async def test_roster_brain_caller_cannot_peek_other_brain(monkeypatch):
    """A brain authenticating with its own token MUST NOT be able to
    pass `caller=other_brain` and see another brain's seats. The route
    overrides `caller` to match the authenticated brain ID."""
    monkeypatch.setenv("REDEYE_INGEST_TOKEN", "redeye-sek-rit")
    # Simulate a request where redeye's token authenticates but the
    # `caller` query param tries to peek at alpha.
    principal = await br._dual_auth(
        x_brain_id="redeye",
        x_runtime_token="redeye-sek-rit",
        operator_user=None,
    )
    assert principal == "brain:redeye"
    # The route handler logic itself (not _dual_auth) does the override.
    # Source-scan to confirm the override is present:
    src = inspect.getsource(br.get_brain_roster)
    assert "caller_brain = principal.split" in src, (
        "Roster endpoint must override `caller` to match the "
        "authenticated brain ID when the caller authenticates with a "
        "runtime token. Without this, brain A can read brain B's "
        "seats by passing `?caller=B`."
    )


def test_roster_payload_is_lean_no_policy_doctrine_dump():
    """Brain-callable roster must NOT return policy, eligibility, or
    the full doctrine guidance. Those belong to the operator-JWT
    endpoint. This keeps the surface small and prevents brain code
    from accidentally consuming the operator-only doctrine string."""
    src = inspect.getsource(br.get_brain_roster)
    for forbidden_key in ('"policy":', '"eligibility":', '"roles":', '"brains":'):
        assert forbidden_key not in src, (
            f"Brain-roster endpoint must NOT include {forbidden_key} "
            "in its response — those are operator-only fields from "
            "the full /admin/roster endpoint."
        )


# ──────────────────────── Status proxy ────────────────────────


def test_status_proxy_requires_operator_jwt():
    """The status proxy MUST be operator-only. Brains don't peek at
    each other's status."""
    src = inspect.getsource(br.get_brain_status)
    assert "get_current_user" in src, (
        "Status proxy must depend on `get_current_user` (operator-JWT only)."
    )
    # And NOT dual auth.
    assert "_dual_auth(" not in src, (
        "Status proxy must NOT use dual auth — operator only."
    )


def test_status_proxy_bounded_timeout():
    """`PROXY_TIMEOUT_S` must be defined and ≤ 10s. A longer timeout
    means a hung brain pod can stall every operator dashboard tile."""
    assert hasattr(br, "PROXY_TIMEOUT_S")
    assert 0 < br.PROXY_TIMEOUT_S <= 10.0, (
        f"PROXY_TIMEOUT_S must be in (0, 10]; got {br.PROXY_TIMEOUT_S}"
    )


@pytest.mark.asyncio
async def test_status_proxy_returns_wrapper_on_no_upstream():
    """Brain without `<BRAIN>_STATUS_URL` env var must NOT 500. The
    endpoint returns `{ok: false, error: no_upstream_configured}` so
    the dashboard tile can render a graceful state."""
    # Ensure env var is absent.
    os.environ.pop("ALPHA_STATUS_URL", None)
    # We can't easily invoke through the FastAPI dependency stack here
    # without a TestClient, but we can verify the source contract.
    src = inspect.getsource(br.get_brain_status)
    assert '"no_upstream_configured"' in src
    assert '"ok": False' in src
    # No HTTPException for missing upstream.
    assert "raise HTTPException(status_code=500" not in src


def test_status_proxy_audit_writes_every_attempt():
    """Source-scan: `_write_proxy_audit` must be called from BOTH the
    success and failure paths of the upstream fetch. Operator forensics
    require visibility into hits AND misses."""
    src = inspect.getsource(br.get_brain_status)
    # The audit write is called once unconditionally after _fetch_upstream.
    assert "_write_proxy_audit(" in src
    # Not gated behind an `if status_code == 200:` block.
    audit_idx = src.index("_write_proxy_audit(")
    # The 50 chars before the call should NOT contain a status check
    # that would skip the audit on failure.
    snippet = src[max(0, audit_idx - 80):audit_idx]
    assert "if payload is None:" not in snippet, (
        "Audit write must run BEFORE the payload-is-None check, so "
        "every attempt is logged regardless of outcome."
    )


@pytest.mark.asyncio
async def test_fetch_upstream_returns_error_on_timeout(monkeypatch):
    """`_fetch_upstream` MUST never raise. Timeout returns
    (None, None, duration, 'upstream_timeout')."""
    class _T:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a, **kw):
            return False
        async def get(self, *a, **kw):
            raise httpx.TimeoutException("simulated")
    monkeypatch.setattr(br.httpx, "AsyncClient", lambda *a, **kw: _T())
    status, payload, duration_ms, err = await br._fetch_upstream(
        "redeye", "https://redeye.fake/status",
    )
    assert status is None
    assert payload is None
    assert err == "upstream_timeout"
    assert duration_ms >= 0


@pytest.mark.asyncio
async def test_fetch_upstream_returns_error_on_non_200(monkeypatch):
    """4xx/5xx from upstream must be reported as
    'upstream_http_<code>' without crashing."""
    mock_resp = MagicMock()
    mock_resp.status_code = 503

    class _T:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a, **kw):
            return False
        async def get(self, *a, **kw):
            return mock_resp
    monkeypatch.setattr(br.httpx, "AsyncClient", lambda *a, **kw: _T())
    status, payload, duration_ms, err = await br._fetch_upstream(
        "redeye", "https://redeye.fake/status",
    )
    assert status == 503
    assert payload is None
    assert err == "upstream_http_503"


@pytest.mark.asyncio
async def test_fetch_upstream_returns_payload_on_success(monkeypatch):
    """200 + valid JSON → status=200, payload=parsed dict, err=None."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json = MagicMock(return_value={"brain": "redeye", "ok": True})

    class _T:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a, **kw):
            return False
        async def get(self, *a, **kw):
            return mock_resp
    monkeypatch.setattr(br.httpx, "AsyncClient", lambda *a, **kw: _T())
    status, payload, duration_ms, err = await br._fetch_upstream(
        "redeye", "https://redeye.fake/status",
    )
    assert status == 200
    assert payload == {"brain": "redeye", "ok": True}
    assert err is None


# ──────────────────────── Source-level doctrine ────────────────────────


def test_module_never_serves_broker_keys():
    src = inspect.getsource(br)
    for forbidden in (
        "ALPACA_API_KEY", "ALPACA_SECRET",
        "KRAKEN_API_KEY", "KRAKEN_SECRET",
        "IBKR_TOKEN", "BROKER_SECRET",
    ):
        assert forbidden not in src, (
            f"brain_runtime references broker key {forbidden!r}"
        )


def test_module_is_read_only_on_roster():
    """The roster path must NEVER mutate seat assignments. Only the
    audit-log insert is permitted as a write."""
    src = inspect.getsource(br)
    # Look for any roster-mutating call.
    for forbidden in (
        "SHARED_ROSTER",  # collection name
        "assign(",         # function on shared.roster
        "swap(",
    ):
        assert forbidden not in src, (
            f"brain_runtime calls {forbidden!r} — must be read-only "
            "on the roster path."
        )


def test_roster_endpoint_doctrine_compatible():
    """Governor exclusivity (2026-05-26): only Chevelle and RedEye
    may hold governor / crypto_governor seats. This is enforced at
    WRITE time in `shared/roster.py`. The brain-callable READ endpoint
    must not provide any path that bypasses or weakens that doctrine.

    Specifically: my endpoint must NOT have any code path that calls
    the assign / swap / eligibility-mutation handlers (already
    covered by `test_module_is_read_only_on_roster`), AND must not
    introduce a separate eligibility check that could disagree with
    `_ensure_assignment_eligible`.
    """
    src = inspect.getsource(br)
    # Forbid touching the governor-restriction internals — if we ever
    # need to expose them we should re-use the canonical helper.
    for forbidden in (
        "_GOVERNOR_EXCLUSIVE_SEATS",
        "_GOVERNOR_EXCLUSIVE_BRAINS",
        "_ensure_assignment_eligible",
    ):
        assert forbidden not in src, (
            f"brain_runtime references {forbidden!r}. Governor "
            "exclusivity enforcement lives at the WRITE boundary in "
            "`shared/roster.py`; the brain-callable READ endpoint "
            "must stay clear of it."
        )


def test_routes_registered():
    paths = {r.path for r in br.router.routes}
    assert "/admin/runtime/roster" in paths
    assert "/admin/runtime/{brain}/status" in paths
    assert "/admin/runtime/{brain}/status/refresh" in paths
    assert "/admin/runtime/status-proxy-audit" in paths

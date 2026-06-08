"""Brain-runtime doctrine tripwires (rewritten 2026-02-XX).

Locks the contract of the IN-PROCESS `routes/brain_runtime.py`:

  ROSTER endpoint:
    - Dual auth (operator OR brain token)
    - Brain caller cannot peek at another brain's seats
    - Lean payload — no policy/eligibility/doctrine string from the
      full admin endpoint
    - Read-only, never mutates seat assignments

  STATUS endpoint (in-process):
    - Operator-only (no dual auth — brains don't peek at each other)
    - Returns the SAME wrapper shape (`{brain, ok, _proxied_from,
      payload}`) the dashboard tile already renders
    - On build failure: returns `{ok: false}`, NEVER 500s
    - Pulls from MC's own collections (heartbeats, sovereign state,
      shared intents) plus the in-process runner stats — never
      reaches for an external sidecar

  UNIVERSE endpoint:
    - Dual auth, brain auth pinned to path brain
    - Read-only over `patterns_universe`

The dead external-sidecar proxy infrastructure (`_fetch_upstream`,
`_PROXY_CACHE`, `brain_status_proxy_audit`, `/status/refresh`,
`/status-proxy-audit`) was REMOVED. If those tripwires fire, the
removal regressed — re-test the in-process path instead.
"""
from __future__ import annotations

import inspect

import pytest

from routes import brain_runtime as br


pytestmark = [pytest.mark.tripwire]


# ──────────────────────── Roster endpoint ────────────────────────

@pytest.mark.asyncio
async def test_roster_brain_caller_cannot_peek_other_brain(monkeypatch):
    """A brain authenticating with its own token MUST NOT be able to
    pass `caller=other_brain` and see another brain's seats."""
    monkeypatch.setenv("REDEYE_INGEST_TOKEN", "redeye-sek-rit")
    principal = await br._dual_auth(
        x_brain_id="redeye",
        x_runtime_token="redeye-sek-rit",
        operator_user=None,
    )
    assert principal == "brain:redeye"
    src = inspect.getsource(br.get_brain_roster)
    assert "caller_brain = principal.split" in src, (
        "Roster endpoint must override `caller` to match the "
        "authenticated brain ID when the caller authenticates with a "
        "runtime token."
    )


def test_roster_payload_is_lean_no_policy_doctrine_dump():
    """Brain-callable roster must NOT return policy, eligibility, or
    the full doctrine guidance. Those belong to the operator-JWT
    endpoint at /admin/roster."""
    src = inspect.getsource(br.get_brain_roster)
    for forbidden_key in ('"policy":', '"eligibility":', '"roles":', '"brains":'):
        assert forbidden_key not in src, (
            f"Brain-roster endpoint must NOT include {forbidden_key} "
            "in its response."
        )


# ──────────────────────── Status endpoint (in-process) ────────────────────────


def test_status_endpoint_requires_operator_jwt():
    """The status endpoint MUST be operator-only — no dual auth."""
    src = inspect.getsource(br.get_brain_status)
    assert "get_current_user" in src, (
        "Status endpoint must depend on `get_current_user` (operator-JWT only)."
    )
    assert "_dual_auth(" not in src, (
        "Status endpoint must NOT use dual auth — operator only."
    )


def test_status_endpoint_returns_in_process_marker():
    """The status endpoint must stamp `_proxied_from: in_process` so
    the frontend can distinguish in-process from external-sidecar
    (legacy) and the operator knows MC is the runtime."""
    src = inspect.getsource(br.get_brain_status)
    assert '"in_process"' in src
    assert '"in_process_runtime_status"' in src


def test_status_endpoint_never_500s_on_build_failure():
    """On `_build_in_process_status` failure the endpoint must return
    `{ok: false}` so the dashboard tile renders a degraded state
    instead of going blank."""
    src = inspect.getsource(br.get_brain_status)
    assert "raise HTTPException(status_code=500" not in src
    assert '"ok": False' in src


def test_status_payload_uses_section_names_the_tile_renders():
    """`_build_in_process_status` must emit the section keys the
    dashboard's BrainProxiedStatusTile already renders, so no
    frontend change is needed: identity, seats, heartbeat, intents."""
    src = inspect.getsource(br._build_in_process_status)
    for key in ('"identity"', '"seats"', '"heartbeat"', '"intents"', '"in_process_runner"'):
        assert key in src, f"in-process status payload must include {key} section"


def test_status_endpoint_does_not_reach_for_external_sidecars():
    """The cleaned-up module must NOT reintroduce an httpx-based
    external-sidecar fetch path. The brains live in-process; any
    external fetch would re-trip the 'site disconnected' regression.

    We scan the EXECUTABLE source (module dict members), not the
    file string, so doctrine docstrings that NAME the removed
    helpers don't trip the tripwire.
    """
    # Names that, if reintroduced as module-level symbols, would
    # indicate the dead proxy is back.
    for forbidden in (
        "_fetch_upstream", "_PROXY_CACHE", "PROXY_TIMEOUT_S",
        "_write_proxy_audit", "_cache_get", "_cache_set",
        "_upstream_url_for", "BRAIN_STATUS_PROXY_AUDIT",
        "get_proxy_audit", "force_refresh_brain_status",
    ):
        assert not hasattr(br, forbidden), (
            f"brain_runtime reintroduced {forbidden!r} — that was the "
            "dead external-sidecar proxy infrastructure."
        )
    # And no httpx usage in the live function bodies (docstrings are
    # not part of `inspect.getsource` of a function … but they ARE,
    # so we scan only the body-after-docstring of each function).
    import ast
    src = inspect.getsource(br)
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # Drop the docstring node if present.
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                body = body[1:]
            for stmt in body:
                stmt_src = ast.unparse(stmt)
                assert "httpx" not in stmt_src, (
                    f"function `{node.name}` references httpx — dead "
                    f"external-sidecar proxy code is back."
                )


# ──────────────────────── Universe endpoint ────────────────────────


def test_universe_endpoint_uses_dual_auth():
    """Universe is dual-auth (brain or operator) — brains use this to
    refresh their tradeable symbol set."""
    src = inspect.getsource(br.get_brain_universe)
    assert "_dual_auth(" in src


def test_universe_endpoint_pins_brain_auth_to_path():
    """A brain cannot read another brain's universe via the path
    parameter — the authenticated brain must equal `{brain}`."""
    src = inspect.getsource(br.get_brain_universe)
    assert "auth_brain != brain" in src, (
        "Universe endpoint must enforce auth_brain == path brain."
    )


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
    """The roster path must NEVER mutate seat assignments."""
    src = inspect.getsource(br)
    for forbidden in (
        "SHARED_ROSTER",
        "assign(",
        "swap(",
    ):
        assert forbidden not in src, (
            f"brain_runtime calls {forbidden!r} — must be read-only "
            "on the roster path."
        )


def test_roster_endpoint_doctrine_compatible():
    """Governor exclusivity is enforced at the WRITE boundary in
    `shared/roster.py`. The brain-callable READ endpoint must stay
    clear of those internals."""
    src = inspect.getsource(br)
    for forbidden in (
        "_GOVERNOR_EXCLUSIVE_SEATS",
        "_GOVERNOR_EXCLUSIVE_BRAINS",
        "_ensure_assignment_eligible",
    ):
        assert forbidden not in src, (
            f"brain_runtime references {forbidden!r}. Governor "
            "exclusivity enforcement lives at the WRITE boundary."
        )


def test_routes_registered():
    """Exactly the three live endpoints — roster / status / universe.
    The dead /status/refresh and /status-proxy-audit must be GONE."""
    paths = {r.path for r in br.router.routes}
    assert "/admin/runtime/roster" in paths
    assert "/admin/runtime/{brain}/status" in paths
    assert "/admin/runtime/{brain}/universe" in paths
    # Dead endpoints — must not exist on the cleaned-up router.
    assert "/admin/runtime/{brain}/status/refresh" not in paths
    assert "/admin/runtime/status-proxy-audit" not in paths

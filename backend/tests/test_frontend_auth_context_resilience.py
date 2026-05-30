"""
Tripwire: Frontend AuthContext must NOT clear the operator's session
token on transient backend errors.

History (2026-02-17): Operators were being silently logged out during
MC redeploys, brief Cloudflare blips, or any 5xx from `/auth/me`. The
original `AuthContext.js` called `setToken(null)` inside a bare
`catch {}` — any failure (network, 500, 502, 504, timeout) wiped the
token and bounced them to /login.

This tripwire is a SOURCE-LEVEL invariant. It does not run the React
code (no jsdom dependency here) — it asserts that the source file
itself encodes the correct contract:

  1. Token-clearing is gated on an explicit auth-rejection check.
  2. The auth-rejection check accepts ONLY HTTP 401 and 403.
  3. Transient failures get retried (RETRY_DELAYS_MS exists).
  4. There is no bare `catch { setToken(null) }` pattern.

If any of these assertions fail, the frontend has regressed to the
"logout-on-any-error" behaviour.
"""
from __future__ import annotations

import re
from pathlib import Path

AUTH_CONTEXT = Path("/app/frontend/src/context/AuthContext.js")


def _read_source() -> str:
    assert AUTH_CONTEXT.exists(), f"AuthContext.js missing at {AUTH_CONTEXT}"
    return AUTH_CONTEXT.read_text(encoding="utf-8")


def test_auth_context_file_exists():
    src = _read_source()
    assert "AuthProvider" in src
    assert "useAuth" in src


def test_auth_rejection_only_on_401_or_403():
    """
    AUTH_ERROR_STATUSES set must contain 401 and 403 — and ONLY those.
    A regression that added 500 / 502 / 504 here would re-introduce the
    logout-on-transient-error bug.
    """
    src = _read_source()
    match = re.search(
        r"AUTH_ERROR_STATUSES\s*=\s*new\s+Set\(\s*\[([^\]]*)\]\s*\)",
        src,
    )
    assert match, "AUTH_ERROR_STATUSES set declaration not found"
    statuses_raw = match.group(1)
    statuses = {
        int(tok.strip())
        for tok in statuses_raw.split(",")
        if tok.strip().isdigit()
    }
    assert statuses == {401, 403}, (
        f"AUTH_ERROR_STATUSES must be exactly {{401, 403}}, got {statuses}. "
        "Adding any other status here will log operators out on transient "
        "MC errors."
    )


def test_retry_schedule_present():
    """
    Transient errors must be retried, not surfaced as instant logout.
    The retry delay array must exist and have at least one entry.
    """
    src = _read_source()
    match = re.search(
        r"RETRY_DELAYS_MS\s*=\s*\[([^\]]*)\]",
        src,
    )
    assert match, "RETRY_DELAYS_MS retry schedule not declared"
    delays = [
        int(tok.strip())
        for tok in match.group(1).split(",")
        if tok.strip().isdigit()
    ]
    assert len(delays) >= 1, (
        "RETRY_DELAYS_MS must have at least one retry delay; otherwise "
        "a single transient blip logs the operator out."
    )
    # Sanity: delays should be positive and total > 500ms of patience.
    assert all(d > 0 for d in delays), "Retry delays must be positive"
    assert sum(delays) >= 500, (
        "Cumulative retry patience must be ≥ 500ms; otherwise the "
        "retry loop completes faster than a typical Cloudflare blip."
    )


def test_no_unconditional_set_token_null_in_auth_me_catch():
    """
    Forbid the regression pattern: a bare catch around `/auth/me`
    that immediately calls `setToken(null)` without first checking
    if the error is a real auth rejection.
    """
    src = _read_source()
    # Locate the /auth/me call site.
    assert 'api.get("/auth/me")' in src, "/auth/me call site missing"

    # Find any `catch {` (no error binding) AND `catch (e) {` blocks
    # in the body of AuthProvider. The pattern we forbid is one that
    # *immediately* calls setToken(null) without an isAuthRejection
    # check between the catch and the setToken call.
    forbidden = re.compile(
        r"catch\s*(\(\s*\w+\s*\))?\s*\{\s*setToken\s*\(\s*null\s*\)",
    )
    assert not forbidden.search(src), (
        "Found a bare `catch { setToken(null) }` pattern. Token "
        "clearing MUST be gated on an explicit auth-rejection check, "
        "otherwise transient backend errors will log operators out."
    )


def test_isauthrejection_guards_token_clear():
    """
    The token clear must be reachable only from inside an
    `isAuthRejection(...)` branch.
    """
    src = _read_source()
    assert "isAuthRejection" in src, (
        "isAuthRejection helper must exist to distinguish real "
        "auth failures from transient errors."
    )
    # Find every setToken(null) call inside AuthContext.js. The ONLY
    # ones permitted are:
    #   a) Inside an `if (isAuthRejection(...))` block
    #   b) Inside the `logout` callback
    # We enforce (a) loosely by requiring that every setToken(null)
    # occurrence sits within ~200 chars after an `isAuthRejection` or
    # `logout` token.
    set_token_null_positions = [
        m.start() for m in re.finditer(r"setToken\(\s*null\s*\)", src)
    ]
    assert set_token_null_positions, (
        "No setToken(null) calls found — file may have been gutted."
    )
    for pos in set_token_null_positions:
        window_start = max(0, pos - 400)
        window = src[window_start:pos]
        permitted = (
            "isAuthRejection" in window
            or "logout" in window
        )
        assert permitted, (
            f"setToken(null) at offset {pos} is not guarded by an "
            "`isAuthRejection(...)` check or inside `logout`. This "
            "would log operators out on transient errors."
        )

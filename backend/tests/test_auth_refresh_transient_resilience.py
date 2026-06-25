"""
Tripwire: api.js `tryRefresh` must distinguish REAL auth rejection
(401/403 from /auth/refresh) from TRANSIENT failures (5xx, network,
Cloudflare 520/502/504). Without this, a single Cloudflare 520 on
/auth/refresh logs the operator out — the user-reported "3-min
auto-logout" symptom on mission.risedual.ai prod (2026-02-23).

Source-level invariants enforced here:

  1. `tryRefresh` returns a tri-state object — `{token}`, `{rejected}`,
     or `{transient}` — NOT a bare token-or-null.
  2. The 401-interceptor only calls `setToken(null)` inside the
     `result.rejected` branch (never inside `result.transient`).
  3. AuthContext.login uses the same RETRY_DELAYS_MS retry doctrine
     so a transient 520 on POST /auth/login doesn't immediately
     surface "Cannot reach Mission Control" to the operator.
  4. The `risedual:auth-expired` CustomEvent carries a `reason`
     string in `detail` so the /login banner can tell the operator
     WHY they were bounced (401 vs cookie-drop vs unknown).

If these regress, the prod logout symptom returns.
"""
from __future__ import annotations

import re
from pathlib import Path

API_JS         = Path("/app/frontend/src/lib/api.js")
AUTH_CTX       = Path("/app/frontend/src/context/AuthContext.js")
LOGIN_PAGE     = Path("/app/frontend/src/pages/Login.jsx")


def _read(p: Path) -> str:
    assert p.exists(), f"missing: {p}"
    return p.read_text(encoding="utf-8")


def test_try_refresh_returns_tristate():
    """`tryRefresh` must surface a rejected vs transient distinction."""
    src = _read(API_JS)
    # The function must yield BOTH `rejected: true` and `transient: true`
    # somewhere in its body.
    assert "rejected: true" in src, (
        "tryRefresh must return { rejected: true, ... } on 401/403 "
        "from /auth/refresh — otherwise the 401-interceptor cannot "
        "distinguish real auth rejection from transient 5xx."
    )
    assert "transient: true" in src, (
        "tryRefresh must return { transient: true, ... } on 5xx / "
        "network failures so the caller KEEPS the token instead of "
        "purging the session on a Cloudflare 520."
    )


def test_try_refresh_does_not_clear_on_any_non_2xx():
    """The legacy 'if (!resp.ok) return null' shape would re-introduce
    the 520-logs-out bug. Forbid that exact pattern."""
    src = _read(API_JS)
    # The replacement code branches on `resp.status === 401 || 403`
    # BEFORE the generic `!resp.ok` fall-through. Assert that branch
    # exists.
    assert re.search(
        r"resp\.status\s*===\s*401\s*\|\|\s*resp\.status\s*===\s*403",
        src,
    ), (
        "tryRefresh must explicitly branch on 401/403 from "
        "/auth/refresh before falling through to the transient path."
    )


def test_set_token_null_only_inside_rejected_branch():
    """The api.js 401-interceptor must call setToken(null) ONLY inside
    `result.rejected` — never inside `result.transient`."""
    src = _read(API_JS)
    # Find every setToken(null) call in api.js.
    positions = [m.start() for m in re.finditer(r"setToken\(\s*null\s*\)", src)]
    assert positions, "setToken(null) call site missing in api.js"
    for pos in positions:
        window = src[max(0, pos - 400):pos]
        # The preceding code must reference `result.rejected` OR be
        # part of a refresh-result narrowing. We accept either the
        # literal `result.rejected` token or a generic `rejected`
        # check within 400 chars.
        assert "rejected" in window, (
            f"setToken(null) at offset {pos} is not guarded by a "
            "`result.rejected` check. Transient 5xx from "
            "/auth/refresh would purge the session — exactly the "
            "2026-02-23 prod symptom this fix exists to prevent."
        )


def test_auth_expired_event_carries_reason():
    """The /login banner reads `detail.reason` from
    `risedual:auth-expired`. The event MUST carry a reason string so
    operators can tell 401 vs Cloudflare-520 vs cookie-drop apart."""
    src = _read(API_JS)
    # Look for a CustomEvent dispatch with a `reason` field in detail.
    assert re.search(
        r"risedual:auth-expired[\s\S]{0,200}reason:",
        src,
    ), (
        "The risedual:auth-expired CustomEvent must include a "
        "`reason` field in `detail` so /login can render which "
        "failure mode bounced the operator."
    )


def test_login_uses_retry_with_backoff():
    """AuthContext.login must retry on transient (5xx/network) errors.
    A 520 on POST /api/auth/login should not surface to the operator
    on the first attempt — mirror the /auth/me doctrine."""
    src = _read(AUTH_CTX)
    # The login callback must reference RETRY_DELAYS_MS AND
    # isAuthRejection (to short-circuit on 401/403 — real wrong creds).
    login_section_match = re.search(
        r"const\s+login\s*=\s*useCallback\(([\s\S]+?)\n\s*\},\s*\[\]\);",
        src,
    )
    assert login_section_match, "login useCallback not found"
    body = login_section_match.group(1)
    assert "RETRY_DELAYS_MS" in body, (
        "login must use RETRY_DELAYS_MS — without retries, a single "
        "Cloudflare 520 on POST /auth/login surfaces directly to "
        "the operator as 'Cannot reach Mission Control: HTTP 520'."
    )
    assert "isAuthRejection" in body, (
        "login must short-circuit on isAuthRejection so a real "
        "401 (wrong credentials) fails FAST rather than waiting "
        "through retries."
    )


def test_login_page_has_session_lost_banner():
    """Login page must render a session-lost banner sourced from
    sessionStorage so operators see WHY they were bounced."""
    src = _read(LOGIN_PAGE)
    assert 'sessionStorage.getItem("risedual_session_lost")' in src or \
           'sessionStorage.getItem(\'risedual_session_lost\')' in src, (
        "Login page must read `risedual_session_lost` from "
        "sessionStorage on mount."
    )
    assert 'data-testid="login-session-lost-banner"' in src, (
        "Login page must mount the session-lost banner with a "
        "stable data-testid so QA can drive it."
    )
    assert 'data-testid="login-session-lost-dismiss"' in src, (
        "Banner must have a dismiss control."
    )


def test_session_lost_cleared_on_successful_login():
    """A successful login must clear the session-lost banner state
    so the next render of /login doesn't show a stale reason."""
    src = _read(AUTH_CTX)
    assert 'sessionStorage.removeItem("risedual_session_lost")' in src, (
        "Successful login must clear the session-lost state, "
        "otherwise the banner persists across sessions."
    )

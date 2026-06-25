"""Tripwire: api.js MUST retry idempotent GETs on transient 5xx.

Operator pin (2026-02-23): Prod `mission.risedual.ai` was showing
inline `HTTP 520` on multiple panels simultaneously (intents feed,
master trading switch, OTOCO, parabolic phase map, seat roster,
tunables simulator). Root cause: Cloudflare edge ↔ origin transient
failures on the consolidated MC pod. The previous auth-resilience
fix prevented logouts; this fix makes single-shot 520s self-recover
at the request layer so panels don't surface "HTTP 520" anymore.

Source-level invariants enforced here:

  1. `TRANSIENT_STATUS_CODES` set exists and includes 502, 503, 504,
     520, 522, 523, 524 — the full Cloudflare origin-failure family.
  2. `READ_RETRY_DELAYS_MS` exists with backoff entries — total
     wait window must be > 3s so one transient blip resolves before
     the panel sees an error.
  3. Retry branch in `request()` guards on `method !== "GET"` so
     POST/PUT/PATCH/DELETE are NEVER retried (no double-fire of
     side effects: ARM ALL, seat assign, flag flip, etc.).
  4. Network-error catch branch (fetch throw) ALSO calls the retry
     helper — DNS / TCP reset / TLS hiccups must self-recover too.
  5. The retry counter is threaded through cfg so the recursion
     terminates (no infinite-retry footgun).

If any of these regress, the prod 520-on-every-panel symptom
returns.
"""
from __future__ import annotations

import re
from pathlib import Path

API_JS = Path("/app/frontend/src/lib/api.js")


def _read() -> str:
    return API_JS.read_text(encoding="utf-8")


def test_transient_status_codes_set_present():
    src = _read()
    assert "TRANSIENT_STATUS_CODES" in src, (
        "api.js must declare TRANSIENT_STATUS_CODES — the set of "
        "HTTP statuses that trigger a GET retry."
    )
    # All seven Cloudflare-class origin failures must be in the set.
    for code in (502, 503, 504, 520, 522, 523, 524):
        assert re.search(rf"\b{code}\b", src), (
            f"TRANSIENT_STATUS_CODES must include {code} — without "
            f"it, panels hitting a {code} won't auto-retry."
        )


def test_read_retry_delays_present_with_meaningful_backoff():
    src = _read()
    m = re.search(r"READ_RETRY_DELAYS_MS\s*=\s*\[([^\]]+)\]", src)
    assert m, "READ_RETRY_DELAYS_MS must be declared as an array"
    nums = [int(n) for n in re.findall(r"\d+", m.group(1))]
    assert len(nums) >= 2, (
        "Need at least 2 retry delays — single-retry is too thin "
        "for a Cloudflare blip that takes 1-2s to resolve."
    )
    total = sum(nums)
    assert total >= 3000, (
        f"Total retry window {total}ms < 3000ms — too short for "
        "Cloudflare edge to recover. Bump the delays."
    )


def test_retry_guard_excludes_non_get_methods():
    """POST/PUT/PATCH/DELETE must NEVER auto-retry — side effects
    can't be silently double-fired."""
    src = _read()
    # The retry helper or its branch must short-circuit on
    # method != "GET".
    assert re.search(r'method\s*!==\s*"GET"', src), (
        "api.js must guard the retry path with `method !== \"GET\"` "
        "to prevent POST/PUT/PATCH/DELETE from silently retrying "
        "and double-firing side effects (ARM ALL, seat assign, "
        "flag flip, etc.)."
    )


def test_network_error_branch_also_retries():
    """Fetch-throw (network / DNS / TLS) on a GET must take the same
    retry path as a transient 5xx — otherwise prod TLS hiccups still
    surface as inline errors."""
    src = _read()
    # The catch block around fetch must call the retry helper.
    catch_block = re.search(
        r"catch\s*\(e\)\s*\{[\s\S]+?throw err;",
        src,
    )
    assert catch_block, "fetch catch block not found in api.js"
    body = catch_block.group(0)
    assert "_retryIfTransient" in body or "READ_RETRY_DELAYS_MS" in body, (
        "The network-error catch block must invoke the retry helper "
        "(same path as transient 5xx). Without it, prod DNS / TLS / "
        "TCP-reset hiccups still surface as inline 'Network error'."
    )


def test_retry_counter_threaded_through_cfg():
    """The recursive retry call must bump cfg._readAttempt so the
    recursion terminates — no infinite-retry footgun."""
    src = _read()
    assert "_readAttempt" in src, (
        "Retry depth must be threaded through cfg._readAttempt to "
        "terminate after READ_RETRY_DELAYS_MS.length attempts."
    )
    # The recursive call must pass an incremented counter.
    assert re.search(r"_readAttempt:\s*attempt\s*\+\s*1", src), (
        "Recursive retry must increment cfg._readAttempt to avoid "
        "an infinite loop on persistent 520s."
    )


def test_retry_branch_runs_before_error_decode():
    """The transient-5xx branch must execute BEFORE the !resp.ok
    error-decoding path inside `request()`, otherwise the retry
    never happens and the panel just sees `HTTP 520` directly."""
    src = _read()
    # Scope to the request() function body. tryRefresh() also has
    # its own `if (!resp.ok)` which we must not pick up.
    req_match = re.search(
        r"async function request\([\s\S]+?\n\}\n",
        src,
    )
    assert req_match, "request() function body not found"
    body = req_match.group(0)
    pos_transient = body.find("TRANSIENT_STATUS_CODES.has(resp.status)")
    pos_resp_not_ok = body.find("if (!resp.ok)")
    assert pos_transient != -1, (
        "transient-5xx branch missing inside request() body"
    )
    assert pos_resp_not_ok != -1, (
        "!resp.ok branch missing inside request() body"
    )
    assert pos_transient < pos_resp_not_ok, (
        "Transient-5xx retry branch must come BEFORE the !resp.ok "
        "error-decode block inside request() — otherwise the error "
        "path runs first and the panel surfaces 'HTTP 520' before "
        "any retry."
    )

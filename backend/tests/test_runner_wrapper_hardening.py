"""Tripwire — verify the May-14 wrapper-hardening doctrine is still
applied to `external/brains/runner.py`. This test exists because the
fixes were silently lost when the brain runner was unified into MC's
monorepo (2026-02-19 audit), causing 3-of-4 brains to go silent for
6-12 days under prod pod rotations. If this test fails in the future,
someone has reintroduced the half-open socket / single-tick / silent-
loop class of bug.
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

# Same path bootstrap as the other neutral-brain tests so `external.brains`
# is importable when running from `cd /app/backend && pytest`.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def test_phased_httpx_timeout_helper_exists():
    """Fix #1 — httpx clients must use bounded per-phase timeouts and
    have keep-alive disabled, not a scalar `timeout=N`."""
    from external.brains.runner import _create_http_client
    client = _create_http_client()
    try:
        # httpx.Timeout exposes the four phases as attributes.
        t = client.timeout
        assert t.connect is not None, "connect timeout missing"
        assert t.read is not None, "read timeout missing"
        assert t.write is not None, "write timeout missing"
        assert t.pool is not None, "pool timeout missing"
        # Defensive: numeric values must be tight enough to trip on
        # a half-open socket within a few seconds.
        assert t.connect <= 5.0, f"connect timeout too lax: {t.connect}"
        assert t.pool <= 5.0, f"pool timeout too lax: {t.pool}"
    finally:
        # Sync teardown — AsyncClient.aclose is async, but the unit
        # test only inspects sync attrs.
        del client


def test_watchdog_constant_defined():
    """Fix #3 — every loop iter must be wrapped in `wait_for(...,
    timeout=WATCHDOG_ITER_TIMEOUT_SEC)`. Constant must exist + be
    reasonable (long enough for real work, short enough to catch hangs)."""
    from external.brains.runner import WATCHDOG_ITER_TIMEOUT_SEC
    assert isinstance(WATCHDOG_ITER_TIMEOUT_SEC, (int, float))
    assert 5.0 <= WATCHDOG_ITER_TIMEOUT_SEC <= 60.0, (
        f"watchdog timeout out of band: {WATCHDOG_ITER_TIMEOUT_SEC}"
    )


def test_loops_are_split():
    """Fix #2 — three independent loops, not one fat tick body."""
    from external.brains.runner import BrainRunner
    loop_names = {"_intent_loop", "_checkin_loop", "_sovereign_loop"}
    actual = {n for n in dir(BrainRunner) if n in loop_names}
    assert actual == loop_names, f"missing split loops: {loop_names - actual}"


def test_each_loop_wraps_iter_in_wait_for():
    """All three loops must use `asyncio.wait_for(...)` around their
    iter body so a hang in one HTTP call doesn't freeze the loop.

    Doctrine: if someone removes the watchdog, we want to catch it in
    CI not in a 6-day prod outage. We inspect the source rather than
    runtime-mock because the failure mode is structural."""
    from external.brains.runner import BrainRunner
    for loop_name in ("_intent_loop", "_checkin_loop", "_sovereign_loop"):
        src = inspect.getsource(getattr(BrainRunner, loop_name))
        assert "asyncio.wait_for" in src, (
            f"{loop_name} missing watchdog (asyncio.wait_for)"
        )
        assert "WATCHDOG_ITER_TIMEOUT_SEC" in src, (
            f"{loop_name} not using the shared watchdog timeout constant"
        )


def test_stats_exposes_loop_health():
    """Fix #3 follow-on — the `BrainRunner.stats` payload must publish
    per-loop success/trip timestamps so MC's Diagnostics endpoint can
    detect a silent-hang signature."""
    from external.brains.runner import BrainRunner
    # Construct a runner without starting it — stats should be safe to
    # read at any time.
    r = BrainRunner(brain_id="alpha", display_name="Camino", token="x" * 20)
    stats = r.stats
    assert "loop_health" in stats
    lh = stats["loop_health"]
    for key in (
        "intent_last_success_age_s",
        "checkin_last_success_age_s",
        "sovereign_last_success_age_s",
        "intent_last_watchdog_trip_age_s",
        "checkin_last_watchdog_trip_age_s",
        "sovereign_last_watchdog_trip_age_s",
    ):
        assert key in lh, f"loop_health missing {key}"
        # Pre-start, all timestamps are None — that's correct.
        assert lh[key] is None


def test_no_scalar_httpx_client_construction_in_loops():
    """Regression guard — no loop may construct
    `httpx.AsyncClient(timeout=HTTP_TIMEOUT_SEC)` directly anymore.
    All HTTP clients in loop bodies must go through `_create_http_client`
    (which applies the phased timeouts + zero keep-alive doctrine)."""
    from external.brains.runner import BrainRunner
    for loop_name in ("_intent_loop", "_checkin_loop", "_sovereign_loop"):
        src = inspect.getsource(getattr(BrainRunner, loop_name))
        # The bad pattern: scalar timeout via the legacy alias.
        assert "httpx.AsyncClient(timeout=" not in src, (
            f"{loop_name} reintroduced raw httpx.AsyncClient — must "
            "use _create_http_client() for May-14 hardening"
        )
        assert "_create_http_client" in src, (
            f"{loop_name} not using _create_http_client"
        )

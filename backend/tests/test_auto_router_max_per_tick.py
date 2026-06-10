"""Per-tick rate cap regression test.

Doctrine pin (2026-06-10): `AUTO_ROUTER_MAX_PER_TICK` is NOT obsolete
just because in-flight dedupe (`shared/in_flight_orders.py`) is now
live. They solve different problems:

  * MAX_PER_TICK = rate cap (broker quota protection + operator
    visibility during a queued-intent burst).
  * in_flight_orders.claim() = duplicate prevention (stops the same
    intent from being re-picked under contention).

These tests pin the cap so a future "let's simplify" pass can't
silently remove it.
"""
import sys

sys.path.insert(0, "/app/backend")

from shared import auto_router


def test_max_per_tick_has_safe_default():
    """The hardcoded default ceiling sits well below broker quotas
    AND keeps the small-pilot trade rate observable. Going above 20
    would compromise operator-visibility during a queued burst."""
    assert auto_router.AUTO_ROUTER_MAX_PER_TICK >= 1
    assert auto_router.AUTO_ROUTER_MAX_PER_TICK <= 20


def test_max_per_tick_is_env_overridable():
    """The cap MUST remain env-tunable so the operator can poke it
    without a deploy. A future refactor that hardcodes the value
    breaks the small-pilot tightening workflow."""
    import os
    val = os.environ.get("AUTO_ROUTER_MAX_PER_TICK")
    # Confirm the module's value matches env when env is set, OR
    # falls back to the default of "5" when env is absent.
    if val is None:
        assert auto_router.AUTO_ROUTER_MAX_PER_TICK == 5
    else:
        assert auto_router.AUTO_ROUTER_MAX_PER_TICK == int(val)


def test_tick_status_exposes_max_per_tick():
    """The operator-facing /admin/auto-router/status payload must
    surface the cap so an external dashboard can show it. If a
    refactor renames or hides the key, this test catches it."""
    status = auto_router.get_status()
    assert "max_per_tick" in status
    assert status["max_per_tick"] == auto_router.AUTO_ROUTER_MAX_PER_TICK


def test_max_per_tick_is_used_in_query():
    """The intent query uses `MAX_PER_TICK * 4` for sampling. Read
    the function source to verify the cap is referenced — a future
    pass that drops the limit clause must update this test
    deliberately, not silently."""
    import inspect
    src = inspect.getsource(auto_router._tick)
    assert "AUTO_ROUTER_MAX_PER_TICK" in src, (
        "_tick must reference AUTO_ROUTER_MAX_PER_TICK to enforce the cap"
    )
    # Belt-and-braces: the loop break that stops processing at the
    # cap MUST exist.
    assert ">= AUTO_ROUTER_MAX_PER_TICK" in src, (
        "_tick must break out of the eligible-intent loop once it "
        "has processed AUTO_ROUTER_MAX_PER_TICK orders"
    )

"""Webull quotes circuit breaker — prod-incident regression tests
(2026-02-19).

Doctrine: when api.webull.com gets slow the SDK's blocking `requests`
calls tie up the asyncio default thread pool, which starves
UNRELATED async work like bcrypt password verification on
/api/auth/login. The operator's symptom: cyclic "Failed to fetch"
banners AND occasional login timeouts on prod.

The breaker trips after `_CB_FAIL_THRESHOLD` consecutive failures
and short-circuits the next `_CB_OPEN_SEC` seconds of calls to
return None immediately. A successful call closes the breaker
again.

These tests pin the breaker behavior with the SDK methods stubbed
out so we don't make real network calls.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from dotenv import load_dotenv  # noqa: E402
load_dotenv("/app/backend/.env")

from shared.market_data import webull_quotes  # noqa: E402
from shared.market_data.webull_quotes import (  # noqa: E402
    WebullQuotesClient,
    reset_quotes_client_for_tests,
    webull_quotes_breaker_status,
)


@pytest.fixture(autouse=True)
def _reset():
    """Reset breaker + cache between tests."""
    reset_quotes_client_for_tests()
    yield
    reset_quotes_client_for_tests()


def _client_with_failing_snapshot(failures: int):
    """Build a WebullQuotesClient whose snapshot call raises `failures`
    times then would-succeed (but the breaker should kick in before
    then)."""
    data = MagicMock()
    call_count = {"n": 0}

    def _snap(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] <= failures:
            raise RuntimeError(f"connect timeout (iteration {call_count['n']})")
        rv = MagicMock()
        rv.json.return_value = [{"symbol": "AAPL", "last": 150.0}]
        return rv

    data.market_data.get_snapshot = _snap
    trade = MagicMock()
    return WebullQuotesClient(data=data, trade=trade), call_count


def test_breaker_opens_after_threshold_failures():
    """3 consecutive failures → breaker opens → subsequent calls
    short-circuit to None WITHOUT invoking the SDK."""
    client, call_count = _client_with_failing_snapshot(failures=10)

    # 3 failures (threshold) → breaker should now be open.
    for _ in range(3):
        assert client.equity_snapshot("AAPL") is None
    assert call_count["n"] == 3

    status = webull_quotes_breaker_status()
    assert status["open"] is True
    assert status["consecutive_failures"] >= 3

    # While open, calls return None WITHOUT touching the SDK.
    for _ in range(5):
        assert client.equity_snapshot("AAPL") is None
    assert call_count["n"] == 3, (
        f"breaker leaked: SDK was called {call_count['n']} times despite "
        "being open"
    )


def test_breaker_recovers_after_cooldown(monkeypatch):
    """After the cool-down window, the next call goes through. A
    success closes the breaker and resets the failure counter."""
    # Shrink the cool-down so the test runs fast.
    monkeypatch.setattr(webull_quotes, "_CB_OPEN_SEC", 0.5)
    # Force the breaker's internal lock to honour the shorter window
    # by rebuilding it via the public reset and then-failing path.
    reset_quotes_client_for_tests()

    client, call_count = _client_with_failing_snapshot(failures=3)

    # Trip the breaker.
    for _ in range(3):
        assert client.equity_snapshot("AAPL") is None
    assert webull_quotes_breaker_status()["open"] is True

    # Wait past the cool-down.
    time.sleep(0.6)
    assert webull_quotes_breaker_status()["open"] is False

    # The 4th SDK invocation succeeds (failures=3 in the stub), and
    # the breaker should record success + close.
    rv = client.equity_snapshot("AAPL")
    assert rv == {"symbol": "AAPL", "last": 150.0}
    status = webull_quotes_breaker_status()
    assert status["open"] is False
    assert status["consecutive_failures"] == 0


def test_success_resets_failure_counter():
    """A success after 2 failures (below threshold) resets the
    counter so the next failure starts from 0."""
    data = MagicMock()
    state = {"i": 0, "fail_pattern": [True, True, False, True, True, True]}

    def _snap(symbols, category):
        if state["i"] >= len(state["fail_pattern"]):
            raise RuntimeError("end of pattern")
        should_fail = state["fail_pattern"][state["i"]]
        state["i"] += 1
        if should_fail:
            raise RuntimeError(f"fail at i={state['i']-1}")
        rv = MagicMock()
        rv.json.return_value = [{"symbol": symbols[0]}]
        return rv

    data.market_data.get_snapshot = _snap
    client = WebullQuotesClient(data=data, trade=MagicMock())

    # Use different symbols on each call so we exercise the SDK
    # every time (cache hits would mask the failure-pattern logic).
    # Steps 0-1: failures (below threshold, breaker stays closed)
    assert client.equity_snapshot("AAA") is None
    assert client.equity_snapshot("BBB") is None
    assert webull_quotes_breaker_status()["open"] is False
    assert webull_quotes_breaker_status()["consecutive_failures"] == 2

    # Step 2: success → counter resets to 0
    assert client.equity_snapshot("CCC") is not None
    assert webull_quotes_breaker_status()["consecutive_failures"] == 0

    # Steps 3-4: 2 more failures — breaker should still be closed
    # (counter started from 0).
    assert client.equity_snapshot("DDD") is None
    assert client.equity_snapshot("EEE") is None
    assert webull_quotes_breaker_status()["open"] is False


def test_cache_hit_bypasses_breaker():
    """A cached value short-circuits BEFORE the breaker check —
    we should never even ask the breaker if we have fresh data."""
    data = MagicMock()
    rv = MagicMock()
    rv.json.return_value = [{"symbol": "AAPL", "last": 150.0}]
    data.market_data.get_snapshot = MagicMock(return_value=rv)
    client = WebullQuotesClient(data=data, trade=MagicMock())

    # Prime the cache.
    first = client.equity_snapshot("AAPL")
    assert first == {"symbol": "AAPL", "last": 150.0}
    assert data.market_data.get_snapshot.call_count == 1

    # Manually trip the breaker.
    for _ in range(5):
        webull_quotes._BREAKER.record_failure("manual")

    # Cached call should still return the value WITHOUT consulting
    # the breaker or the SDK.
    second = client.equity_snapshot("AAPL")
    assert second == {"symbol": "AAPL", "last": 150.0}
    assert data.market_data.get_snapshot.call_count == 1  # no new call


def test_status_shape():
    """Diagnostics endpoint will surface this — pin the schema."""
    status = webull_quotes_breaker_status()
    assert set(status.keys()) >= {
        "open", "consecutive_failures", "open_for_seconds",
        "threshold", "cool_down_seconds",
    }
    assert isinstance(status["open"], bool)
    assert isinstance(status["consecutive_failures"], int)
    assert isinstance(status["open_for_seconds"], float)

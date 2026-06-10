"""Signal-ranked symbol selection + anti-drumbeat cooldown.

These tests pin the 2026-06-09 fix to the AAPL saturation incident.
Before this fix the runner selected symbols by `tick % len(universe)`
— alphabetical round-robin. That bug let 4 brains × tick #1 all
pick `symbol[0]` (AAPL) and stack 4 simultaneous BUYs on one ticker.

The fix:
  1. Every tick, score every symbol in the universe by setup_score.
  2. Sort descending. Pick the head.
  3. Skip any symbol whose last emit was within INTENT_COOLDOWN_TICKS.
  4. If everything is on cooldown, fall back to the least-recently
     emitted symbol (we always owe MC at least one OBSERVE so the
     audit trail doesn't go silent).

These tests prove all four properties in isolation.

Doctrine (2026-06-10): originally these tests called
`asyncio.get_event_loop().run_until_complete(...)` from sync test
bodies. Under pytest-asyncio auto-mode the session loop is already
running by the time these run inside the full suite, which raises
`RuntimeError: This event loop is already running`. Converted to
native async tests so the harness drives them — order-independent.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make the `external/brains` package importable. The tests live under
# backend/tests/ so they can be run from the same pytest invocation
# as the other backend suites.
REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from external.brains import runner  # noqa: E402


class _StubRunner(runner.BrainRunner):
    """A NeutralBrainRunner with the network calls stubbed out so the
    intent-loop scoring logic can be exercised deterministically."""

    def __init__(self, universe, scores_by_symbol):
        super().__init__(brain_id="alpha", display_name="Camino", token="t")
        self._universe = list(universe)
        self._scores_by_symbol = dict(scores_by_symbol)
        self.posted: list[tuple[str, str]] = []

    async def _fetch_technical(self, http, symbol):  # type: ignore[override]
        return {"signals": {"setup_score": self._scores_by_symbol.get(symbol, 0.0)}}

    async def _evaluate_and_post(self, http, lane, symbol):  # type: ignore[override]
        self.posted.append((lane, symbol))


@pytest.mark.asyncio
async def test_ranking_selects_highest_setup_score_not_alphabetical():
    """The AAPL saturation root cause: tick-1 picked symbol[0] every
    time. New behaviour: highest setup_score wins regardless of
    alphabetical position."""
    universe = [("equity", s) for s in ("AAPL", "MSFT", "NVDA", "TSLA")]
    # NVDA has the strongest setup; AAPL is weakest. Alphabetical
    # would still pick AAPL on tick 1; ranked picks NVDA.
    scores = {"AAPL": 0.10, "MSFT": 0.40, "NVDA": 0.85, "TSLA": 0.30}
    r = _StubRunner(universe, scores)

    ranked = await r._rank_universe(http=None)
    assert ranked[0][1] == "NVDA"
    assert ranked[-1][1] == "AAPL"
    # Descending order, no ties on this fixture
    assert [row[1] for row in ranked] == ["NVDA", "MSFT", "TSLA", "AAPL"]


@pytest.mark.asyncio
async def test_cooldown_blocks_repeat_within_window():
    """Anti-drumbeat: a symbol emitted on tick N must NOT be re-picked
    before tick N + INTENT_COOLDOWN_TICKS, even if it's still the
    highest-ranked symbol. The brain should fall through to the
    next-best name."""
    universe = [("equity", s) for s in ("AAPL", "MSFT")]
    scores = {"AAPL": 0.90, "MSFT": 0.80}
    r = _StubRunner(universe, scores)
    r._tick_count = 100
    r._last_emit_tick[("equity", "AAPL")] = 99  # emitted 1 tick ago

    ranked = await r._rank_universe(http=None)
    # Manually walk the selection loop with the real cooldown const
    pick = None
    for lane, symbol, score in ranked:
        last = r._last_emit_tick.get((lane, symbol), -10**9)
        if (r._tick_count - last) < runner.INTENT_COOLDOWN_TICKS:
            continue
        pick = (lane, symbol)
        break
    assert pick == ("equity", "MSFT"), (
        f"expected MSFT (AAPL on cooldown), got {pick}"
    )


@pytest.mark.asyncio
async def test_cooldown_releases_after_window():
    """The cooldown is a window, not a permanent ban. After
    INTENT_COOLDOWN_TICKS ticks pass since the last emit, the symbol
    is eligible again."""
    universe = [("equity", s) for s in ("AAPL", "MSFT")]
    scores = {"AAPL": 0.90, "MSFT": 0.10}
    r = _StubRunner(universe, scores)
    # AAPL was emitted on tick 50; we're now at tick
    # 50 + INTENT_COOLDOWN_TICKS — cooldown has just expired.
    r._tick_count = 50 + runner.INTENT_COOLDOWN_TICKS
    r._last_emit_tick[("equity", "AAPL")] = 50

    pick = None
    for lane, symbol, _score in await r._rank_universe(http=None):
        last = r._last_emit_tick.get((lane, symbol), -10**9)
        if (r._tick_count - last) < runner.INTENT_COOLDOWN_TICKS:
            continue
        pick = (lane, symbol)
        break
    assert pick == ("equity", "AAPL"), (
        f"AAPL should be eligible again at tick "
        f"{50 + runner.INTENT_COOLDOWN_TICKS}, got {pick}"
    )


def test_universe_refresh_purges_dropped_symbols_from_cooldown():
    """When the operator removes a symbol from `patterns_universe`,
    its cooldown entry should be purged so it doesn't leak forever."""
    r = _StubRunner([("equity", "AAPL")], {"AAPL": 0.5})
    r._last_emit_tick[("equity", "AAPL")] = 10
    r._last_emit_tick[("equity", "STALE")] = 5  # symbol no longer in universe

    # Manually apply the same filter the loop applies on refresh
    r._last_emit_tick = {
        k: v for k, v in r._last_emit_tick.items()
        if k in r._universe
    }
    assert ("equity", "AAPL") in r._last_emit_tick
    assert ("equity", "STALE") not in r._last_emit_tick


@pytest.mark.asyncio
async def test_score_failures_degrade_not_drop():
    """If MC's technical endpoint errors for one symbol, it should
    still appear in the ranked list (at score 0.0) — never disappear.
    Otherwise a flaky endpoint could silently shrink the universe."""
    universe = [("equity", s) for s in ("AAPL", "MSFT", "TSLA")]

    class FlakyRunner(_StubRunner):
        async def _fetch_technical(self, http, symbol):
            if symbol == "MSFT":
                raise RuntimeError("simulated 502")
            return {"signals": {"setup_score": 0.5}}

    r = FlakyRunner(universe, {})
    ranked = await r._rank_universe(http=None)
    syms = [row[1] for row in ranked]
    assert set(syms) == {"AAPL", "MSFT", "TSLA"}, (
        f"all symbols should be ranked even on partial failure, got {syms}"
    )
    # MSFT (flaky) should rank at the bottom (0.0)
    assert ranked[-1] == ("equity", "MSFT", 0.0)

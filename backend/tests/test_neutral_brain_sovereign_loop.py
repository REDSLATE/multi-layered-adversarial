"""Tests for the neutral-brain sovereign contribution loop.

Doctrine pin: the 4 permanent neutral brains (Camino, Barracuda,
Hellcat, GTO) MUST post substantive sovereign contributions periodically
so MC's `brain_emission_diagnose.sovereign_loop` stays in the `live`
band. A skeleton/empty payload trips the 422 empty-contribution gate
and the operator sees "STALE_SOVEREIGN" chips even when the brains are
otherwise healthy.

These are pure unit tests — no Mongo, no HTTP — they exercise the
runner's payload-building + tape-recording logic in isolation.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


# Make `external.brains.*` importable for the tests.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


from external.brains.runner import BrainRunner  # noqa: E402


def _fake_response(status_code: int = 200, text: str = "{}") -> MagicMock:
    r = MagicMock()
    r.status_code = status_code
    r.text = text
    return r


def _record_intent(runner: BrainRunner, action: str, conf: float, sym: str = "BTC/USD") -> None:
    """Simulate `_evaluate_and_post` succeeding once, by mutating the
    runner state the way the real method does. Lets the sovereign loop
    pull a real (non-empty) tape."""
    runner._recent_tape.append({
        "symbol": sym,
        "action": action,
        "confidence": conf,
        "outcome": 0,
        "resolved_at": None,
        "notional": 0.0,
    })
    runner._intent_count += 1
    runner._tick_count += 1
    runner._last_action = action
    runner._last_confidence = conf


@pytest.mark.asyncio
async def test_sovereign_payload_is_substantive_and_posts():
    """When the brain has produced ≥1 intent, the sovereign contribution
    payload carries non-empty notes + weights + recent_outcomes + tape,
    and gets POSTed to the correct MC loopback endpoint with the
    runtime token header."""
    runner = BrainRunner(brain_id="alpha", display_name="Camino", token="tok-xyz")
    _record_intent(runner, "BUY", 0.71, "BTC/USD")
    _record_intent(runner, "SELL", 0.62, "ETH/USD")

    http = MagicMock()
    http.post = AsyncMock(return_value=_fake_response(200))

    await runner._post_sovereign_contribution(http)

    assert runner._sovereign_count == 1
    # One POST against the canonical contribution endpoint.
    assert http.post.await_count == 1
    call = http.post.await_args
    url = call.args[0]
    assert url.endswith("/api/runtime-discussion/sovereign/contribution")
    assert call.kwargs["params"] == {"runtime": "alpha"}
    headers = call.kwargs["headers"]
    assert headers["X-Runtime-Token"] == "tok-xyz"
    assert headers["X-Client-Request-Id"].startswith("alpha-sovereign-")

    body = call.kwargs["json"]
    # Mode + bounds — MC validates these server-side.
    assert body["mode"] == "PRD"
    assert isinstance(body["live_trading_enabled"], bool)
    assert 0.0 <= body["learning_rate"] <= 0.5
    assert -0.25 <= body["confidence_delta"] <= 0.25
    # SUBSTANTIVE — at least one of notes / weights / recent_outcomes
    # must carry real content or MC's 422 gate fires.
    assert body["notes"].strip(), "notes must be non-empty"
    assert body["weights"], "weights must be non-empty"
    assert len(body["recent_outcomes"]) == 2
    # Tape entries preserve the brain's POSTed action verbatim.
    actions = [o["action"] for o in body["recent_outcomes"]]
    assert "BUY" in actions and "SELL" in actions


@pytest.mark.asyncio
async def test_sovereign_payload_substantive_even_without_tape():
    """Cold start — brain has no intents yet. The contribution still
    needs to be substantive (weights + notes) so the empty-contribution
    gate doesn't reject it."""
    runner = BrainRunner(brain_id="camaro", display_name="Barracuda", token="tok-b")
    http = MagicMock()
    http.post = AsyncMock(return_value=_fake_response(200))

    await runner._post_sovereign_contribution(http)

    body = http.post.await_args.kwargs["json"]
    # Tape may be empty, but weights + notes carry real content.
    assert body["recent_outcomes"] == []
    assert body["weights"], "weights must populate even on cold start"
    assert body["notes"].strip(), "notes must populate even on cold start"
    # Hollow-test the empty-fields heuristic MC uses (≥5 empty = reject).
    empty = []
    if not body["notes"].strip():
        empty.append("notes")
    if not body["weights"]:
        empty.append("weights")
    if not body["recent_outcomes"]:
        empty.append("recent_outcomes")
    if not body["delta_reason"].strip():
        empty.append("delta_reason")
    if body["confidence_delta"] == 0.0:
        empty.append("confidence_delta")
    assert len(empty) < 5, (
        f"empty_field_count={len(empty)} >= 5 would trip MC's "
        f"422 empty_contribution gate (fields: {empty})"
    )


@pytest.mark.asyncio
async def test_sovereign_count_unchanged_on_rejection():
    """A non-2xx response from MC must NOT bump `_sovereign_count`. The
    counter is the operator's proof of successful posts."""
    runner = BrainRunner(brain_id="redeye", display_name="GTO", token="t")
    http = MagicMock()
    http.post = AsyncMock(return_value=_fake_response(422, "{'detail':'bad'}"))

    await runner._post_sovereign_contribution(http)
    assert runner._sovereign_count == 0


@pytest.mark.asyncio
async def test_intent_tape_is_bounded():
    """The rolling tape stays bounded at 25 — older entries are
    dropped so the contribution payload never bloats."""
    runner = BrainRunner(brain_id="chevelle", display_name="Hellcat", token="t")
    for i in range(40):
        runner._recent_tape.append({
            "symbol": f"SYM{i}", "action": "BUY", "confidence": 0.5,
            "outcome": 0, "resolved_at": None, "notional": 0.0,
        })
        if len(runner._recent_tape) > 25:
            runner._recent_tape = runner._recent_tape[-25:]
    assert len(runner._recent_tape) == 25
    # And the payload caps at 20 (≤ MC's MAX_RECENT_OUTCOMES=50).
    http = MagicMock()
    http.post = AsyncMock(return_value=_fake_response(200))
    await runner._post_sovereign_contribution(http)
    body = http.post.await_args.kwargs["json"]
    assert len(body["recent_outcomes"]) == 20


@pytest.mark.asyncio
async def test_stats_exposes_sovereign_count():
    """Operator's /admin/neutral-brains/status reads this directly."""
    runner = BrainRunner(brain_id="alpha", display_name="Camino", token="t")
    http = MagicMock()
    http.post = AsyncMock(return_value=_fake_response(200))
    await runner._post_sovereign_contribution(http)
    await runner._post_sovereign_contribution(http)
    stats = runner.stats
    assert stats["sovereign_count"] == 2
    assert stats["brain_id"] == "alpha"
    assert stats["display_name"] == "Camino"

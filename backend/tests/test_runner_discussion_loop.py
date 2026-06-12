"""Tests for the cross-brain discussion loop (P1, 2026-02-19).

Operator directive: solo opinions are healthy (every brain posts 200
OK on every intent), but ZERO `in_reply_to` rows ever land — brains
monologue, never react to peers. Scope chosen by main agent's default
recommendation: LIGHTWEIGHT DISSENT-ONLY. The loop posts a `disagree`
reply ONLY when a peer's stance directly contradicts this brain's
most-recent stance on the same symbol.

These tests pin:
  * Direct contradictions (`long`↔`short`, `long`↔`veto`,
    `short`↔`veto`) generate exactly one dissent reply.
  * Concurrence (`long`↔`long`) is silent.
  * Missing own-stance is silent (no monologue from peer alone).
  * Self-authored peer rows are skipped.
  * Already-replied peer rows are skipped (idempotency).
  * Non-`symbol:` topics are skipped (no contradiction model).
  * Replies cap at DISCUSSION_MAX_REPLIES_PER_TICK per iteration.
  * Reply payload has correct shape: stance=disagree, in_reply_to,
    runtime=self, topic mirrors peer.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from external.brains.runner import (  # noqa: E402
    DISCUSSION_MAX_REPLIES_PER_TICK,
    BrainRunner,
    _CONFLICTING_PAIRS,
)


# ── helpers ───────────────────────────────────────────────────────


def _peer_opinion(
    opinion_id: str,
    runtime: str,
    symbol: str,
    stance: str,
    confidence: float = 0.7,
) -> dict:
    return {
        "opinion_id": opinion_id,
        "runtime": runtime,
        "topic": f"symbol:{symbol}",
        "stance": stance,
        "confidence": confidence,
        "body": f"{runtime} says {stance} on {symbol}",
        "posted_at": datetime.now(timezone.utc).isoformat(),
    }


class _FakeResponse:
    """Stand-in for httpx.Response."""

    def __init__(self, status_code: int = 200, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = ""

    def json(self) -> dict:
        return self._payload


class _FakeHttp:
    """Records every GET + POST so tests can assert exact requests."""

    def __init__(self, get_payload: dict):
        self.get_payload = get_payload
        self.gets: list[tuple] = []
        self.posts: list[dict] = []

    async def get(self, url, *, params=None, headers=None, **_kw):
        self.gets.append((url, params, headers))
        return _FakeResponse(200, self.get_payload)

    async def post(self, url, *, json=None, headers=None, **_kw):
        # Record the parsed body so assertions are clean.
        self.posts.append({"url": url, "body": json, "headers": headers})
        return _FakeResponse(200, {"opinion_id": "REPLY-OK"})


def _runner(brain_id: str = "alpha") -> BrainRunner:
    return BrainRunner(brain_id=brain_id, display_name="Camino", token="tok-test")


def _seed_my_stance(r: BrainRunner, symbol: str, stance: str) -> None:
    r._my_last_stance_by_symbol[symbol.upper()] = (
        stance, datetime.now(timezone.utc),
    )


# ── core dissent behavior ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_dissent_when_peer_short_and_we_are_long():
    """The whole reason the loop exists: peer says short, we say long
    → exactly one `disagree` reply with `in_reply_to` pointing at
    the peer's opinion_id."""
    r = _runner("alpha")
    _seed_my_stance(r, "NVDA", "long")
    http = _FakeHttp({
        "items": [_peer_opinion("OP-1", "camaro", "NVDA", "short")],
    })

    await r._discussion_tick(http)

    assert len(http.posts) == 1, "exactly one reply expected"
    body = http.posts[0]["body"]
    assert body["stance"] == "disagree"
    assert body["in_reply_to"] == "OP-1"
    assert body["topic"] == "symbol:NVDA"
    assert body["runtime"] == "alpha"
    assert body["may_execute"] is False
    # Evidence carries the peer's context for the audit trail.
    assert body["evidence"]["peer_runtime"] == "camaro"
    assert body["evidence"]["peer_stance"] == "short"
    assert body["evidence"]["my_stance"] == "long"
    # Counter incremented + idempotency record set.
    assert r._discussion_reply_count == 1
    assert "OP-1" in r._replied_to_opinion_ids


@pytest.mark.asyncio
async def test_dissent_when_peer_long_and_we_are_short():
    """Reverse direction also conflicts."""
    r = _runner("alpha")
    _seed_my_stance(r, "AAPL", "short")
    http = _FakeHttp({
        "items": [_peer_opinion("OP-2", "chevelle", "AAPL", "long")],
    })
    await r._discussion_tick(http)
    assert len(http.posts) == 1
    assert http.posts[0]["body"]["stance"] == "disagree"


@pytest.mark.asyncio
async def test_dissent_when_peer_veto_and_we_are_long():
    """Veto is a contradicting stance for directional intents."""
    r = _runner("alpha")
    _seed_my_stance(r, "TSLA", "long")
    http = _FakeHttp({
        "items": [_peer_opinion("OP-3", "redeye", "TSLA", "veto")],
    })
    await r._discussion_tick(http)
    assert len(http.posts) == 1
    assert http.posts[0]["body"]["stance"] == "disagree"


# ── silence cases ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_concurrence_is_silent():
    """Both long on same symbol → no reply."""
    r = _runner("alpha")
    _seed_my_stance(r, "MSFT", "long")
    http = _FakeHttp({
        "items": [_peer_opinion("OP-4", "camaro", "MSFT", "long")],
    })
    await r._discussion_tick(http)
    assert len(http.posts) == 0
    assert r._discussion_reply_count == 0


@pytest.mark.asyncio
async def test_no_own_stance_is_silent():
    """No own stance on the symbol → silent (concurrence-by-default)."""
    r = _runner("alpha")
    # Note: deliberately NOT seeding _my_last_stance_by_symbol
    http = _FakeHttp({
        "items": [_peer_opinion("OP-5", "camaro", "AMD", "short")],
    })
    await r._discussion_tick(http)
    assert len(http.posts) == 0


@pytest.mark.asyncio
async def test_skips_self_authored():
    """Brain never replies to its own opinion."""
    r = _runner("alpha")
    _seed_my_stance(r, "AAPL", "long")
    http = _FakeHttp({
        "items": [_peer_opinion("OP-6", "alpha", "AAPL", "short")],  # self
    })
    await r._discussion_tick(http)
    assert len(http.posts) == 0


@pytest.mark.asyncio
async def test_skips_already_replied_to():
    """Idempotency — a second tick that surfaces the same peer
    opinion must NOT generate a second reply."""
    r = _runner("alpha")
    _seed_my_stance(r, "NVDA", "long")
    peer_op = _peer_opinion("OP-7", "camaro", "NVDA", "short")
    http = _FakeHttp({"items": [peer_op]})

    await r._discussion_tick(http)
    await r._discussion_tick(http)  # second pass — same payload

    assert len(http.posts) == 1, (
        "second tick must skip already-replied-to opinion_id"
    )


@pytest.mark.asyncio
async def test_skips_non_symbol_topics():
    """Non-symbol topics (regime, theory, free) carry no directional
    contradiction model — skip them entirely."""
    r = _runner("alpha")
    _seed_my_stance(r, "NVDA", "long")
    http = _FakeHttp({"items": [
        {"opinion_id": "OP-R1", "runtime": "camaro",
         "topic": "regime:trend", "stance": "long",
         "confidence": 0.7, "body": "trend regime", "posted_at": ""},
        {"opinion_id": "OP-T1", "runtime": "redeye",
         "topic": "theory:momentum_decay", "stance": "short",
         "confidence": 0.6, "body": "decay theory", "posted_at": ""},
    ]})
    await r._discussion_tick(http)
    assert len(http.posts) == 0


# ── throttle ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_throttles_to_max_replies_per_tick():
    """A burst of N conflicting peer opinions must cap at
    DISCUSSION_MAX_REPLIES_PER_TICK replies per tick — the
    overflow gets picked up on subsequent ticks (or simply ages
    out of the lookback window)."""
    r = _runner("alpha")
    # Seed conflicting own stances on 10 distinct symbols.
    symbols = [f"SYM{i}" for i in range(10)]
    for s in symbols:
        _seed_my_stance(r, s, "long")
    items = [
        _peer_opinion(f"OP-{i}", "camaro", symbols[i], "short")
        for i in range(10)
    ]
    http = _FakeHttp({"items": items})
    await r._discussion_tick(http)
    # Exactly the throttle cap.
    assert len(http.posts) == DISCUSSION_MAX_REPLIES_PER_TICK
    assert r._discussion_reply_count == DISCUSSION_MAX_REPLIES_PER_TICK


# ── doctrine invariants ───────────────────────────────────────────


def test_conflicting_pairs_is_locked_to_directional_only():
    """Doctrine pin — the contradiction set MUST stay restricted to
    directional pairs. Adding e.g. (`observation`, `long`) would turn
    the loop into a herd-detection bot, which is exactly what the
    operator's scope explicitly rejected."""
    assert ("long", "short") in _CONFLICTING_PAIRS
    assert ("short", "long") in _CONFLICTING_PAIRS
    assert ("long", "veto") in _CONFLICTING_PAIRS
    # Observations / agreements / refinements MUST NOT trigger
    # contradiction — they're meta-statements, not stances.
    assert ("observation", "long") not in _CONFLICTING_PAIRS
    assert ("agree", "disagree") not in _CONFLICTING_PAIRS
    assert ("long", "long") not in _CONFLICTING_PAIRS


def test_post_directional_opinion_stance_tracking_is_directional_only():
    """`_post_directional_opinion` must only populate
    `_my_last_stance_by_symbol` for `long`/`short` stances — HOLD's
    `observation` stance must NOT seed contradiction detection (it
    would generate false-positive dissents)."""
    import inspect
    src = inspect.getsource(BrainRunner._post_directional_opinion)
    assert 'stance in ("long", "short")' in src, (
        "stance tracking must be gated to directional stances only"
    )


@pytest.mark.asyncio
async def test_get_request_uses_runtime_token_and_caller():
    """The reader endpoint requires both X-Runtime-Token header and a
    `caller` query param. Verify the loop sends them."""
    r = _runner("alpha")
    http = _FakeHttp({"items": []})
    await r._discussion_tick(http)
    assert len(http.gets) == 1
    url, params, headers = http.gets[0]
    assert "runtime-discussion/opinions" in url
    assert params["caller"] == "alpha"
    assert "since" in params
    assert headers["X-Runtime-Token"] == "tok-test"


def test_discussion_loop_wired_into_run():
    """Sanity — the new `_discussion_loop` task must be wired into
    `run()` next to the existing intent/checkin/sovereign tasks, with
    the operator kill-switch gate."""
    import inspect
    src = inspect.getsource(BrainRunner.run)
    assert "_discussion_loop" in src, (
        "discussion_loop task must be spawned by run()"
    )
    assert "DISCUSSION_LOOP_ENABLED" in src, (
        "the kill-switch env knob must gate task creation so the "
        "operator can disable it without code change"
    )

"""Opinion ingest latency guards — doctrine tripwires (2026-05-28, pass #22).

Background:
    Chevelle-author reported 504s from `/api/ingest/opinion` under load:
    "MC /api/ingest/opinion returned 504: hard wall-clock deadline 10.0s
    exceeded". Root cause: anchor-price capture synchronously called
    Alpaca's get_latest_trade (equity) / Kraken's public ticker
    (crypto), and slow broker responses pushed the request past the
    platform ingress 10s deadline. Same risk in conflict auto-detect
    under high opinion-volume load.

These tripwires lock the fix so it can't silently get refactored away:

  1. `asyncio.wait_for` MUST wrap the anchor-price fetch
  2. `asyncio.wait_for` MUST wrap the conflict-detect call
  3. Both bounded timeouts MUST be read from env with sensible defaults
  4. The opinion MUST still post when either bounded call times out
"""
from __future__ import annotations

import asyncio
import inspect
import os
import re
import uuid

import pytest


pytestmark = [pytest.mark.tripwire]


# ──────────────────────── SOURCE-SCAN INVARIANTS ────────────────────────


def _opinions_src() -> str:
    from shared import opinions
    return inspect.getsource(opinions)


def test_anchor_fetch_is_wrapped_in_asyncio_wait_for():
    """The anchor-price capture path MUST be bounded by asyncio.wait_for
    so a slow broker cannot stall the opinion POST past the ingress
    10s deadline."""
    src = _opinions_src()
    # Look for `_fetch_current_price` invocation inside a wait_for(...)
    # call. The wrap can be `await asyncio.wait_for(_fetch_current_price(...)`
    # — anything tighter wouldn't be useful here.
    pattern = re.compile(
        r"asyncio\.wait_for\(\s*_fetch_current_price\(",
        re.MULTILINE,
    )
    assert pattern.search(src), (
        "DOCTRINE VIOLATION: anchor-price fetch in shared/opinions.py is "
        "no longer wrapped in asyncio.wait_for(). A slow broker will "
        "now block the opinion POST past the platform 10s deadline → "
        "504 cascades, brain silence, gate-chain stall."
    )


def test_conflict_detect_is_wrapped_in_asyncio_wait_for():
    """Conflict detection MUST be bounded by asyncio.wait_for too.
    Under high opinion-volume load the mongo round-trips can exceed
    the ingress deadline."""
    src = _opinions_src()
    pattern = re.compile(
        r"asyncio\.wait_for\(\s*detect_conflicts_for_opinion\(",
        re.MULTILINE,
    )
    assert pattern.search(src), (
        "DOCTRINE VIOLATION: conflict-detect in shared/opinions.py is "
        "no longer wrapped in asyncio.wait_for(). High-volume opinion "
        "posting can stall the request past the ingress deadline."
    )


def test_timeouts_come_from_env_with_defaults():
    """Both timeouts MUST be env-tuneable so the operator can dial
    them without a code redeploy. Defaults MUST exist."""
    src = _opinions_src()
    assert "OPINION_ANCHOR_FETCH_TIMEOUT_SEC" in src, (
        "OPINION_ANCHOR_FETCH_TIMEOUT_SEC env knob removed"
    )
    assert "OPINION_CONFLICT_DETECT_TIMEOUT_SEC" in src, (
        "OPINION_CONFLICT_DETECT_TIMEOUT_SEC env knob removed"
    )


def test_timeouts_default_below_ingress_deadline():
    """Both default timeouts must be well below the platform's 10s
    ingress wall-clock deadline. Sum of both defaults must leave
    budget for the mongo insert + mirror + auth + JSON parse."""
    from shared import opinions  # noqa: F401 — ensure module import side-effects
    anchor_default = float(os.environ.get(
        "OPINION_ANCHOR_FETCH_TIMEOUT_SEC", "1.5",
    ))
    conflict_default = float(os.environ.get(
        "OPINION_CONFLICT_DETECT_TIMEOUT_SEC", "2.0",
    ))
    assert anchor_default <= 3.0, (
        f"anchor timeout {anchor_default}s is too lax; raise breaks 10s budget"
    )
    assert conflict_default <= 3.0, (
        f"conflict timeout {conflict_default}s is too lax"
    )
    # Combined hard ceiling — must leave at least 5s for the rest.
    assert anchor_default + conflict_default <= 5.0


# ──────────────────────── BEHAVIORAL INVARIANTS ────────────────────────


@pytest.mark.asyncio
async def test_post_opinion_completes_when_anchor_fetch_hangs(monkeypatch):
    """When `_fetch_current_price` hangs forever, the POST MUST still
    return (anchor_price simply not stamped on the doc)."""
    from shared import opinions as opinions_mod
    from shared import opinion_resolver

    async def _hang_forever(*_a, **_kw):
        await asyncio.sleep(60)  # well past the bounded timeout
        return 100.0  # never reached

    # Hijack the resolver's price fetch with a hanging coroutine.
    monkeypatch.setattr(
        opinion_resolver, "_fetch_current_price", _hang_forever,
    )
    # Tight timeout so the test finishes fast.
    monkeypatch.setenv("OPINION_ANCHOR_FETCH_TIMEOUT_SEC", "0.2")

    # Bypass the runtime-token verify by patching it to pass.
    monkeypatch.setattr(
        opinions_mod, "verify_runtime_token", lambda *a, **kw: None,
    )

    body = opinions_mod.OpinionIn(
        runtime="alpha",
        topic="symbol:_HANGTEST_",
        stance="long",
        confidence=0.5,
        body="anchor-fetch hang tripwire",
        evidence={},
        in_reply_to=None,
        regime=None,
        may_execute=False,
    )

    # Race the handler against a generous test-side timeout. If the
    # bounded wait_for is gone, this test will time out (failing
    # loudly) instead of silently passing.
    out = await asyncio.wait_for(
        opinions_mod.post_opinion(body=body, x_runtime_token="dummy"),
        timeout=5.0,
    )
    assert out.get("ok") is True
    assert "opinion_id" in out

    # The persisted doc must NOT have an anchor_price (fetch timed out).
    from db import db
    from namespaces import SHARED_OPINIONS
    row = await db[SHARED_OPINIONS].find_one(
        {"opinion_id": out["opinion_id"]}, {"_id": 0},
    )
    assert row is not None
    assert row.get("anchor_price") is None, (
        "anchor_price stamped despite hanging fetch — "
        "the wait_for bound is missing"
    )

    await db[SHARED_OPINIONS].delete_one({"opinion_id": out["opinion_id"]})


@pytest.mark.asyncio
async def test_post_opinion_completes_when_conflict_detect_hangs(monkeypatch):
    """Conflict detection hanging MUST NOT block the post — the
    response carries `conflicts_detected=[]` and moves on."""
    from shared import conflicts as conflicts_mod
    from shared import opinions as opinions_mod

    async def _hang(*_a, **_kw):
        await asyncio.sleep(60)
        return []  # never reached

    monkeypatch.setattr(
        conflicts_mod, "detect_conflicts_for_opinion", _hang,
    )
    monkeypatch.setenv("OPINION_CONFLICT_DETECT_TIMEOUT_SEC", "0.2")
    monkeypatch.setattr(
        opinions_mod, "verify_runtime_token", lambda *a, **kw: None,
    )

    body = opinions_mod.OpinionIn(
        runtime="alpha",
        topic="symbol:_CONFLICTHANG_",
        stance="observation",
        confidence=0.5,
        body="conflict-detect hang tripwire",
        evidence={},
        in_reply_to=None,
        regime=None,
        may_execute=False,
    )
    out = await asyncio.wait_for(
        opinions_mod.post_opinion(body=body, x_runtime_token="dummy"),
        timeout=5.0,
    )
    assert out.get("ok") is True
    assert out.get("conflicts_detected") == []

    from db import db
    from namespaces import SHARED_OPINIONS
    await db[SHARED_OPINIONS].delete_one({"opinion_id": out["opinion_id"]})

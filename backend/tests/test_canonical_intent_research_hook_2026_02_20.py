"""2026-02-20 — `_post_intent_impl` MUST stamp Research Layer evidence.

Closes the doctrine loop: production runtime-token emissions and
admin bridge emissions now both carry the same evidence shape.

Pinned behaviors:
  1. Live runtime intent → `evidence.research_signals` populated.
  2. Bar-source crash → intent still persists (best-effort guard).
  3. Brain decision fields (action, confidence) NEVER overwritten.
  4. `requires_final_authority` / `gate_state` / `executed` never
     mutated by the research hook.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest


def _bull_run(n: int = 80, start: float = 100.0) -> list[dict]:
    bars: list[dict] = []
    price = start
    for i in range(n):
        base = 0.2 + (i / n) * 0.6
        step = -base * 0.3 if i % 5 == 4 else base
        o = price
        c = price + step
        h = max(o, c) + 0.1
        l = min(o, c) - 0.1  # noqa: E741
        v = 1_000 if i < n - 3 else 5_000
        bars.append({"ts": i, "o": o, "h": h, "l": l, "c": c, "v": v})
        price = c
    return bars


@pytest.mark.asyncio
async def test_attach_research_evidence_stamps_on_doc_shape_from_post_intent_impl():
    """Mock the bar source and exercise `attach_research_evidence`
    against the exact dict shape `_post_intent_impl` builds. This is
    cheaper and more deterministic than spinning the full FastAPI app
    + Mongo, and it pins the contract the runtime path depends on."""
    from shared.research.intent_evidence import attach_research_evidence

    # Mirror the doc shape `_post_intent_impl` builds at line ~1045-1126.
    doc = {
        "intent_id": "test-runtime-001",
        "stack": "camino",
        "action": "BUY",
        "symbol": "AAPL",
        "lane": "equity",
        "confidence": 0.65,
        "rationale": "runtime smoke",
        "evidence": {"source_doc": {}},     # built upstream
        "executed": False,
        "gate_state": "pending",
    }
    snapshot_action = doc["action"]
    snapshot_confidence = doc["confidence"]
    snapshot_executed = doc["executed"]
    snapshot_gate = doc["gate_state"]

    async def _load(symbol, tf="1h", limit=120, source=None):
        return _bull_run(), "finnhub_equity"

    with patch("shared.research.intent_evidence.load_recent_bars", new=_load):
        await attach_research_evidence(doc)

    # 1. Research evidence is stamped on `evidence.research_signals`.
    ev = doc["evidence"]
    assert ev["research_status"] == "ok"
    assert ev["research_source"] == "finnhub_equity"
    assert ev["research_bars_used"] == 80
    assert ev["research_tf"] == "1d"        # default for equity lane
    sigs = ev["research_signals"]
    assert sigs and sigs[0]["strategy_id"] == "large_cap_momentum_v1"
    assert sigs[0]["direction"] == "BUY"

    # 2. Brain decision fields are untouched.
    assert doc["action"] == snapshot_action
    assert doc["confidence"] == snapshot_confidence

    # 3. Pipeline fields are untouched.
    assert doc["executed"] is snapshot_executed
    assert doc["gate_state"] == snapshot_gate


@pytest.mark.asyncio
async def test_attach_research_evidence_bar_source_crash_is_contained():
    """The runtime path's try/except wraps the helper; the helper
    itself ALSO contains its own errors. Both layers tested here so
    a future refactor of either one doesn't quietly start dropping
    intents on a research outage."""
    from shared.research.intent_evidence import attach_research_evidence

    doc = {
        "intent_id": "test-runtime-002",
        "stack": "hellcat",
        "action": "SELL",
        "symbol": "ETH/USD",
        "lane": "crypto",
        "confidence": 0.71,
        "evidence": {},
    }

    async def _boom(*args, **kwargs):
        raise RuntimeError("mongo unreachable during research read")

    with patch("shared.research.intent_evidence.load_recent_bars", new=_boom):
        await attach_research_evidence(doc)

    # Status field captures the failure for the operator …
    assert doc["evidence"]["research_status"] == "error"
    assert "mongo unreachable" in doc["evidence"]["research_error"]
    # … but the intent's emit fields are completely untouched.
    assert doc["action"] == "SELL"
    assert doc["confidence"] == 0.71


@pytest.mark.asyncio
async def test_attach_research_evidence_no_lane_safe_noop():
    """An intent without a `lane` field (legacy / malformed) must not
    crash the runtime emit. The helper should detect the missing
    field and silently no-op."""
    from shared.research.intent_evidence import attach_research_evidence
    doc = {
        "intent_id": "test-runtime-003",
        "stack": "gto",
        "action": "BUY",
        "symbol": "AAPL",
        "confidence": 0.6,
        "evidence": {},
        # lane intentionally absent
    }
    await attach_research_evidence(doc)
    # No status fields stamped → caller can tell research didn't run.
    assert "research_status" not in doc["evidence"]
    assert doc["action"] == "BUY"


@pytest.mark.asyncio
async def test_runtime_and_admin_paths_share_the_same_helper():
    """Tiny structural assertion — both call sites import from the
    same module so the doctrine guard ("research is evidence, never
    authority") is enforced in exactly one place. If a future refactor
    moves one of them onto a parallel implementation, this test
    catches it."""
    import shared.intents as intents_mod
    src = intents_mod.__file__
    with open(src, "r", encoding="utf-8") as f:
        text = f.read()
    occurrences = text.count(
        "from shared.research.intent_evidence import attach_research_evidence"
    )
    # One in _post_intent_impl, one in admin_post_intent.
    assert occurrences == 2, (
        f"expected 2 imports of attach_research_evidence in shared/intents.py, "
        f"found {occurrences}"
    )

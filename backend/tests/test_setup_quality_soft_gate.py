"""Setup-quality soft-gate tests for `shared.auto_router._route_one`.

Doctrine (2026-07-09 operator directive, P1b):

    "if failed_checks == ['liquidity_ok', 'quality_ok', 'score_ok']:
     notional_usd *= 0.20 — marginal setups execute as $1 probes
     instead of blocking completely."

The soft-gate reads `intent.doctrine_packet.seats.execution_judge
.failed_checks`. When the set is EXACTLY the three marginal-setup
markers, the resolved notional is shrunk to 20% and `notional_source`
is overridden to `"quality_soft_gate"`. Any other failed-check shape
(fewer, more, different) falls through untouched.
"""
from __future__ import annotations

import sys
import uuid

import pytest

sys.path.insert(0, "/app/backend")

from db import db  # noqa: E402
from namespaces import CAPITAL_LEDGER, SHARED_INTENTS  # noqa: E402
from shared import auto_router  # noqa: E402
from shared.capital.ledger import init_ledger  # noqa: E402

# Reuse the exact same test scaffolding as the micro-notional file so
# both suites stay wire-compatible.
from tests.test_micro_notional_fallback import (  # noqa: E402
    _capture_broker,
    _insert_intent as _upstream_insert_intent,
    _wire_common_patches,
)


@pytest.fixture(autouse=True)
async def _clean_state():
    """Purge synthetic soft-gate rows before and after each test.
    Mirrors the micro-notional suite's cleanup so both share DB
    hygiene (no leakage into prod-shared `test_database`)."""
    await db[CAPITAL_LEDGER].delete_many({})
    await db[SHARED_INTENTS].delete_many(
        {"intent_id": {"$regex": "^(soft-gate-test-|micro-notional-test-)"}},
    )
    yield
    await db[CAPITAL_LEDGER].delete_many({})
    await db[SHARED_INTENTS].delete_many(
        {"intent_id": {"$regex": "^(soft-gate-test-|micro-notional-test-)"}},
    )
    # Reset sys.modules-cached `shared.<attr>` so subsequent tests
    # get fresh, un-mocked seat/risk/executions modules.
    import shared as _shared_pkg
    for _attr in ("seat", "risk", "executions"):
        try:
            delattr(_shared_pkg, _attr)
        except AttributeError:
            pass


async def _insert_intent_with_doctrine(intent_id, action, failed_checks,
                                        *, legacy=None):
    """Insert a test intent with a doctrine_packet stamped with the
    given `failed_checks`. Mirrors the role-keyed doctrine shape that
    both equity and crypto emit."""
    doc = await _upstream_insert_intent(intent_id, action, legacy=legacy)
    packet = {
        "seats": {
            "execution_judge": {
                "execution_ready": len(failed_checks) == 0,
                "failed_checks": list(failed_checks),
                "not_ready_reason": (
                    "; ".join(failed_checks) if failed_checks else None
                ),
            },
        },
    }
    doc["doctrine_packet"] = packet
    await db[SHARED_INTENTS].update_one(
        {"intent_id": intent_id},
        {"$set": {"doctrine_packet": packet}},
    )
    return doc


# ─── Case 1: exact marginal-setup pattern → shrunk to 20% ────────

@pytest.mark.asyncio
async def test_soft_gate_marginal_setup_shrinks_to_20pct(monkeypatch):
    """failed_checks == {liquidity_ok, quality_ok, score_ok} → the
    resolved notional is 20% of the ladder pick, and `notional_source`
    is stamped as `quality_soft_gate`."""
    await init_ledger(1000.0, 500.0)
    intent_id = f"soft-gate-test-{uuid.uuid4()}"
    intent = await _insert_intent_with_doctrine(
        intent_id, "BUY",
        ["liquidity_ok", "quality_ok", "score_ok"],
        legacy=50.0,
    )

    _wire_common_patches(monkeypatch)
    captured = _capture_broker(monkeypatch)

    result = await auto_router._route_one(intent)
    assert result["verdict"] == "executed"
    # $50 legacy → 20% → $10 shipped.
    assert captured["notional"] == pytest.approx(10.0)

    doc = await db[SHARED_INTENTS].find_one({"intent_id": intent_id})
    assert doc.get("notional_source") == "quality_soft_gate"


# ─── Case 2: superset of the marginal pattern → NOT soft-gated ──

@pytest.mark.asyncio
async def test_soft_gate_extra_failed_check_is_not_marginal(monkeypatch):
    """A superset of the marginal pattern (extra failed check) is NOT
    the marginal-setup shape — must fall through with the original
    notional and original source."""
    await init_ledger(1000.0, 500.0)
    intent_id = f"soft-gate-test-{uuid.uuid4()}"
    intent = await _insert_intent_with_doctrine(
        intent_id, "BUY",
        ["liquidity_ok", "quality_ok", "score_ok", "spread_ok"],
        legacy=50.0,
    )

    _wire_common_patches(monkeypatch)
    captured = _capture_broker(monkeypatch)

    result = await auto_router._route_one(intent)
    assert result["verdict"] == "executed"
    assert captured["notional"] == pytest.approx(50.0), (
        "extra failed check must NOT trigger the soft-gate — this is "
        "a stricter shape than marginal, needs a different remediation"
    )
    doc = await db[SHARED_INTENTS].find_one({"intent_id": intent_id})
    assert doc.get("notional_source") == "brain_legacy"


# ─── Case 3: subset of the marginal pattern → NOT soft-gated ────

@pytest.mark.asyncio
async def test_soft_gate_partial_failed_checks_not_marginal(monkeypatch):
    """Only two of the three marginal markers failing is NOT the
    marginal-setup shape — must fall through untouched."""
    await init_ledger(1000.0, 500.0)
    intent_id = f"soft-gate-test-{uuid.uuid4()}"
    intent = await _insert_intent_with_doctrine(
        intent_id, "BUY",
        ["liquidity_ok", "quality_ok"],
        legacy=50.0,
    )

    _wire_common_patches(monkeypatch)
    captured = _capture_broker(monkeypatch)

    result = await auto_router._route_one(intent)
    assert result["verdict"] == "executed"
    assert captured["notional"] == pytest.approx(50.0)
    doc = await db[SHARED_INTENTS].find_one({"intent_id": intent_id})
    assert doc.get("notional_source") == "brain_legacy"


# ─── Case 4: empty failed_checks → NOT soft-gated ───────────────

@pytest.mark.asyncio
async def test_soft_gate_no_failed_checks_no_op(monkeypatch):
    """Healthy setup (no failed checks) must not be soft-gated."""
    await init_ledger(1000.0, 500.0)
    intent_id = f"soft-gate-test-{uuid.uuid4()}"
    intent = await _insert_intent_with_doctrine(
        intent_id, "BUY", [], legacy=50.0,
    )

    _wire_common_patches(monkeypatch)
    captured = _capture_broker(monkeypatch)

    result = await auto_router._route_one(intent)
    assert result["verdict"] == "executed"
    assert captured["notional"] == pytest.approx(50.0)


# ─── Case 5: missing doctrine_packet → NOT soft-gated ───────────

@pytest.mark.asyncio
async def test_soft_gate_missing_packet_falls_through(monkeypatch):
    """Intent with NO doctrine_packet (legacy row / packet build
    failed) must NOT be soft-gated — the guard swallows the KeyError
    and the notional flows through unchanged."""
    await init_ledger(1000.0, 500.0)
    intent_id = f"soft-gate-test-{uuid.uuid4()}"
    intent = await _upstream_insert_intent(intent_id, "BUY", legacy=50.0)
    # No doctrine_packet key at all.

    _wire_common_patches(monkeypatch)
    captured = _capture_broker(monkeypatch)

    result = await auto_router._route_one(intent)
    assert result["verdict"] == "executed"
    assert captured["notional"] == pytest.approx(50.0)


# ─── Case 6: soft-gate composes with micro-live default ─────────

@pytest.mark.asyncio
async def test_soft_gate_composes_with_micro_default(monkeypatch):
    """When the ladder falls back to the $5 micro-live default AND
    the doctrine flags marginal setup, the shipped notional is
    $5 * 0.20 = $1.00 — matching the operator's directive that
    marginal setups execute as $1 probes."""
    monkeypatch.delenv("MICRO_LIVE_DEFAULT_USD", raising=False)
    await init_ledger(1000.0, 500.0)
    intent_id = f"soft-gate-test-{uuid.uuid4()}"
    intent = await _insert_intent_with_doctrine(
        intent_id, "BUY",
        ["liquidity_ok", "quality_ok", "score_ok"],
    )  # no legacy or v3 notional → hits micro-live path

    _wire_common_patches(monkeypatch)
    captured = _capture_broker(monkeypatch)

    result = await auto_router._route_one(intent)
    assert result["verdict"] == "executed"
    assert captured["notional"] == pytest.approx(1.0), (
        f"$5 micro-live × 0.20 = $1.00 probe, got ${captured.get('notional')}"
    )
    doc = await db[SHARED_INTENTS].find_one({"intent_id": intent_id})
    # `quality_soft_gate` wins over `micro_live_default` — the last-
    # applied resolution is the audit-canonical one.
    assert doc.get("notional_source") == "quality_soft_gate"

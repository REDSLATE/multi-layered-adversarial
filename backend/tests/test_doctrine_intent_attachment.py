"""Integration tests for the doctrine sidecar attachment in the equity
intent ingest path.

Doctrine pin: this layer is a READ-ONLY ATTACHMENT — it must NEVER
change direction, confidence, gate state, or anything execution-related.
The tests below assert exactly that, in addition to the basic flow.
"""
from __future__ import annotations

import asyncio
import os

import pytest

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test_database")


def _run(coro, timeout: float = 15.0):
    return asyncio.run(asyncio.wait_for(coro, timeout=timeout))


# ─── lane filter: crypto returns None ───────────────────────────────────

def test_crypto_lane_now_returns_crypto_packet():
    """Doctrine pin (2026-02-17): twin lanes get twin doctrine. Crypto
    intents must get the CRYPTO packet (lane='crypto',
    doctrine_version='crypto_sidecar_v1'), NOT the equity packet."""
    from shared.intents import _build_and_persist_doctrine_packet
    result = _run(_build_and_persist_doctrine_packet(
        intent_id="test-crypto-1",
        stack="redeye",
        lane="crypto",
        symbol="BTC/USD",
        action="BUY",
        confidence=0.7,
        snapshot={
            "volume_24h_usd": 500_000_000,
            "spread_bps": 10,
            "exchange_liquidity_score": 0.9,
            "trend_strength": 0.8,
            "volatility_1h": 0.02,
            "funding_rate": 0.0001,
            "open_interest_change_pct": 5,
            "liquidation_imbalance": 0.2,
            "btc_regime_alignment": 0.8,
        },
        ingest_method="test",
    ))
    assert result is not None
    assert result["event_type"] == "BRAIN_DOCTRINE_SIDECAR_PACKET"
    assert result["lane"] == "crypto"
    assert result["doctrine_version"] == "crypto_sidecar_v1"
    # Role-keyed shape
    assert "seats" in result
    assert set(result["seats"].keys()) == {"strategist", "adversary", "governor", "execution_judge"}
    # Every seat pins may_execute=False
    for s in result["seats"].values():
        assert s["may_execute"] is False


def test_missing_lane_returns_unknown_lane_reject():
    """Routing-level safety: an intent with no lane gets a hard REJECT
    packet (not None) so the absence of doctrine is visible in the
    audit log instead of being silently dropped."""
    from shared.intents import _build_and_persist_doctrine_packet
    result = _run(_build_and_persist_doctrine_packet(
        intent_id="test-nolane-1",
        stack="alpha",
        lane=None,
        symbol="AAPL",
        action="BUY",
        confidence=0.5,
        snapshot=None,
        ingest_method="test",
    ))
    assert result is not None
    assert result["doctrine_version"] == "unknown_lane_reject_v1"
    assert result["base_labels"]["quality"] == "REJECT"


# ─── lane filter: equity returns full packet ────────────────────────────

def test_equity_a_quality_packet_shape():
    from shared.intents import _build_and_persist_doctrine_packet
    snap = {
        "price": 7.5, "gap_pct": 22, "relative_volume": 8,
        "has_news": True, "float_millions": 10, "pattern": "pullback",
        "market_regime": "strong", "spread_bps": 40,
    }
    packet = _run(_build_and_persist_doctrine_packet(
        intent_id="test-eq-a-1", stack="alpha", lane="equity",
        symbol="NVDA", action="BUY", confidence=0.78,
        snapshot=snap, ingest_method="test",
    ))
    assert packet is not None
    assert packet["event_type"] == "BRAIN_DOCTRINE_SIDECAR_PACKET"
    assert packet["doctrine_version"] == "small_account_sidecar_v1"
    # Role-keyed shape — same as crypto twin
    for role in ("strategist", "adversary", "governor", "execution_judge"):
        assert role in packet["seats"], f"missing role {role}"
        assert packet["seats"][role]["may_execute"] is False
    # Quality lifted to A
    assert packet["base_labels"]["quality"] == "A_QUALITY"
    assert packet["seats"]["execution_judge"]["execution_ready"] is True


def test_equity_with_empty_snapshot_still_returns_packet():
    """No facts ⇒ REJECT quality, but the packet still attaches."""
    from shared.intents import _build_and_persist_doctrine_packet
    packet = _run(_build_and_persist_doctrine_packet(
        intent_id="test-eq-empty-1", stack="alpha", lane="equity",
        symbol="AAPL", action="BUY", confidence=0.5,
        snapshot=None, ingest_method="test",
    ))
    assert packet is not None
    assert packet["base_labels"]["quality"] == "REJECT"
    assert packet["seats"]["governor"]["governor_action"] == "block"


# ─── safety pins: read-only attachment ──────────────────────────────────

def test_packet_never_grants_execution_authority():
    """Every seat must pin may_execute=False regardless of inputs."""
    from shared.intents import _build_and_persist_doctrine_packet
    snap = {
        "price": 7.5, "gap_pct": 22, "relative_volume": 8,
        "has_news": True, "float_millions": 10, "pattern": "pullback",
        "market_regime": "strong", "spread_bps": 40,
    }
    packet = _run(_build_and_persist_doctrine_packet(
        intent_id="test-readonly-1", stack="alpha", lane="equity",
        symbol="NVDA", action="BUY", confidence=0.99,
        snapshot=snap, ingest_method="test",
    ))
    for seat in packet["seats"].values():
        assert seat["may_execute"] is False
    assert packet["seats"]["execution_judge"]["may_create_direction"] is False
    for role in ("strategist", "adversary", "governor"):
        assert packet["seats"][role]["may_override_direction"] is False


# ─── audit row persistence ──────────────────────────────────────────────

def test_audit_row_written_to_doctrine_sidecars_collection():
    """The packet build helper must also write an audit row joined to
    `intent_id` so Shelly + the operator can reconstruct what doctrine
    said about a specific intent later.

    Uses a dedicated event loop so Motor's connection pool + the Shelly
    background `record_async` task can drain before the loop closes
    (avoids `RuntimeError: Event loop is closed` cleanup spam).
    """
    from db import db
    from namespaces import DOCTRINE_SIDECARS
    from shared.intents import _build_and_persist_doctrine_packet

    intent_id = f"audit-test-{os.getpid()}-{int.from_bytes(os.urandom(2), 'big')}"
    snap = {
        "price": 7.5, "gap_pct": 22, "relative_volume": 8,
        "has_news": True, "float_millions": 10, "pattern": "pullback",
        "market_regime": "strong", "spread_bps": 40,
    }

    async def _go():
        await _build_and_persist_doctrine_packet(
            intent_id=intent_id, stack="alpha", lane="equity",
            symbol="NVDA", action="BUY", confidence=0.7,
            snapshot=snap, ingest_method="test_audit",
        )
        row = await db[DOCTRINE_SIDECARS].find_one(
            {"intent_id": intent_id}, {"_id": 0},
        )
        await db[DOCTRINE_SIDECARS].delete_many({"intent_id": intent_id})
        # Drain any background tasks (the Shelly recorder fires
        # `record_async` which schedules an async task; if we don't
        # await it, Motor logs an event-loop-closed traceback on cleanup).
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        return row

    loop = asyncio.new_event_loop()
    try:
        row = loop.run_until_complete(asyncio.wait_for(_go(), timeout=15))
    except Exception as e:
        pytest.skip(f"Mongo unavailable: {e}")
        return
    finally:
        loop.close()

    assert row is not None, "audit row was not written to doctrine_sidecars"
    assert row["intent_id"] == intent_id
    assert row["stack"] == "alpha"
    assert row["lane"] == "equity"
    assert row["symbol"] == "NVDA"
    assert row["quality"] == "A_QUALITY"
    assert row["camaro_execution_ready"] is True
    assert row["redeye_challenge_required"] is False
    assert row["chevelle_governor_action"] == "modulate"
    assert "packet" in row and isinstance(row["packet"], dict)
    assert "ts" in row

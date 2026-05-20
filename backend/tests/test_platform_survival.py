import os

from shared.runtime.platform_survival import (
    RuntimeStamp,
    sidecar_build_intent,
    mc_canonical_gate,
    broker_verify_receipt,
)


def test_sidecar_has_no_local_execution_authority():
    stamp = RuntimeStamp.current(sidecar_room="room_5")
    assert stamp.local_execution_authority is False


def test_mc_does_not_block_low_confidence_under_doctrine_c():
    """Doctrine (c, 2026-05-20): MC no longer re-judges brain
    confidence. Brain owns its own conviction floor. MC accepts the
    intent and surfaces the brain's self-assessment as telemetry."""
    os.environ["RISEDUAL_CRYPTO_CONFIDENCE_FLOOR"] = "0.45"

    intent = sidecar_build_intent(
        brain_id="camaro",
        lane="crypto",
        symbol="BTC-USD",
        direction="BUY",
        confidence=0.27,
        room_id="room_1",
    )

    result = mc_canonical_gate(intent)
    assert result["accepted"] is True
    assert result["reason"] == "MC_CANONICAL_GATE_APPROVED"
    # Telemetry: MC still records what the brain's floor said.
    assert result["brain_confidence_below_floor"] is True
    assert result["brain_confidence_floor"] == 0.45
    assert "CONFIDENCE_BELOW_FLOOR" not in result["errors"]


def test_mc_allows_valid_intent_with_receipt():
    os.environ["RISEDUAL_CRYPTO_CONFIDENCE_FLOOR"] = "0.20"
    os.environ["RISEDUAL_MC_RECEIPT_SECRET"] = "test-secret"

    intent = sidecar_build_intent(
        brain_id="camaro",
        lane="crypto",
        symbol="BTC-USD",
        direction="BUY",
        confidence=0.27,
        room_id="room_1",
    )

    result = mc_canonical_gate(intent)
    assert result["accepted"] is True

    broker = broker_verify_receipt(result["receipt"])
    assert broker["ok"] is True
    assert broker["reason"] == "VALID_MC_RECEIPT"


def test_broker_rejects_unsigned_or_tampered_receipt():
    os.environ["RISEDUAL_CRYPTO_CONFIDENCE_FLOOR"] = "0.20"
    os.environ["RISEDUAL_MC_RECEIPT_SECRET"] = "test-secret"

    intent = sidecar_build_intent(
        brain_id="redeye",
        lane="crypto",
        symbol="ETH-USD",
        direction="SELL",
        confidence=0.80,
        room_id="room_5",
    )

    result = mc_canonical_gate(intent)
    receipt = result["receipt"]
    receipt["symbol"] = "BTC-USD"

    broker = broker_verify_receipt(receipt)
    assert broker["ok"] is False
    assert broker["reason"] == "BAD_MC_RECEIPT_SIGNATURE"

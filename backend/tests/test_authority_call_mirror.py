"""End-to-end test for the governor authority-call mirror.

Pins the doctrine: when a brain POSTs `/api/ingest/opinion` carrying
`evidence.authority_call`, MC mirrors the call into
`shared_adl_receipts` in the exact shape the council's
`_latest_governor_call()` + `_normalize_governor_call()` expect, so
the gate chain consumes the call on the next intent.

Without this bridge, Chevelle's authority calls land only in
`shared_opinions` and are invisible to the council — every intent
would block on `NO_STANCE_LOW_EFFECTIVE_CONF`.
"""
from __future__ import annotations

import pytest

from db import db
from namespaces import SHARED_RECEIPTS
from shared.opinions import _mirror_authority_call_to_receipts
from shared.council import (
    _latest_governor_call,
    _normalize_governor_call,
    _governance_verdict,
    COUNCIL_POLICY,
)

pytestmark = [pytest.mark.tripwire, pytest.mark.asyncio]


def _opinion(symbol: str, status: str, reason: str, confidence: float = 0.85) -> dict:
    return {
        "opinion_id": f"test-op-{symbol}",
        "runtime": "chevelle",
        "topic": f"symbol:{symbol}",
        "stance": "veto" if status == "BLOCK" else "hypothesis",
        "body": "test",
        "confidence": confidence,
        "evidence": {
            "authority_call": {
                "brain": "chevelle",
                "role": "governor",
                "intent_type": "GOVERNOR_AUTHORITY",
                "lane": "equity",
                "symbol": symbol,
                "status": status,
                "reason": reason,
                "confidence": confidence,
            }
        },
        "thread_root": f"test-op-{symbol}",
    }


async def _cleanup(symbol: str):
    await db[SHARED_RECEIPTS].delete_many(
        {"kind": "authority_call_mirror", "symbol": symbol}
    )


async def test_mirror_lands_in_receipts_with_council_signal_shape():
    await _cleanup("PRMIR_A")
    await _mirror_authority_call_to_receipts(
        _opinion("PRMIR_A", "BLOCK", "GOVERNOR_HARD_VETO")
    )
    doc = await db[SHARED_RECEIPTS].find_one(
        {"kind": "authority_call_mirror", "symbol": "PRMIR_A"}, {"_id": 0}
    )
    assert doc is not None
    # Council filter compatibility — must satisfy _ACTION_FIELDS + _AUTHORITY_CALL_VALUES
    assert doc["action"] == "authority_call"
    # Normalizer requires signals in a recognized container
    assert "payload" in doc
    payload = doc["payload"]
    assert payload["executable"] is False
    assert payload["veto"] is True
    assert payload["stance"] == "VETO"
    assert payload["reason"] == "GOVERNOR_HARD_VETO"
    assert payload["confidence"] == 0.85
    # Audit copy preserved
    assert doc["authority_call"]["status"] == "BLOCK"


async def test_hard_veto_round_trip_produces_hard_block_verdict():
    await _cleanup("PRMIR_B")
    await _mirror_authority_call_to_receipts(
        _opinion("PRMIR_B", "BLOCK", "GOVERNOR_HARD_VETO")
    )
    holder, gov_doc = await _latest_governor_call("PRMIR_B", "equity")
    assert holder == "chevelle"
    assert gov_doc is not None
    norm = _normalize_governor_call(gov_doc)
    assert norm is not None
    assert norm["veto"] is True
    assert norm["reason"] == "GOVERNOR_HARD_VETO"
    v = _governance_verdict(
        intent={"intent_id": "x", "symbol": "PRMIR_B", "action": "BUY", "confidence": 0.7},
        gov_norm=norm,
        governor_alive=True,
        governor_holder=holder,
        policy=COUNCIL_POLICY["equity"],
    )
    assert v["allowed"] is False
    assert v["reason"] == "GOVERNOR_HARD_VETO"
    assert v["execution_effect"] == "HARD_BLOCK"
    assert v["display_status"] == "BLOCK"


async def test_warn_status_round_trip_produces_soft_dissent_downweight():
    await _cleanup("PRMIR_C")
    await _mirror_authority_call_to_receipts(
        _opinion("PRMIR_C", "WARN", "CHEVELLE_REDUCE_SIZE", confidence=0.55)
    )
    holder, gov_doc = await _latest_governor_call("PRMIR_C", "equity")
    norm = _normalize_governor_call(gov_doc)
    assert norm["veto"] is False
    assert norm["stance"] == "DISSENT"
    assert norm["reason"] == "CHEVELLE_REDUCE_SIZE"
    v = _governance_verdict(
        intent={"intent_id": "x", "symbol": "PRMIR_C", "action": "BUY", "confidence": 0.7},
        gov_norm=norm,
        governor_alive=True,
        governor_holder=holder,
        policy=COUNCIL_POLICY["equity"],
    )
    assert v["allowed"] is True
    assert v["execution_effect"] == "ALLOW"
    assert v["reason"] == "SOFT_DISSENT_DOWNWEIGHTED"


async def test_allow_status_round_trip_passes_through():
    await _cleanup("PRMIR_D")
    await _mirror_authority_call_to_receipts(
        _opinion("PRMIR_D", "ALLOW", "NO_GOVERNOR_DISSENT", confidence=0.8)
    )
    holder, gov_doc = await _latest_governor_call("PRMIR_D", "equity")
    norm = _normalize_governor_call(gov_doc)
    assert norm["executable"] is True
    assert norm["veto"] is False
    v = _governance_verdict(
        intent={"intent_id": "x", "symbol": "PRMIR_D", "action": "BUY", "confidence": 0.7},
        gov_norm=norm,
        governor_alive=True,
        governor_holder=holder,
        policy=COUNCIL_POLICY["equity"],
    )
    assert v["allowed"] is True
    assert v["reason"] == "NO_GOVERNOR_DISSENT"


async def test_mirror_refuses_brain_impersonation():
    """An opinion runtime=chevelle that smuggles an authority_call
    saying brain=alpha MUST NOT be mirrored (defensive)."""
    await _cleanup("PRMIR_E")
    op = _opinion("PRMIR_E", "BLOCK", "GOVERNOR_HARD_VETO")
    op["evidence"]["authority_call"]["brain"] = "alpha"  # impersonation
    await _mirror_authority_call_to_receipts(op)
    doc = await db[SHARED_RECEIPTS].find_one(
        {"kind": "authority_call_mirror", "symbol": "PRMIR_E"}
    )
    assert doc is None


async def test_mirror_skipped_when_no_authority_call():
    await _cleanup("PRMIR_F")
    op = {
        "opinion_id": "no-auth",
        "runtime": "chevelle",
        "topic": "symbol:PRMIR_F",
        "stance": "hypothesis",
        "body": "no authority_call in evidence",
        "confidence": 0.5,
        "evidence": {"market_note": "soft"},  # no authority_call
        "thread_root": "no-auth",
    }
    await _mirror_authority_call_to_receipts(op)
    doc = await db[SHARED_RECEIPTS].find_one(
        {"kind": "authority_call_mirror", "symbol": "PRMIR_F"}
    )
    assert doc is None

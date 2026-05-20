"""Brain Memory Translator — translation contract tests.

The translator is the load-bearing wall in front of the (not-yet-built)
MemoryKernelLedger. Many brain dialects in, exactly one MC language out.

If any of these tests break, MC's "one truth format" guarantee is broken.
"""
from __future__ import annotations

import pytest

from services.brain_memory_translator import (
    DIRECTION_ALIASES,
    FIELD_ALIASES,
    MEMORY_TYPE_ALIASES,
    STACK_ALIASES,
    normalize_memory_type,
    normalize_payload,
    normalize_stack,
    translate_brain_memory,
)


# ───── normalize_stack() ──────────────────────────────────────────────


def test_stack_red_eye_variants_collapse_to_redeye():
    assert normalize_stack("REDEYE") == "redeye"
    assert normalize_stack("red_eye") == "redeye"
    assert normalize_stack("Red-Eye") == "redeye"
    assert normalize_stack("redeye") == "redeye"


def test_stack_canonical_passthrough():
    for canon in ("alpha", "camaro", "chevelle", "redeye"):
        assert normalize_stack(canon) == canon
        assert normalize_stack(canon.upper()) == canon


def test_stack_unknown_returns_lowercased():
    assert normalize_stack(" Mustang ") == "mustang"


def test_stack_empty_returns_empty():
    assert normalize_stack("") == ""
    assert normalize_stack(None) == ""  # type: ignore[arg-type]


# ───── normalize_memory_type() ────────────────────────────────────────


def test_execution_dialects_collapse_to_execution():
    for raw in ("fill", "trade", "order_fill", "paper_fill", "live_fill", "execution"):
        assert normalize_memory_type(raw) == "execution"


def test_dissent_is_distinct_from_diagnostic():
    # critical: dissent must NOT be folded into diagnostic
    assert normalize_memory_type("dissent") == "council_dissent"
    assert normalize_memory_type("critique") == "diagnostic"


def test_simulation_lanes_kept_separate():
    assert normalize_memory_type("replay") == "replay"
    assert normalize_memory_type("backtest") == "backtest"
    assert normalize_memory_type("simulation") == "simulation"


def test_unknown_memory_type_defaults_to_diagnostic():
    assert normalize_memory_type("") == "diagnostic"
    assert normalize_memory_type(None) == "diagnostic"  # type: ignore[arg-type]


# ───── normalize_payload() — field renaming ───────────────────────────


def test_ticker_pair_asset_all_become_symbol():
    assert normalize_payload({"ticker": "btc-usd"})["symbol"] == "BTC-USD"
    assert normalize_payload({"pair": "eth-usd"})["symbol"] == "ETH-USD"
    assert normalize_payload({"asset": "aapl"})["symbol"] == "AAPL"


def test_order_id_variants_become_broker_order_id():
    assert normalize_payload({"order_id": "abc"})["broker_order_id"] == "abc"
    assert normalize_payload({"broker_id": "abc"})["broker_order_id"] == "abc"
    assert normalize_payload({"brokerOrderId": "abc"})["broker_order_id"] == "abc"


def test_receipt_variants_become_execution_receipt_id():
    assert normalize_payload({"receipt": "r1"})["execution_receipt_id"] == "r1"
    assert normalize_payload({"receipt_id": "r1"})["execution_receipt_id"] == "r1"
    assert normalize_payload({"executionReceiptId": "r1"})["execution_receipt_id"] == "r1"


def test_qty_variants_become_filled_qty():
    assert normalize_payload({"qty": "1.5"})["filled_qty"] == 1.5
    assert normalize_payload({"quantity": 2})["filled_qty"] == 2.0
    assert normalize_payload({"filledQuantity": "3.0"})["filled_qty"] == 3.0


def test_side_action_signal_all_become_direction():
    assert normalize_payload({"side": "buy"})["direction"] == "BUY"
    assert normalize_payload({"action": "sell"})["direction"] == "SELL"
    assert normalize_payload({"signal": "hold"})["direction"] == "HOLD"


# ───── normalize_payload() — direction aliasing ───────────────────────


def test_long_bull_bullish_become_buy():
    for raw in ("LONG", "BULL", "BULLISH", "long", "Bull"):
        out = normalize_payload({"direction": raw})
        assert out["direction"] == "BUY", f"expected BUY for {raw!r}"


def test_short_bear_bearish_become_sell():
    for raw in ("SHORT", "BEAR", "BEARISH", "short", "Bear"):
        out = normalize_payload({"direction": raw})
        assert out["direction"] == "SELL", f"expected SELL for {raw!r}"


def test_no_trade_neutral_wait_become_hold():
    for raw in ("NO_TRADE", "NEUTRAL", "WAIT", "neutral"):
        out = normalize_payload({"direction": raw})
        assert out["direction"] == "HOLD"


def test_unknown_direction_is_uppercased_passthrough():
    # don't silently swallow new directions — surface them upper-cased
    assert normalize_payload({"direction": "cover"})["direction"] == "COVER"


# ───── normalize_payload() — coercion ─────────────────────────────────


def test_confidence_percentage_form_normalised_to_unit_interval():
    assert normalize_payload({"confidence": 75})["confidence"] == 0.75
    assert normalize_payload({"confidence": "62.5"})["confidence"] == 0.625


def test_confidence_already_unit_interval_passes_through():
    assert normalize_payload({"confidence": 0.42})["confidence"] == 0.42


def test_confidence_clamped_to_unit_interval():
    assert normalize_payload({"confidence": 9999})["confidence"] == 1.0
    assert normalize_payload({"confidence": -5})["confidence"] == 0.0


def test_confidence_unparseable_becomes_none():
    assert normalize_payload({"confidence": "high"})["confidence"] is None


def test_filled_qty_unparseable_becomes_none():
    assert normalize_payload({"qty": "not-a-number"})["filled_qty"] is None


def test_symbol_uppercased_and_stripped():
    assert normalize_payload({"symbol": "  btc-usd  "})["symbol"] == "BTC-USD"


def test_non_dict_payload_returns_empty():
    assert normalize_payload(None) == {}  # type: ignore[arg-type]
    assert normalize_payload("garbage") == {}  # type: ignore[arg-type]
    assert normalize_payload([1, 2]) == {}  # type: ignore[arg-type]


# ───── translate_brain_memory() — end-to-end ──────────────────────────


def test_camaro_dialect_translated():
    stack, mtype, payload = translate_brain_memory(
        source_stack="Camaro",
        memory_type="fill",
        payload={"ticker": "aapl", "order_id": "ord-1", "qty": 10, "side": "long", "conf": 80},
    )
    assert stack == "camaro"
    assert mtype == "execution"
    assert payload["symbol"] == "AAPL"
    assert payload["broker_order_id"] == "ord-1"
    assert payload["filled_qty"] == 10.0
    assert payload["direction"] == "BUY"
    assert payload["confidence"] == 0.80


def test_redeye_dialect_translated():
    stack, mtype, payload = translate_brain_memory(
        source_stack="RED-EYE",
        memory_type="trade",
        payload={
            "pair": "btc-usd",
            "brokerOrderId": "kr-99",
            "filledQuantity": "0.25",
            "action": "short",
            "score": 0.91,
        },
    )
    assert stack == "redeye"
    assert mtype == "execution"
    assert payload["symbol"] == "BTC-USD"
    assert payload["broker_order_id"] == "kr-99"
    assert payload["filled_qty"] == 0.25
    assert payload["direction"] == "SELL"
    assert payload["confidence"] == 0.91


def test_alpha_dialect_translated():
    stack, mtype, payload = translate_brain_memory(
        source_stack="alpha",
        memory_type="paper_fill",
        payload={"asset": "spy", "receipt_id": "rcpt-7", "qty": 3, "side": "bullish"},
    )
    assert stack == "alpha"
    assert mtype == "execution"
    assert payload["symbol"] == "SPY"
    assert payload["execution_receipt_id"] == "rcpt-7"
    assert payload["filled_qty"] == 3.0
    assert payload["direction"] == "BUY"


def test_chevelle_governance_dialect_translated():
    stack, mtype, payload = translate_brain_memory(
        source_stack="chevelle",
        memory_type="governance",
        payload={"note": "policy review flagged spread floor"},
    )
    assert stack == "chevelle"
    assert mtype == "governance_review"
    assert payload["note"] == "policy review flagged spread floor"


def test_translation_breadcrumb_preserved():
    _, _, payload = translate_brain_memory(
        source_stack="Red_Eye",
        memory_type="critique",
        payload={"ticker": "eth-usd"},
    )
    assert payload["_translated_from"] == {
        "source_stack": "Red_Eye",
        "memory_type": "critique",
    }


# ───── locked-table contracts (tripwire surface) ──────────────────────


@pytest.mark.tripwire
def test_canonical_stacks_locked():
    """The 4 canonical stacks must remain stable for the kernel."""
    canonical = {STACK_ALIASES[k] for k in STACK_ALIASES}
    assert canonical == {"alpha", "camaro", "chevelle", "redeye"}


@pytest.mark.tripwire
def test_canonical_memory_types_locked():
    """Kernel-side memory types — adding is fine, renaming is not."""
    canonical = set(MEMORY_TYPE_ALIASES.values())
    assert {
        "execution",
        "diagnostic",
        "council_dissent",
        "governance_review",
        "replay",
        "backtest",
        "simulation",
    }.issubset(canonical)


@pytest.mark.tripwire
def test_canonical_directions_locked():
    """Only BUY / SELL / HOLD may appear as a translated direction."""
    assert set(DIRECTION_ALIASES.values()) == {"BUY", "SELL", "HOLD"}


@pytest.mark.tripwire
def test_canonical_fields_locked():
    """MC speaks exactly these names downstream of the translator."""
    canonical = set(FIELD_ALIASES.values())
    assert {
        "symbol",
        "broker_order_id",
        "execution_receipt_id",
        "filled_qty",
        "direction",
        "confidence",
    }.issubset(canonical)

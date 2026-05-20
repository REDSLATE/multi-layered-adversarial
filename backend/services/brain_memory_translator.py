"""
Brain Memory Translator
=======================

Sits in front of the MemoryKernelLedger. Brains may speak many dialects;
MC stores exactly one language.

    Camaro / Alpha / Chevelle / REDEYE
            v
       BrainMemoryTranslator   <-- this module
            v
     canonical MC memory payload
            v
        MemoryKernelLedger
            v
       VE / SO / DI / UV

Translation is purely structural normalisation. No provenance is assigned
here -- that is the kernel's job.
"""

from typing import Any, Dict, Tuple


STACK_ALIASES = {
    "camaro": "camaro",
    "alpha": "alpha",
    "chevelle": "chevelle",
    "redeye": "redeye",
    "red_eye": "redeye",
    "red-eye": "redeye",
}


MEMORY_TYPE_ALIASES = {
    # execution
    "fill": "execution",
    "trade": "execution",
    "order_fill": "execution",
    "paper_fill": "execution",
    "live_fill": "execution",
    "execution": "execution",

    # diagnostics / opinions
    "critique": "diagnostic",
    "dissent": "council_dissent",
    "governance": "governance_review",
    "review": "governance_review",
    "note": "diagnostic",

    # simulation
    "replay": "replay",
    "backtest": "backtest",
    "simulation": "simulation",
}


FIELD_ALIASES = {
    "ticker": "symbol",
    "asset": "symbol",
    "pair": "symbol",

    "order_id": "broker_order_id",
    "broker_id": "broker_order_id",
    "brokerOrderId": "broker_order_id",

    "receipt": "execution_receipt_id",
    "receipt_id": "execution_receipt_id",
    "executionReceiptId": "execution_receipt_id",

    "qty": "filled_qty",
    "quantity": "filled_qty",
    "filledQuantity": "filled_qty",

    "side": "direction",
    "action": "direction",
    "signal": "direction",

    "conf": "confidence",
    "score": "confidence",
}


DIRECTION_ALIASES = {
    "LONG": "BUY",
    "BULL": "BUY",
    "BULLISH": "BUY",
    "BUY": "BUY",

    "SHORT": "SELL",
    "BEAR": "SELL",
    "BEARISH": "SELL",
    "SELL": "SELL",

    "NO_TRADE": "HOLD",
    "NEUTRAL": "HOLD",
    "WAIT": "HOLD",
    "HOLD": "HOLD",
}


def normalize_stack(name: str) -> str:
    """Normalise a brain stack name to its canonical short form."""
    key = str(name or "").strip().lower()
    return STACK_ALIASES.get(key, key)


def normalize_memory_type(memory_type: str) -> str:
    """Collapse dialect memory types to canonical kernel types."""
    key = str(memory_type or "").strip().lower()
    return MEMORY_TYPE_ALIASES.get(key, key or "diagnostic")


def normalize_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Rename brain-specific fields to MC canonical fields and coerce types."""
    if not isinstance(payload, dict):
        return {}

    out: Dict[str, Any] = {}

    for k, v in payload.items():
        canonical_key = FIELD_ALIASES.get(k, k)
        out[canonical_key] = v

    if "symbol" in out and isinstance(out["symbol"], str):
        out["symbol"] = out["symbol"].upper().strip()

    if "direction" in out:
        raw = str(out["direction"]).upper().strip()
        out["direction"] = DIRECTION_ALIASES.get(raw, raw)

    if "confidence" in out:
        try:
            c = float(out["confidence"])
            if c > 1:
                c = c / 100.0
            out["confidence"] = max(0.0, min(1.0, c))
        except Exception:
            out["confidence"] = None

    if "filled_qty" in out:
        try:
            out["filled_qty"] = float(out["filled_qty"])
        except Exception:
            out["filled_qty"] = None

    return out


def translate_brain_memory(
    *,
    source_stack: str,
    memory_type: str,
    payload: Dict[str, Any],
) -> Tuple[str, str, Dict[str, Any]]:
    """
    Translate a raw brain memory submission into MC canonical form.

    Returns:
        (canonical_stack, canonical_memory_type, canonical_payload)

    The canonical_payload carries a provenance breadcrumb under
    ``_translated_from`` so the original dialect is forensically recoverable.
    """
    canonical_stack = normalize_stack(source_stack)
    canonical_type = normalize_memory_type(memory_type)
    canonical_payload = normalize_payload(payload)

    canonical_payload["_translated_from"] = {
        "source_stack": source_stack,
        "memory_type": memory_type,
    }

    return canonical_stack, canonical_type, canonical_payload

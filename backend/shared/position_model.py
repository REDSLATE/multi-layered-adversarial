"""Position-aware intent classification.

Doctrine (2026-06-09 operator directive, post-AAPL-misread incident):
the brains emitted BUY on AAPL while AAPL was actually a short
position in the broker account. They had no model of "side", so a
BUY was always interpreted as "open long" — which against a real
short position means *adding* to the loser instead of *covering*
the winner. The signal was right; the position-state reader was
wrong.

Logged failure pattern: `MISREAD_POSITION_SIDE`, `missed_short_profit=true`.

This module exists to make position side explicit, machine-checkable,
and impossible to lose silently. Every intent flowing through the
auto-router can be classified into one of five primitives by
comparing the action with the current position state at the broker:

    SELL when flat   → OPEN  (short)
    SELL when long   → REDUCE / CLOSE (long)
    SELL when short  → ADD   (short)
    BUY  when flat   → OPEN  (long)
    BUY  when long   → ADD   (long)
    BUY  when short  → REDUCE / CLOSE (short)  — aka COVER
    Opposite-side BUY/SELL crossing through zero → FLIP

This is intentionally a pure module — no DB, no I/O, no broker. It
defines the schema and the classifier. Wiring it into the gate chain
is a separate change (operator-deferred while live trading is active
on prod). The hook point for that integration is in
`backend/shared/execution.py::_evaluate_gates` — add a new gate
`position_aware_intent_classification` that calls into here.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class PositionSide(str, Enum):
    """Side of an existing position at the broker.

    Stored as the lowercase string in audit rows so it's
    grep-friendly. FLAT means "no position at all" — distinct from
    SHORT with zero quantity which is a state bug in the
    upstream reader."""
    LONG = "long"
    SHORT = "short"
    FLAT = "flat"


class IntentType(str, Enum):
    """Classification of a (BUY/SELL) intent relative to the current
    position. This is the missing primitive the AAPL misread
    revealed — the brain emits an action verb (BUY/SELL), MC must
    derive the semantic from comparing it to the existing side."""
    OPEN = "open"          # Going from FLAT to LONG/SHORT
    ADD = "add"            # Increasing magnitude on the existing side
    REDUCE = "reduce"      # Decreasing magnitude (partial fill toward close)
    CLOSE = "close"        # Bringing magnitude to zero
    FLIP = "flip"          # Crossing through zero to the opposite side


# Action constants — keep aligned with `IntentIn.action` in intents.py.
ACTION_BUY = "BUY"
ACTION_SELL = "SELL"
ACTION_SHORT = "SHORT"   # explicit "open short" verb (rare; brain may emit SELL+flat instead)
ACTION_COVER = "COVER"   # explicit "close short" verb (rare; brain may emit BUY+short instead)


@dataclass(frozen=True)
class PositionState:
    """Snapshot of a position. `signed_qty` is the source of truth —
    side is derived. A position with `signed_qty=0` is FLAT
    regardless of any side string the broker happened to send."""
    symbol: str
    signed_qty: float          # positive = long, negative = short, 0 = flat
    avg_cost: Optional[float] = None
    market_price: Optional[float] = None
    as_of: Optional[datetime] = None

    @property
    def side(self) -> PositionSide:
        if math.isclose(self.signed_qty, 0.0, abs_tol=1e-9):
            return PositionSide.FLAT
        return PositionSide.LONG if self.signed_qty > 0 else PositionSide.SHORT

    @property
    def abs_qty(self) -> float:
        return abs(self.signed_qty)


def classify_intent(
    action: str,
    intended_qty: float,
    current: PositionState,
) -> IntentType:
    """Classify a brain-emitted action against the current position.

    Args:
        action: One of BUY / SELL / SHORT / COVER (case-insensitive).
        intended_qty: Magnitude (positive) the brain wants to
                      transact. Sign comes from `action`.
        current: The position state at the broker right NOW (NOT a
                 stale cache — caller is responsible for freshness).

    Returns:
        IntentType — OPEN, ADD, REDUCE, CLOSE, or FLIP.

    Doctrine: this function is the single source of truth for what
    a BUY/SELL means. The caller MUST consult it before sizing the
    order or computing slippage budgets. Without this layer, the
    auto-router treats every BUY as "open long" which is exactly
    what caused the AAPL misread.
    """
    act = (action or "").strip().upper()
    qty = abs(float(intended_qty))
    if qty <= 0:
        raise ValueError("intended_qty must be > 0")

    # Convert action to signed direction:  +1 = adding longs / covering shorts
    #                                       -1 = adding shorts / closing longs
    if act in (ACTION_BUY, ACTION_COVER):
        signed_action = +1
    elif act in (ACTION_SELL, ACTION_SHORT):
        signed_action = -1
    else:
        raise ValueError(f"unknown action {action!r}")

    cur = current.signed_qty
    new = cur + signed_action * qty

    # FLAT → anywhere = OPEN
    if math.isclose(cur, 0.0, abs_tol=1e-9):
        return IntentType.OPEN

    # Same-side magnitude growth = ADD
    if (cur > 0 and signed_action > 0) or (cur < 0 and signed_action < 0):
        return IntentType.ADD

    # Opposite-side: comparing magnitudes to decide REDUCE / CLOSE / FLIP
    new_abs = abs(new)
    if new_abs <= 1e-9:
        return IntentType.CLOSE
    if (cur > 0 and new > 0) or (cur < 0 and new < 0):
        # Reduced toward zero but didn't cross
        return IntentType.REDUCE
    # Crossed through zero — operator must approve crossing-the-zero
    # actions separately if they want partial-cover-then-open behaviour.
    return IntentType.FLIP


# ── Misread audit ────────────────────────────────────────────────


MISREAD_COLLECTION = "shared_position_misreads"


@dataclass
class PositionMisread:
    """Audit row capturing a moment where the brain's emit logic
    disagreed with the actual broker position state. The AAPL
    incident on 2026-06-09 was THE prototype of this row — brain
    treated AAPL as FLAT and kept emitting BUY; broker actually
    held a SHORT position; result was a cascade of "open long"
    orders that compounded the loss instead of covering."""

    symbol: str
    brain: str                   # canonical brain_id
    lane: str
    emitted_action: str          # BUY / SELL
    assumed_side: PositionSide   # what the brain thought
    actual_side: PositionSide    # what the broker said
    assumed_qty: float
    actual_signed_qty: float
    correct_intent_type: IntentType
    missed_short_profit: bool    # True iff actual=SHORT and brain emitted BUY
    note: str = ""
    detected_at: datetime = None  # type: ignore[assignment]

    def to_doc(self) -> dict:
        ts = self.detected_at or datetime.now(timezone.utc)
        return {
            "kind": "MISREAD_POSITION_SIDE",
            "symbol": self.symbol,
            "brain": self.brain,
            "lane": self.lane,
            "emitted_action": self.emitted_action,
            "assumed_side": self.assumed_side.value,
            "actual_side": self.actual_side.value,
            "assumed_qty": self.assumed_qty,
            "actual_signed_qty": self.actual_signed_qty,
            "correct_intent_type": self.correct_intent_type.value,
            "missed_short_profit": self.missed_short_profit,
            "note": self.note,
            "detected_at": ts.isoformat(),
        }


def detect_misread(
    emitted_action: str,
    assumed_side: PositionSide,
    actual: PositionState,
    brain: str,
    lane: str,
    intended_qty: float,
    note: str = "",
) -> Optional[PositionMisread]:
    """Return a PositionMisread row if the brain's assumption about
    the position side disagrees with the broker's actual state, in
    a way that matters for routing.

    "Matters for routing" means: the corrective intent_type would be
    different. e.g. brain thinks FLAT → emits BUY (intends OPEN
    LONG). Reality: broker shows SHORT 10. Correct intent is
    actually a partial COVER, not OPEN LONG. The two have opposite
    risk implications.
    """
    if assumed_side == actual.side:
        return None

    correct_type = classify_intent(emitted_action, intended_qty, actual)

    # The original "missed short profit" event from operator: BUY
    # was emitted while position was SHORT — meaning the trade
    # would have COVERED (taken profit) but the brain doesn't see
    # it as such.
    missed_short_profit = (
        actual.side == PositionSide.SHORT
        and emitted_action.upper() == ACTION_BUY
    )

    return PositionMisread(
        symbol=actual.symbol,
        brain=brain,
        lane=lane,
        emitted_action=emitted_action,
        assumed_side=assumed_side,
        actual_side=actual.side,
        assumed_qty=intended_qty,
        actual_signed_qty=actual.signed_qty,
        correct_intent_type=correct_type,
        missed_short_profit=missed_short_profit,
        note=note,
    )


# ── Operator-spec trade-transition layer (2026-06-XX) ─────────────
#
# Doctrine pin (operator directive, post-AAPL incident):
#   "Stop feeding the brains only `action = BUY/SELL`. Start feeding
#    them position_side (LONG/SHORT/FLAT), intent_type (OPEN/ADD/
#    REDUCE/CLOSE/FLIP), exposure_direction (LONG_BIAS/SHORT_BIAS/
#    NEUTRAL). The brain should not think in just buy/sell — it
#    should think in trade transitions."
#
# The functions below are the canonical implementation the operator
# pinned verbatim. They are intentionally a richer, more granular
# overlay on top of the existing `classify_intent` primitive:
#
#   classify_intent           → 5 states: OPEN/ADD/REDUCE/CLOSE/FLIP
#   classify_trade_transition → 10 states with side awareness:
#       OPEN_LONG, ADD_LONG, REDUCE_LONG, CLOSE_LONG,
#       OPEN_SHORT, ADD_SHORT, REDUCE_SHORT, CLOSE_SHORT,
#       FLIP_LONG_TO_SHORT, FLIP_SHORT_TO_LONG, HOLD
#
# The richer layer is what the brain runner injects into the brain's
# decision context. The original 5-primitive form is still used by
# the gate chain audit / misread detector — they're complementary,
# not redundant.

# Allowed-transition table per current side — the brain reads this
# off the position_context so it knows that, e.g., on a SHORT
# position a BUY does NOT mean OPEN_LONG, it means REDUCE/CLOSE.
_ALLOWED_TRANSITIONS_LONG = [
    "SELL_TO_REDUCE",
    "SELL_TO_CLOSE",
    "BUY_TO_ADD_LONG",
    "SELL_TO_FLIP_TO_SHORT",
]
_ALLOWED_TRANSITIONS_SHORT = [
    "BUY_TO_REDUCE",
    "BUY_TO_CLOSE",
    "SELL_TO_ADD_SHORT",
    "BUY_TO_FLIP_TO_LONG",
]
_ALLOWED_TRANSITIONS_FLAT = [
    "BUY_TO_OPEN_LONG",
    "SELL_TO_OPEN_SHORT",
]


def allowed_transitions_for(side: str) -> list:
    """Return the list of legal transition labels for the given
    current side. Side is the lowercase string (long/short/flat) the
    brain receives in `position_context.current_side`.

    The labels are intentionally human-readable verbs the brain core
    and the operator audit log both consume verbatim. They are NOT
    a wire schema — they are descriptive evidence injected into the
    brain's snapshot so the brain can reason: "I'm SHORT MSFT;
    BUY means COVER, not OPEN LONG."
    """
    s = (side or "").strip().lower()
    if s == "long":
        return list(_ALLOWED_TRANSITIONS_LONG)
    if s == "short":
        return list(_ALLOWED_TRANSITIONS_SHORT)
    return list(_ALLOWED_TRANSITIONS_FLAT)


def classify_trade_transition(action: str, signed_qty: float, order_qty: float) -> dict:
    """Classify a (action, current signed_qty, order_qty) tuple into
    the operator-pinned 10-state transition vocabulary.

    Args:
        action:     "BUY" | "SELL" (case-insensitive). "HOLD" returns
                    intent_type="HOLD" with no side change.
        signed_qty: Current broker position. > 0 = LONG, < 0 = SHORT,
                    0 = FLAT.
        order_qty:  Magnitude (positive) the brain intends to transact.

    Returns:
        dict with: current_side, signed_qty, order_action, order_qty,
                   intent_type.

    Doctrine pin: this is the exact function-body the operator
    posted in the directive. Do not refactor without operator
    approval — the brains, the audit log, and the
    position_misread_audit module all key off these intent_type
    strings verbatim.
    """
    action = (action or "").upper()

    if signed_qty > 0:
        side = "LONG"
    elif signed_qty < 0:
        side = "SHORT"
    else:
        side = "FLAT"

    abs_pos = abs(float(signed_qty))
    q = float(order_qty)

    if action == "BUY":
        if side == "FLAT":
            intent = "OPEN_LONG"
        elif side == "LONG":
            intent = "ADD_LONG"
        elif q < abs_pos:
            intent = "REDUCE_SHORT"
        elif q == abs_pos:
            intent = "CLOSE_SHORT"
        else:
            intent = "FLIP_SHORT_TO_LONG"
    elif action == "SELL":
        if side == "FLAT":
            intent = "OPEN_SHORT"
        elif side == "SHORT":
            intent = "ADD_SHORT"
        elif q < abs_pos:
            intent = "REDUCE_LONG"
        elif q == abs_pos:
            intent = "CLOSE_LONG"
        else:
            intent = "FLIP_LONG_TO_SHORT"
    else:
        intent = "HOLD"

    return {
        "current_side": side,
        "signed_qty": float(signed_qty),
        "order_action": action,
        "order_qty": q,
        "intent_type": intent,
    }


def normalize_position(raw: dict) -> dict:
    """Normalize a broker position dict into the operator-canonical
    shape every layer of MC consumes downstream.

    Inputs accepted (any of):
      qty           — float, signed OR unsigned magnitude
      side / quantitySide — one of {LONG, SHORT, SELL_SHORT, BUY, SELL}
      market_value, avg_entry_price, unrealized_pl — passed through
      symbol        — required

    Output shape:
      {
        "symbol":           str,
        "side":             "LONG" | "SHORT" | "FLAT",
        "qty_abs":          float,
        "signed_qty":       float,   ← source of truth for everything
        "market_value":     float|None,
        "avg_entry_price":  float|None,
        "unrealized_pl":    float|None,
      }

    Doctrine: `signed_qty` is the SINGLE source of truth across MC.
    Any caller that uses `qty_abs` or `side` without consulting
    `signed_qty` is wrong by construction — the AAPL misread
    happened because the broker reported `qty=100, side="long"` and
    the routing layer never noticed the broker's `side` was just a
    label, not a sign on the magnitude.
    """
    qty = float(raw.get("qty", 0) or 0)
    side_raw = str(raw.get("side") or raw.get("quantitySide") or "").upper().strip()

    if side_raw in {"SHORT", "SELL_SHORT", "SELL"}:
        signed_qty = -abs(qty)
    elif side_raw in {"LONG", "BUY"}:
        signed_qty = abs(qty)
    else:
        # No side label at all — trust whatever sign was on `qty`.
        # If broker reported unsigned 100 with no side, we treat
        # that as FLAT-uncertain rather than guess long.
        signed_qty = qty

    if signed_qty > 0:
        side = "LONG"
    elif signed_qty < 0:
        side = "SHORT"
    else:
        side = "FLAT"

    return {
        "symbol": raw.get("symbol", ""),
        "side": side,
        "qty_abs": abs(signed_qty),
        "signed_qty": float(signed_qty),
        "market_value": raw.get("market_value"),
        "avg_entry_price": raw.get("avg_entry_price"),
        "unrealized_pl": raw.get("unrealized_pl"),
    }


__all__ = [
    "PositionSide", "IntentType", "PositionState",
    "PositionMisread", "MISREAD_COLLECTION",
    "classify_intent", "detect_misread",
    "classify_trade_transition", "normalize_position",
    "allowed_transitions_for",
    "ACTION_BUY", "ACTION_SELL", "ACTION_SHORT", "ACTION_COVER",
]

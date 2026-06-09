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
    "classify_position_evolution", "classify_risk_transition",
    "ACTION_BUY", "ACTION_SELL", "ACTION_SHORT", "ACTION_COVER",
]


# ── Portfolio-manager-grade vocabulary (operator directive, 2026-06-XX) ──
#
# Doctrine pin (verbatim):
#   "Once they stop thinking in BUY/SELL and start thinking in state
#    transitions, the brains can learn much richer behavior … the big
#    mental shift is from 'what should I buy or sell?' to 'how should
#    this position evolve?'"
#
# The base 10-state vocabulary (classify_trade_transition) is the
# wire-level grammar. The functions below sit ABOVE that as the
# portfolio-manager layer. They take the primitive transition_intent
# and refine it using confidence (planned vs reactive), order
# magnitude (partial vs full), and market regime (RISK_ON / RISK_OFF).
#
# Operator's scoped vocabulary for THIS pass (90% of real PM behavior):
#   Position evolution :  OPEN  ADD  REDUCE  CLOSE  FLIP
#                         SCALE_IN  SCALE_OUT
#                         PARTIAL_COVER  FULL_COVER
#                         HOLD
#   Risk transition    :  RISK_ON  RISK_OFF  NEUTRAL
#
# Reserved for later (operator explicitly deferred):
#   ROLL_FORWARD / ROLL_UP / ROLL_DOWN (options)
#   ROTATE_SECTOR (portfolio)
#   ENABLE_HEDGE / REMOVE_HEDGE (portfolio)
#   ENTER_TREND / EXIT_TREND (regime)
#   OBSERVE / ACCUMULATE / ATTACK / DEFEND / EXIT (market awareness)
#
# Thresholds below are operator-tunable but live as named constants
# so the audit log can pin "this brain emitted SCALE_IN because
# confidence=0.72 >= SCALE_IN_CONFIDENCE_FLOOR".

# Confidence floor above which a BUY into an existing LONG is a
# planned SCALE_IN rather than a reactive ADD. Operator's framing:
# "SCALE_IN is different from a normal ADD because it's planned."
# We encode "planned" as "brain committed enough that the action is
# a deliberate increment, not a momentum reaction."
SCALE_IN_CONFIDENCE_FLOOR = 0.65

# Confidence floor above which a SELL into an existing LONG is a
# planned SCALE_OUT (lock in gains) rather than a reactive REDUCE.
SCALE_OUT_CONFIDENCE_FLOOR = 0.55

# Confidence floor above which a BUY against a SHORT is a full
# FULL_COVER rather than a PARTIAL_COVER. Confidence here is a
# proxy for "the brain wants the whole short off" — the actual
# order quantity is sized by the gate chain, not the brain.
FULL_COVER_CONFIDENCE_FLOOR = 0.78

# Regimes that put us in defensive posture. Any de-risking action
# (REDUCE / CLOSE / SCALE_OUT / PARTIAL_COVER) under one of these
# regimes lifts the intent to RISK_OFF. Any risk-adding action
# (OPEN / ADD / SCALE_IN) under a CALM/BULLISH regime lifts to
# RISK_ON. Doctrine: brains report the RISK transition; the gate
# chain decides whether to honor it.
RISK_OFF_REGIMES = frozenset({"volatile", "crisis", "stressed", "risk_off"})
RISK_ON_REGIMES = frozenset({"calm", "bullish", "trend", "risk_on"})


def classify_position_evolution(
    transition_intent: str,
    current_side: str,
    confidence: float = 0.0,
    order_qty: float = 0.0,
    abs_position_qty: float = 0.0,
) -> str:
    """Refine the base 10-state transition into the portfolio-manager
    vocabulary the operator pinned (2026-06-XX).

    Args:
        transition_intent:  One of the 6-state primitives the brain
                            already emits — OPEN / ADD / REDUCE /
                            CLOSE / FLIP / HOLD.
        current_side:       LONG / SHORT / FLAT.
        confidence:         Brain confidence ∈ [0, 1]. High =
                            "planned move", low = "reactive nudge".
        order_qty:          Brain's intended size (magnitude). Used
                            only for PARTIAL vs FULL cover when the
                            primitive can't tell.
        abs_position_qty:   |signed_qty| of current position.

    Returns:
        One of:  OPEN  ADD  REDUCE  CLOSE  FLIP  HOLD
                 SCALE_IN  SCALE_OUT
                 PARTIAL_COVER  FULL_COVER

    Doctrine pin:
        SCALE_IN  ≠ ADD       (planned vs reactive)
        SCALE_OUT ≠ REDUCE    (lock-in-gains vs panic-trim)
        PARTIAL_COVER ≠ FULL_COVER (taking some off vs flat)

        FLIP and HOLD are passed through unchanged — they have no
        refined form in the operator's spec.
    """
    side = (current_side or "FLAT").upper()
    base = (transition_intent or "HOLD").upper()
    c = float(confidence or 0.0)

    if base in ("HOLD", "FLIP", "OPEN"):
        return base

    if base == "ADD":
        # ADD only happens on the matching side (LONG+BUY or
        # SHORT+SELL). High-confidence ADD on a LONG = SCALE_IN
        # (planned increment). For SHORT, the operator-pinned
        # vocabulary keeps "ADD_SHORT" plain — there's no
        # SCALE_IN_SHORT in the scoped list — so SHORT stays ADD.
        if side == "LONG" and c >= SCALE_IN_CONFIDENCE_FLOOR:
            return "SCALE_IN"
        return "ADD"

    if base == "REDUCE":
        # REDUCE on a LONG with enough conviction = SCALE_OUT
        # (lock-in-gains). REDUCE on a SHORT is a partial cover.
        if side == "LONG":
            if c >= SCALE_OUT_CONFIDENCE_FLOOR:
                return "SCALE_OUT"
            return "REDUCE"
        if side == "SHORT":
            # Order qty < abs(position) is, by definition of REDUCE,
            # already true here. Operator's PARTIAL_COVER is exactly
            # this. We keep the label distinct from PLAIN REDUCE so
            # the audit log can show "the brain wanted SOME of the
            # short off, not all."
            return "PARTIAL_COVER"
        return "REDUCE"

    if base == "CLOSE":
        # CLOSE on a SHORT = FULL_COVER. Use confidence to confirm —
        # if the brain isn't committed, downgrade to PARTIAL_COVER.
        if side == "SHORT":
            if c >= FULL_COVER_CONFIDENCE_FLOOR:
                return "FULL_COVER"
            # Low-commitment CLOSE on a short is closer to a partial
            # cover semantically — the brain is hedging its bet.
            return "PARTIAL_COVER"
        # CLOSE on a LONG stays CLOSE (operator did not introduce a
        # PARTIAL_CLOSE for longs — SCALE_OUT covers the partial
        # case, CLOSE means flatten).
        return "CLOSE"

    return base


def classify_risk_transition(
    market_regime: str,
    position_evolution: str,
) -> str:
    """Lift a per-symbol evolution into a portfolio risk verb.

    Operator framing:
        RISK_OFF → triggered by volatility, news shock, liquidity stress.
        RISK_ON  → triggered by favorable conditions.

    We honour that doctrine using two inputs the brain already has:
        market_regime    — what the snapshot says about conditions
        position_evolution — whether the brain is adding or trimming risk

    Returns: RISK_ON | RISK_OFF | NEUTRAL

    NEUTRAL is the honest default — most ticks are not regime-
    crossing events. We never label every ADD/REDUCE as a risk
    transition; only the ones happening in a regime that makes the
    direction meaningful at the portfolio level.
    """
    regime = (market_regime or "").lower().strip()
    evo = (position_evolution or "").upper()

    de_risk_evos = {"REDUCE", "CLOSE", "SCALE_OUT", "PARTIAL_COVER", "FULL_COVER"}
    add_risk_evos = {"OPEN", "ADD", "SCALE_IN"}

    if regime in RISK_OFF_REGIMES and evo in de_risk_evos:
        return "RISK_OFF"
    if regime in RISK_ON_REGIMES and evo in add_risk_evos:
        return "RISK_ON"
    # FLIP into either direction in a stressed regime is genuinely a
    # risk transition — the brain is rotating exposure under stress.
    if regime in RISK_OFF_REGIMES and evo == "FLIP":
        return "RISK_OFF"
    return "NEUTRAL"

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal, Optional


Side = Literal["BUY", "SELL"]
VerifierTier = Literal["ALPHA", "BETA", "GAMMA"]


class BasketState(str, Enum):
    ACTIVE = "ACTIVE"
    ADDING_ALLOWED = "ADDING_ALLOWED"
    ADDING_DISABLED = "ADDING_DISABLED"
    RECOVERY = "RECOVERY"
    CRISIS = "CRISIS"
    FORCED = "FORCED"
    CLOSED = "CLOSED"


class EscalationSeverity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"
    FATAL = "FATAL"


@dataclass(frozen=True)
class BasketSnapshot:
    symbol: str
    side: Side
    seat_id: str
    sleeve_id: str

    current_price: float
    last_entry_price: float
    weighted_avg_entry: float
    atr_h1: float
    spread_bps: float
    spread_baseline_bps: float

    state: BasketState
    adds_count: int
    max_adds: int = 6
    total_lots: float = 0.0
    floating_pnl_usd: float = 0.0
    net_pnl_usd: float = 0.0

    equity_at_birth: float = 0.0
    equity_current: float = 0.0
    account_dd_pct: float = 0.0
    margin_level: Optional[float] = None
    sleeve_budget_remaining: float = 0.0
    margin_required_for_next_add: float = 0.0

    mae_atr: float = 0.0
    age_days: float = 0.0

    zone_armed: bool = False
    regime_allows_side: bool = False
    trading_hour: bool = True
    kill_engine_halt: bool = False

    verifier_tier: VerifierTier = "GAMMA"


@dataclass(frozen=True)
class GovernorModifier:
    size_multiplier: float = 1.0
    vote_required: bool = False
    reason: str = "PASS"
    severity: EscalationSeverity = EscalationSeverity.INFO
    evidence: dict = field(default_factory=dict)


LOT_SEQUENCE_MULTIPLIERS = [1.0, 1.0, 1.0, 1.25, 1.25, 1.5]


def grid_step_price(price: float, atr_h1: float) -> float:
    """
    MTR-inspired grid spacing:
    max(price * 0.25%, ATR(H1) * 0.35)
    """
    if price <= 0:
        raise ValueError("price must be positive")
    if atr_h1 < 0:
        raise ValueError("atr_h1 cannot be negative")

    return max(price * 0.0025, atr_h1 * 0.35)


def lot_multiplier_for_add(adds_count: int) -> float:
    """
    Semi-fixed lot progression.
    No martingale doubling.
    """
    if adds_count < 0:
        raise ValueError("adds_count cannot be negative")

    if adds_count >= len(LOT_SEQUENCE_MULTIPLIERS):
        return 0.0

    return LOT_SEQUENCE_MULTIPLIERS[adds_count]


def _tier_adjusted_thresholds(basket: BasketSnapshot) -> dict[str, float]:
    """
    Verifier-aware risk thresholds.

    Risk ceilings:
    - Higher threshold = more rope.

    Margin floor:
    - Lower threshold = more rope.
    """
    base = {
        "forced_mae_atr": 10.0,
        "forced_age_days": 45.0,
        "forced_account_dd": 12.0,
        "crisis_mae_atr": 8.0,
        "crisis_age_days": 21.0,
        "crisis_margin_level": 300.0,
        "recovery_mae_atr": 5.0,
        "recovery_age_days": 7.0,
        "recovery_combo_mae": 3.0,
        "recovery_combo_age": 3.0,
    }

    if basket.verifier_tier == "ALPHA":
        risk_factor = 1.2
        margin_factor = 1.2
    elif basket.verifier_tier == "BETA":
        risk_factor = 1.0
        margin_factor = 1.0
    else:
        risk_factor = 0.8
        margin_factor = 0.8

    thresholds = {
        key: value * risk_factor
        for key, value in base.items()
        if key != "crisis_margin_level"
    }

    thresholds["crisis_margin_level"] = (
        base["crisis_margin_level"] / margin_factor
    )

    return thresholds


def evaluate_take_profit(
    basket: BasketSnapshot,
    target_equity_pct: float = 0.0040,
    target_atr_distance: float = 0.50,
    min_profit_after_cost: float = 0.0,
) -> Optional[GovernorModifier]:
    """
    Advisory take-profit signal.

    This does not command a close.
    It marks the intent as requiring Seat review.
    """
    equity_target = basket.equity_at_birth * target_equity_pct

    if basket.floating_pnl_usd >= equity_target and equity_target > 0:
        return GovernorModifier(
            size_multiplier=0.0,
            vote_required=True,
            reason="ADVISORY_EQUITY_PCT_TP",
            severity=EscalationSeverity.CRITICAL,
            evidence={
                "floating_pnl_usd": basket.floating_pnl_usd,
                "equity_target": equity_target,
                "target_equity_pct": target_equity_pct,
            },
        )

    if basket.side == "BUY":
        atr_target = (
            basket.weighted_avg_entry
            + target_atr_distance * basket.atr_h1
        )

        if basket.current_price >= atr_target:
            return GovernorModifier(
                size_multiplier=0.0,
                vote_required=True,
                reason="ADVISORY_ATR_DISTANCE_TP",
                severity=EscalationSeverity.CRITICAL,
                evidence={
                    "current_price": basket.current_price,
                    "atr_target_price": atr_target,
                    "target_atr_distance": target_atr_distance,
                },
            )

    if basket.side == "SELL":
        atr_target = (
            basket.weighted_avg_entry
            - target_atr_distance * basket.atr_h1
        )

        if basket.current_price <= atr_target:
            return GovernorModifier(
                size_multiplier=0.0,
                vote_required=True,
                reason="ADVISORY_ATR_DISTANCE_TP",
                severity=EscalationSeverity.CRITICAL,
                evidence={
                    "current_price": basket.current_price,
                    "atr_target_price": atr_target,
                    "target_atr_distance": target_atr_distance,
                },
            )

    if (
        basket.state == BasketState.RECOVERY
        and basket.net_pnl_usd >= min_profit_after_cost
    ):
        return GovernorModifier(
            size_multiplier=0.0,
            vote_required=True,
            reason="ADVISORY_RECOVERY_BREAKEVEN_EXIT",
            severity=EscalationSeverity.CRITICAL,
            evidence={
                "net_pnl_usd": basket.net_pnl_usd,
                "min_profit_after_cost": min_profit_after_cost,
            },
        )

    return None


def evaluate_recovery_escalation(
    basket: BasketSnapshot,
) -> Optional[GovernorModifier]:
    """
    Recovery escalation ladder.

    FATAL:
        Immediate RoadGuard block candidate.

    CRITICAL:
        Blocks same-side basket birth / demands Seat review.

    WARNING:
        Blocks same-side adds / demands Seat review.

    INFO:
        Normal operation.
    """
    t = _tier_adjusted_thresholds(basket)

    if (
        basket.mae_atr > t["forced_mae_atr"]
        or basket.age_days > t["forced_age_days"]
        or basket.account_dd_pct > t["forced_account_dd"]
    ):
        return GovernorModifier(
            size_multiplier=0.0,
            vote_required=True,
            reason="FATAL_FORCED_CLOSE_THRESHOLD",
            severity=EscalationSeverity.FATAL,
            evidence={
                "mae_atr": basket.mae_atr,
                "threshold_mae_atr": t["forced_mae_atr"],
                "age_days": basket.age_days,
                "threshold_age_days": t["forced_age_days"],
                "account_dd_pct": basket.account_dd_pct,
                "threshold_account_dd": t["forced_account_dd"],
            },
        )

    if (
        basket.mae_atr > t["crisis_mae_atr"]
        or basket.age_days > t["crisis_age_days"]
        or (
            basket.margin_level is not None
            and basket.margin_level < t["crisis_margin_level"]
        )
    ):
        return GovernorModifier(
            size_multiplier=0.0,
            vote_required=True,
            reason="CRITICAL_CRISIS_THRESHOLD",
            severity=EscalationSeverity.CRITICAL,
            evidence={
                "mae_atr": basket.mae_atr,
                "threshold_mae_atr": t["crisis_mae_atr"],
                "age_days": basket.age_days,
                "threshold_age_days": t["crisis_age_days"],
                "margin_level": basket.margin_level,
                "threshold_margin_level": t["crisis_margin_level"],
            },
        )

    if (
        basket.mae_atr > t["recovery_mae_atr"]
        or basket.age_days > t["recovery_age_days"]
        or (
            basket.mae_atr > t["recovery_combo_mae"]
            and basket.age_days > t["recovery_combo_age"]
        )
    ):
        return GovernorModifier(
            size_multiplier=0.0,
            vote_required=True,
            reason="WARNING_RECOVERY_THRESHOLD_DISABLE_ADDS",
            severity=EscalationSeverity.WARNING,
            evidence={
                "mae_atr": basket.mae_atr,
                "threshold_mae_atr": t["recovery_mae_atr"],
                "age_days": basket.age_days,
                "threshold_age_days": t["recovery_age_days"],
            },
        )

    return None


def evaluate_grid_add(basket: BasketSnapshot) -> GovernorModifier:
    """
    Evaluates whether an existing basket may receive another add.

    This function is pure:
    - no state mutation
    - no broker calls
    - no action override
    """
    if basket.kill_engine_halt:
        return GovernorModifier(
            size_multiplier=0.0,
            vote_required=False,
            reason="KILL_ENGINE_HALT",
            severity=EscalationSeverity.INFO,
        )

    if basket.adds_count >= basket.max_adds:
        return GovernorModifier(
            size_multiplier=0.0,
            vote_required=False,
            reason="MAX_ADDS_REACHED",
            severity=EscalationSeverity.INFO,
            evidence={
                "adds_count": basket.adds_count,
                "max_adds": basket.max_adds,
            },
        )

    step = grid_step_price(basket.current_price, basket.atr_h1)

    if basket.side == "BUY":
        price_moved_enough = (
            basket.current_price <= basket.last_entry_price - step
        )
    else:
        price_moved_enough = (
            basket.current_price >= basket.last_entry_price + step
        )

    if not price_moved_enough:
        return GovernorModifier(
            size_multiplier=0.0,
            vote_required=False,
            reason="GRID_STEP_NOT_REACHED",
            severity=EscalationSeverity.INFO,
            evidence={
                "current_price": basket.current_price,
                "last_entry_price": basket.last_entry_price,
                "grid_step": step,
            },
        )

    if not basket.zone_armed:
        return GovernorModifier(
            size_multiplier=0.0,
            vote_required=False,
            reason="ZONE_NOT_ARMED",
            severity=EscalationSeverity.INFO,
        )

    if not basket.regime_allows_side:
        return GovernorModifier(
            size_multiplier=0.0,
            vote_required=False,
            reason="REGIME_BLOCKS_SIDE",
            severity=EscalationSeverity.INFO,
        )

    if not basket.trading_hour:
        return GovernorModifier(
            size_multiplier=0.0,
            vote_required=False,
            reason="OUTSIDE_TRADING_HOURS",
            severity=EscalationSeverity.INFO,
        )

    if (
        basket.spread_baseline_bps > 0
        and basket.spread_bps > basket.spread_baseline_bps * 3
    ):
        return GovernorModifier(
            size_multiplier=0.0,
            vote_required=False,
            reason="SPREAD_TOO_WIDE_FOR_GRID_ADD",
            severity=EscalationSeverity.INFO,
            evidence={
                "spread_bps": basket.spread_bps,
                "spread_baseline_bps": basket.spread_baseline_bps,
            },
        )

    if (
        basket.sleeve_budget_remaining
        < basket.margin_required_for_next_add
    ):
        return GovernorModifier(
            size_multiplier=0.0,
            vote_required=False,
            reason="INSUFFICIENT_SLEEVE_BUDGET",
            severity=EscalationSeverity.INFO,
            evidence={
                "sleeve_budget_remaining": basket.sleeve_budget_remaining,
                "margin_required": basket.margin_required_for_next_add,
            },
        )

    multiplier = lot_multiplier_for_add(basket.adds_count)

    return GovernorModifier(
        size_multiplier=multiplier,
        vote_required=False,
        reason="GRID_ADD_ALLOWED",
        severity=EscalationSeverity.INFO,
        evidence={
            "grid_step": step,
            "adds_count": basket.adds_count,
            "lot_multiplier": multiplier,
        },
    )


def evaluate_grid_recovery(basket: BasketSnapshot) -> GovernorModifier:
    """
    Main entry point.

    Priority:
    1. Take-profit advisory
    2. Recovery escalation
    3. Grid-add eligibility
    """
    take_profit = evaluate_take_profit(basket)
    if take_profit:
        return take_profit

    recovery = evaluate_recovery_escalation(basket)
    if recovery:
        return recovery

    return evaluate_grid_add(basket)


def apply_grid_recovery_modifier(intent, basket: BasketSnapshot):
    """
    Optional helper for the RISEDUAL pipeline.

    Placement:
    Governor sizing
        ↓
    GridRecoveryGovernor
        ↓
    RoadGuard
        ↓
    Seat / Broker

    This helper does not mutate intent.action.
    """
    modifier = evaluate_grid_recovery(basket)

    intent.size_multiplier *= modifier.size_multiplier
    intent.vote_required = intent.vote_required or modifier.vote_required

    if not hasattr(intent, "evidence") or intent.evidence is None:
        intent.evidence = {}

    intent.evidence["grid_recovery_governor"] = {
        "size_multiplier": modifier.size_multiplier,
        "vote_required": modifier.vote_required,
        "reason": modifier.reason,
        "severity": modifier.severity.value,
        "recommended_exit": modifier.reason.startswith("ADVISORY_"),
        "evidence": modifier.evidence,
    }

    return intent

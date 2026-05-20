"""Role-keyed brain doctrine sidecar packet (EQUITY lane).

Doctrine pin (2026-02-17, refactor):
    The SEAT has the restriction, not the brain.

    The packet exposes FOUR role-flavored advisory outputs:

        strategist        → conviction_delta (would Alpha/etc. take it?)
        adversary         → objections + challenge_strength
        governor          → risk_multiplier + block_reasons
        execution_judge   → execution_ready + checks

    Each is wired to a roster seat. `holder` records which brain was
    sitting in that seat at the moment the packet was built. The
    doctrine survives seat rotations untouched — only `holder` shifts.

    Equity seat mapping:
        strategist        → roster seat "decider"
        adversary         → roster seat "opponent"
        governor          → roster seat "governor"
        execution_judge   → roster seat "executor"

    Restrictions are pinned ON THE SEAT, not the brain:
        every seat has `may_execute=False`.
        `execution_judge` has `may_create_direction=False` and
        `requires_existing_trade_intent=True`.

    Mirror crypto twin lives in
    `shared.crypto.doctrine.crypto_brain_sidecars`.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from shared.doctrine.base_labels import build_doctrine_labels


# Doctrine version pinned for downstream consumers (Shelly,
# scorecards, audit) so they can fan out on shape changes.
DOCTRINE_VERSION = "small_account_sidecar_v1"


# role → roster seat name (equity lane). Crypto twin overrides this.
EQUITY_SEAT_MAP = {
    "strategist": "decider",
    "adversary": "opponent",
    "governor": "governor",
    "execution_judge": "executor",
}


def build_all_brain_doctrine_packets(
    snapshot: Dict[str, Any],
    seat_holders: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Build the equity-lane doctrine packet.

    `seat_holders` is an optional `{seat_name: brain_or_None}` map. The
    intent ingest path reads the live roster and passes this in so the
    packet records who was holding each seat at packet build. Pure
    callers (tests, sidecars without DB) leave it None and `holder`
    fields come through as `None`.
    """
    base = build_doctrine_labels(snapshot)
    labels = set(base.labels)
    holders = seat_holders or {}

    strategist = _build_strategist(base, labels, holders.get("decider"))
    adversary = _build_adversary(base, labels, holders.get("opponent"))
    governor = _build_governor(base, labels, holders.get("governor"), snapshot)
    execution_judge = _build_execution_judge(
        base, labels, holders.get("executor"),
    )

    return {
        "event_type": "BRAIN_DOCTRINE_SIDECAR_PACKET",
        "doctrine_version": DOCTRINE_VERSION,
        "lane": "equity",
        "symbol": base.symbol,
        "base_labels": {
            "score": base.score,
            "quality": base.quality,
            "labels": base.labels,
            "reasons": base.reasons,
        },
        "seats": {
            "strategist": strategist,
            "adversary": adversary,
            "governor": governor,
            "execution_judge": execution_judge,
        },
    }


def _build_strategist(base, labels, holder):
    conviction_delta = 0.0
    if base.quality == "A_QUALITY":
        conviction_delta += 0.12
    elif base.quality == "B_QUALITY":
        conviction_delta += 0.05
    elif base.quality == "REJECT":
        conviction_delta -= 0.20
    if "GAPPER" in labels and "HIGH_RELATIVE_VOLUME" in labels:
        conviction_delta += 0.06
    if "NEWS_CATALYST" in labels:
        conviction_delta += 0.04
    if "NO_NEWS_RISK" in labels:
        conviction_delta -= 0.06
    return {
        "role": "strategist",
        "seat": EQUITY_SEAT_MAP["strategist"],
        "holder": holder,
        "conviction_delta": round(conviction_delta, 4),
        "lesson": "Favor high-attention gappers with relative volume, catalyst, and clean pullback structure.",
        "may_execute": False,
        "may_override_direction": False,
    }


def _build_adversary(base, labels, holder):
    objections = []
    if "NO_NEWS_RISK" in labels:
        objections.append("move_not_news_backed")
    if "SPREAD_TOO_WIDE" in labels:
        objections.append("spread_risk")
    if "MARKET_WEAK_REDUCE_RISK" in labels:
        objections.append("weak_market_regime")
    if base.quality in {"C_QUALITY", "REJECT"}:
        objections.append("setup_quality_insufficient")
    if "LOW_FLOAT_SUPPLY_IMBALANCE" not in labels:
        objections.append("supply_imbalance_not_confirmed")
    challenge_strength = min(1.0, 0.20 + 0.18 * len(objections))
    return {
        "role": "adversary",
        "seat": EQUITY_SEAT_MAP["adversary"],
        "holder": holder,
        "challenge_required": bool(objections),
        "challenge_strength": round(challenge_strength, 4),
        "objections": objections,
        "lesson": "Attack weak setups, fake momentum, no-news moves, poor spreads, and weak regimes.",
        "may_execute": False,
        "may_override_direction": False,
    }


def _build_governor(base, labels, holder, snapshot):
    """Doctrine packet's advisory governor view (ADVISORY ONLY — does
    not gate execution; the council's `_governance_verdict` is the
    authoritative gate).

    2026-05-18 operator patch: when the advisory reasons are non-fatal
    (e.g. quality C/B with no hard safety stop), the packet emits
    `display_status="RISK_DOWN"` and a single readable `reason` for
    UI surfacing. When the reasons ARE fatal (three losses, daily max
    loss), the packet still says BLOCK. The UI reads `display_status`
    and `reason` to render the correct chip.
    """
    risk_multiplier = 1.0
    block_reasons = []
    consecutive_losses = int(snapshot.get("consecutive_losses", 0) or 0)
    daily_pnl = float(snapshot.get("daily_pnl", 0.0) or 0.0)
    if base.quality == "A_QUALITY":
        risk_multiplier *= 1.00
    elif base.quality == "B_QUALITY":
        risk_multiplier *= 0.75
    elif base.quality == "C_QUALITY":
        risk_multiplier *= 0.50
    else:
        # REJECT quality is advisory-only — not a fatal safety stop.
        # Downweight aggressively but DON'T zero, so the chip shows
        # RISK_DOWN with reason "doctrine_reject" instead of BLOCK.
        risk_multiplier *= 0.25
        block_reasons.append("doctrine_reject")
    if "MARKET_WEAK_REDUCE_RISK" in labels:
        risk_multiplier *= 0.50
    if "SPREAD_TOO_WIDE" in labels:
        risk_multiplier *= 0.50

    # ── Doctrine (c, 2026-05-20): SIZE ONLY. No fatal stops here.
    # consecutive_losses and daily_pnl losses dampen aggressively but
    # never zero — RoadGuard kills truly unsafe market structure.
    if consecutive_losses >= 3:
        risk_multiplier *= 0.40  # was 0.0
    if daily_pnl <= -100:
        risk_multiplier *= 0.25  # was 0.0
    fatal_stops: list[str] = []  # retained for back-compat; always empty under (c)

    risk_multiplier = max(0.0, min(1.0, risk_multiplier))
    # Floor at 0.10 so a dampened-but-allowed trade can still proceed
    # at minimum size when every other gate passes.
    if risk_multiplier > 0 and risk_multiplier < 0.10:
        risk_multiplier = 0.10
    is_hard_block = False  # doctrine (c): governor never hard-blocks
    display_status = (
        "RISK_DOWN" if (block_reasons or risk_multiplier < 1.0) else "ALLOW"
    )
    # Surface the most-informative single reason for the UI chip.
    primary_reason = block_reasons[0] if block_reasons else None
    return {
        "role": "governor",
        "seat": EQUITY_SEAT_MAP["governor"],
        "holder": holder,
        "risk_multiplier": round(risk_multiplier, 4),
        "governor_action": "modulate",  # doctrine (c): never "block"
        "block_reasons": block_reasons,  # informational only under (c)
        "display_status": display_status,
        "reason": primary_reason,
        "execution_effect": "RISK_DOWN_ONLY" if (block_reasons or risk_multiplier < 1.0) else "ALLOW",
        "lesson": "Governor sizes risk. Quality, regime, spread, losses become dampeners; RoadGuard owns hard kills.",
        "may_execute": False,
        "may_override_direction": False,
    }


def _build_execution_judge(base, labels, holder):
    execution_checks = {
        "quality_ok": base.quality in {"A_QUALITY", "B_QUALITY"},
        "spread_ok": "SPREAD_ACCEPTABLE" in labels,
        "market_not_weak": "MARKET_WEAK_REDUCE_RISK" not in labels,
        "has_attention": "GAPPER" in labels or "HIGH_RELATIVE_VOLUME" in labels,
    }
    return {
        "role": "execution_judge",
        "seat": EQUITY_SEAT_MAP["execution_judge"],
        "holder": holder,
        "execution_ready": all(execution_checks.values()),
        "execution_checks": execution_checks,
        "lesson": "Only execute after independent direction exists and setup quality, spread, attention, and regime are acceptable.",
        "may_execute": False,
        "may_create_direction": False,
        "requires_existing_trade_intent": True,
    }

"""Role-keyed crypto doctrine sidecar packet (CRYPTO lane).

Twin of `shared.doctrine.brain_sidecars` for the crypto lane, with the
same role-flavored shape:

    seats:
        strategist       → roster seat "crypto_decider"
        adversary        → roster seat "crypto_opponent"
        governor         → roster seat "crypto_governor"
        execution_judge  → roster seat "crypto" (= crypto executor)

    holder records WHICH BRAIN was sitting in that seat at packet build.
    Restrictions are pinned ON THE SEAT, not the brain — every seat has
    `may_execute=False` and the doctrine survives seat rotations
    untouched.

Lane isolation: this module imports ONLY from `shared.crypto.doctrine`.
It NEVER imports from `shared.doctrine.*` or `runtimes.*`. See
`tests/test_lane_isolation.py`.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from shared.crypto.doctrine.crypto_labels import label_crypto_snapshot


CRYPTO_SEAT_MAP = {
    "strategist": "crypto_decider",
    "adversary": "crypto_opponent",
    "governor": "crypto_governor",
    "execution_judge": "crypto",
}


def build_crypto_brain_doctrine_packet(
    snapshot: Dict[str, Any],
    seat_holders: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Build the crypto-lane doctrine packet.

    `seat_holders` is an optional `{seat_name: brain_or_None}` map. The
    intent ingest path reads the live roster and passes this in so the
    packet records who was holding each crypto seat at packet build.
    """
    base = label_crypto_snapshot(snapshot)
    labels = set(base.labels)
    holders = seat_holders or {}

    strategist = _build_strategist(
        base, labels, holders.get(CRYPTO_SEAT_MAP["strategist"]),
    )
    adversary = _build_adversary(
        base, labels, holders.get(CRYPTO_SEAT_MAP["adversary"]),
    )
    governor = _build_governor(
        base, labels, holders.get(CRYPTO_SEAT_MAP["governor"]), snapshot,
    )
    execution_judge = _build_execution_judge(
        base, labels, holders.get(CRYPTO_SEAT_MAP["execution_judge"]),
        snapshot,
    )

    return {
        "event_type": "BRAIN_DOCTRINE_SIDECAR_PACKET",
        "doctrine_version": base.doctrine_version,
        "lane": "crypto",
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
    conviction_delta = _alpha_delta(base.score)
    return {
        "role": "strategist",
        "seat": CRYPTO_SEAT_MAP["strategist"],
        "holder": holder,
        "conviction_delta": conviction_delta,
        "quality": base.quality,
        "reasons": list(base.reasons),
        "lesson": "Favor liquid pairs with trend alignment, expanded volatility, neutral funding, and BTC regime support.",
        "may_execute": False,
        "may_override_direction": False,
    }


def _build_adversary(base, labels, holder):
    objections = _redeye_objections(labels)
    return {
        "role": "adversary",
        "seat": CRYPTO_SEAT_MAP["adversary"],
        "holder": holder,
        "challenge_required": bool(objections),
        "challenge_strength": round(1.0 - base.score, 4),
        "objections": objections,
        "lesson": "Attack wide spreads, crowded funding, lopsided liquidations, and dead-vol moves.",
        "may_execute": False,
        "may_override_direction": False,
    }


def _build_governor(base, labels, holder, snapshot):
    """Crypto-side advisory governor packet.

    2026-05-18 operator patch: distinguish FATAL stops (wide spread,
    wrong lane, 3 losses, daily loss limit — true safety) from low
    score (just C-quality, advisory). Low score now risk-downs to a
    minimum floor instead of zeroing.
    """
    risk_multiplier = _chevelle_risk_multiplier(base.score)
    block_reasons = _chevelle_blocks(labels, snapshot)
    is_hard_block = bool(block_reasons)
    if is_hard_block:
        risk_multiplier = 0.0
    elif risk_multiplier == 0.0:
        # Low score with no fatal stops → RISK_DOWN floor, not BLOCK.
        risk_multiplier = 0.25
    display_status = (
        "BLOCK" if is_hard_block
        else ("RISK_DOWN" if risk_multiplier < 1.0 else "ALLOW")
    )
    primary_reason = block_reasons[0] if block_reasons else (
        "low_score" if risk_multiplier < 1.0 else None
    )
    return {
        "role": "governor",
        "seat": CRYPTO_SEAT_MAP["governor"],
        "holder": holder,
        "risk_multiplier": risk_multiplier,
        "governor_action": "block" if is_hard_block else "modulate",
        "block_reasons": block_reasons,
        "display_status": display_status,    # NEW — UI reads this
        "reason": primary_reason,            # NEW — UI reads this
        "execution_effect": "HARD_BLOCK" if is_hard_block else ("RISK_DOWN_ONLY" if risk_multiplier < 1.0 else "ALLOW"),  # NEW
        "lesson": "Block on wide spread, wrong lane, consecutive losses, or daily loss limit; modulate otherwise.",
        "may_execute": False,
        "may_override_direction": False,
    }


def _build_execution_judge(base, labels, holder, snapshot):
    execution_checks = {
        "has_existing_intent": bool(snapshot.get("existing_intent")),
        "spread_ok": "WIDE_SPREAD" not in labels,
        "liquidity_ok": "EXCHANGE_LIQUIDITY_OK" in labels,
        "quality": base.quality,
    }
    return {
        "role": "execution_judge",
        "seat": CRYPTO_SEAT_MAP["execution_judge"],
        "holder": holder,
        "execution_ready": bool(snapshot.get("existing_intent")) and base.score >= 0.60,
        "execution_checks": execution_checks,
        "lesson": "Only execute after independent direction exists and crypto liquidity + spread + quality are acceptable.",
        "may_execute": False,
        "may_create_direction": False,
        "requires_existing_trade_intent": True,
    }


# ─── pure-math helpers (no DB, no async, no lane crosstalk) ──────────

def _alpha_delta(score: float) -> float:
    if score >= 0.80:
        return 0.08
    if score >= 0.60:
        return 0.03
    if score >= 0.40:
        return -0.03
    return -0.10


def _redeye_objections(labels) -> List[str]:
    objections: List[str] = []
    if "WIDE_SPREAD" in labels:
        objections.append("spread risk may destroy edge")
    if "FUNDING_CROWDED" in labels:
        objections.append("funding suggests crowded positioning")
    if "LIQUIDATION_RISK" in labels:
        objections.append("liquidation imbalance raises fakeout risk")
    if "DEAD_VOL" in labels:
        objections.append("volatility is too weak for clean continuation")
    return objections


def _chevelle_risk_multiplier(score: float) -> float:
    if score >= 0.80:
        return 1.00
    if score >= 0.60:
        return 0.85
    if score >= 0.40:
        return 0.65
    return 0.00


def _chevelle_blocks(labels, snapshot) -> List[str]:
    blocks: List[str] = []
    if "WIDE_SPREAD" in labels:
        blocks.append("BLOCK_WIDE_SPREAD")
    if "WRONG_LANE" in labels:
        blocks.append("BLOCK_WRONG_LANE")
    if int(snapshot.get("consecutive_losses", 0) or 0) >= 3:
        blocks.append("BLOCK_THREE_CONSECUTIVE_LOSSES")
    if float(snapshot.get("daily_pnl_usd", 0.0) or 0.0) <= -100:
        blocks.append("BLOCK_DAILY_LOSS_LIMIT")
    return blocks

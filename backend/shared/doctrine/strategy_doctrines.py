"""Strategy-specific doctrine packets.

Doctrine pin (2026-02-17, source-aligned):
    Different strategies have different doctrinal frames. Same seats,
    different doctrine_version. The Patent J ladder can graduate them
    independently.

    Two strategies modeled from source material:
      • `gap_and_go_v1` — Technical Analysis v3 §Gap-and-Go.
        Entry: cross premarket high or premarket bull-flag high.
        Filters: gap≥20% preferred, float<10M ideal, price-above-EMAs.
        Exits: 5-min red candle / heavy level-2 resistance / 2:1 target.
        Doctrinal mood: "breakout or bailout" — quick decisions.

      • `micro_pullback_v1` — Technical Analysis v3 §Micro Pullback.
        Entry: 1-min pullback near half/whole dollar, on momentum.
        Filters: float<20M, price<$25, strong momentum.
        Exits: low of pullback stop / 2:1 target / first 50¢ target.
        Doctrinal mood: dip-buyer with hard stop.

    Each strategy reuses the same role-keyed seat shape so the
    existing audit, scorecard, auto-retire, and UI work unchanged —
    only the doctrine_version axis splits.

Lane: equity (both strategies are equity day-trading). Crypto stays on
its own twin doctrine and is unaffected.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from shared.doctrine.base_labels import build_doctrine_labels


# ── role → roster seat (equity lane) — same map as base_sidecars ──
EQUITY_SEAT_MAP = {
    "strategist": "decider",
    "adversary": "opponent",
    "governor": "governor",
    "execution_judge": "executor",
}

STRATEGIES = {"gap_and_go", "micro_pullback"}


def build_strategy_packet(
    strategy: str,
    snapshot: Dict[str, Any],
    seat_holders: Optional[Dict[str, str]] = None,
) -> Optional[Dict[str, Any]]:
    """Dispatch to the strategy doctrine. Returns None for unknown
    strategies so the router can fall back to the generic small-account
    doctrine without surprise."""
    strategy_norm = (strategy or "").lower()
    if strategy_norm == "gap_and_go":
        return _build_gap_and_go_v1(snapshot, seat_holders)
    if strategy_norm == "micro_pullback":
        return _build_micro_pullback_v1(snapshot, seat_holders)
    return None


# ─── Gap-and-Go v1 ───────────────────────────────────────────────────

def _build_gap_and_go_v1(snapshot, seat_holders):
    base = build_doctrine_labels(snapshot)
    labels = set(base.labels)
    snap = snapshot or {}
    holders = seat_holders or {}

    # Strategy-specific filters from Tech Analysis v3 §Gap-and-Go.
    premarket_high_crossed = bool(snap.get("premarket_high_crossed"))
    premarket_bull_flag = bool(snap.get("premarket_bull_flag"))
    above_emas = bool(snap.get("price_above_emas"))  # 20/50/200 EMA on daily

    # Conviction delta: built on top of the base score but anchored to
    # the strategy's own ideal conditions.
    cd = 0.0
    if base.quality == "A_QUALITY":
        cd += 0.12
    elif base.quality == "B_QUALITY":
        cd += 0.05
    elif base.quality == "REJECT":
        cd -= 0.20
    if "STRONG_GAPPER" in labels:
        cd += 0.08
    if "ULTRA_LOW_FLOAT" in labels:
        cd += 0.06
    if premarket_high_crossed or premarket_bull_flag:
        cd += 0.05
    if above_emas:
        cd += 0.04

    # Adversary objections — strategy-specific traps.
    objections = []
    if "STRONG_GAPPER" not in labels:
        objections.append("gap_too_small_for_gap_and_go")
    if "HIGH_RELATIVE_VOLUME" not in labels:
        objections.append("rvol_insufficient_for_breakout")
    if not (premarket_high_crossed or premarket_bull_flag):
        objections.append("no_premarket_breakout_setup")
    if not above_emas:
        objections.append("daily_trend_against_strategy")
    if "SPREAD_TOO_WIDE" in labels:
        objections.append("spread_kills_breakout_or_bailout")
    challenge_strength = min(1.0, 0.15 + 0.20 * len(objections))

    # Governor risk — strategy-specific.
    # Doctrine (c, 2026-05-20): Governor = SIZE ONLY. No hard zeros from
    # this packet. Quality, spread, losses become dampeners. RoadGuard
    # kills if structure is unsafe; opponent owns directional veto.
    risk_mult = 1.0
    block_reasons: list[str] = []  # retained for back-compat; doctrine (c) keeps it empty
    if base.quality == "REJECT":
        risk_mult *= 0.20  # was 0.0 (doctrine_reject)
    elif base.quality == "C_QUALITY":
        risk_mult *= 0.30
    elif base.quality == "B_QUALITY":
        risk_mult *= 0.65
    if "SPREAD_TOO_WIDE" in labels:
        risk_mult *= 0.50  # was 0.0 (RoadGuard owns the kill now)
    if not above_emas:
        risk_mult *= 0.50
    if int(snap.get("consecutive_losses", 0) or 0) >= 3:
        risk_mult *= 0.40  # was 0.0
    if float(snap.get("daily_pnl", 0.0) or 0.0) <= -100:
        risk_mult *= 0.25  # was 0.0 (daily_max_loss_reached)
    risk_mult = max(0.0, min(1.0, risk_mult))
    # Floor at 0.10 so the operator can still see the dampened proposal
    # on the ledger AND the trade can proceed at minimum size if every
    # other gate passes.
    if risk_mult > 0 and risk_mult < 0.10:
        risk_mult = 0.10

    # Execution-judge checks — strategy-specific.
    execution_checks = {
        "strong_gapper": "STRONG_GAPPER" in labels,
        "premarket_setup_present": premarket_high_crossed or premarket_bull_flag,
        "above_emas": above_emas,
        "spread_ok": "SPREAD_ACCEPTABLE" in labels,
        "quality_ok": base.quality in {"A_QUALITY", "B_QUALITY"},
    }
    _ej_failed = [k for k, v in execution_checks.items() if not v]

    return _packet(
        doctrine_version="gap_and_go_v1",
        base=base,
        seat_holders=holders,
        strategist={
            "conviction_delta": round(cd, 4),
            "lesson": "Gap-and-Go favors ≥20% gappers with ultra-low float, premarket breakout setup, and price above 20/50/200 EMAs.",
        },
        adversary={
            "challenge_required": bool(objections),
            "challenge_strength": round(challenge_strength, 4),
            "objections": objections,
            "lesson": "Attack weak gaps, missing premarket breakout, broken daily trend, and wide spreads — the breakout-or-bailout mood is unforgiving.",
        },
        governor={
            "risk_multiplier": round(risk_mult, 4),
            "governor_action": "modulate",  # doctrine (c): never "block"
            "block_reasons": block_reasons,  # always empty under (c)
            "lesson": "Governor sizes risk. Doctrine quality, spread, losses become dampeners; RoadGuard owns hard kills.",
        },
        execution_judge={
            "execution_ready": all(execution_checks.values()),
            "execution_checks": execution_checks,
            "failed_checks": _ej_failed,
            "not_ready_reason": (
                None
                if not _ej_failed
                else "; ".join(_ej_failed)
            ),
            "lesson": "Only execute after premarket-high cross or bull-flag break confirms the breakout direction.",
        },
    )


# ─── Micro-Pullback v1 ───────────────────────────────────────────────

def _build_micro_pullback_v1(snapshot, seat_holders):
    base = build_doctrine_labels(snapshot)
    labels = set(base.labels)
    snap = snapshot or {}
    holders = seat_holders or {}

    # Strategy-specific snapshot fields.
    near_half_or_whole = bool(snap.get("near_half_or_whole_dollar"))
    momentum_active = bool(snap.get("momentum_active"))
    no_nearby_resistance = bool(snap.get("no_nearby_resistance"))
    pullback_low_known = bool(snap.get("pullback_low"))  # stop reference

    cd = 0.0
    if base.quality == "A_QUALITY":
        cd += 0.10
    elif base.quality == "B_QUALITY":
        cd += 0.04
    elif base.quality == "REJECT":
        cd -= 0.18
    if "MICRO_PULLBACK_PATTERN" in labels:
        cd += 0.06
    if near_half_or_whole:
        cd += 0.05
    if momentum_active:
        cd += 0.05
    if no_nearby_resistance:
        cd += 0.03

    objections = []
    if "MICRO_PULLBACK_PATTERN" not in labels and "BULL_FLAG_PATTERN" not in labels:
        objections.append("pattern_not_a_pullback")
    if not near_half_or_whole:
        objections.append("entry_not_near_half_or_whole_dollar")
    if not momentum_active:
        objections.append("momentum_not_active")
    if not no_nearby_resistance:
        objections.append("nearby_resistance_kills_50c_target")
    if not pullback_low_known:
        objections.append("pullback_low_unknown_no_stop_reference")
    if "PULLBACK_PATTERN_ON_NON_LEADER" in labels:
        objections.append("pullback_on_non_leading_stock")
    if "SPREAD_TOO_WIDE" in labels:
        objections.append("spread_risk")
    challenge_strength = min(1.0, 0.10 + 0.18 * len(objections))

    # Doctrine (c, 2026-05-20): Governor = SIZE ONLY. Quality and
    # stop-reference issues become dampeners; RoadGuard owns hard
    # kills on unsafe structure.
    risk_mult = 1.0
    block_reasons: list[str] = []
    if base.quality == "REJECT":
        risk_mult *= 0.20  # was 0.0 (doctrine_reject)
    elif base.quality == "C_QUALITY":
        risk_mult *= 0.40
    elif base.quality == "B_QUALITY":
        risk_mult *= 0.70
    if not pullback_low_known:
        risk_mult *= 0.30  # was 0.0 (no_pullback_low_so_no_stop)
    if "SPREAD_TOO_WIDE" in labels:
        risk_mult *= 0.50
    if int(snap.get("consecutive_losses", 0) or 0) >= 3:
        risk_mult *= 0.40  # was 0.0
    if float(snap.get("daily_pnl", 0.0) or 0.0) <= -100:
        risk_mult *= 0.25  # was 0.0
    risk_mult = max(0.0, min(1.0, risk_mult))
    if risk_mult > 0 and risk_mult < 0.10:
        risk_mult = 0.10

    execution_checks = {
        "valid_pullback": "MICRO_PULLBACK_PATTERN" in labels or "BULL_FLAG_PATTERN" in labels,
        "near_half_or_whole": near_half_or_whole,
        "momentum_active": momentum_active,
        "pullback_low_known": pullback_low_known,
        "spread_ok": "SPREAD_ACCEPTABLE" in labels,
    }
    _ej_failed = [k for k, v in execution_checks.items() if not v]

    return _packet(
        doctrine_version="micro_pullback_v1",
        base=base,
        seat_holders=holders,
        strategist={
            "conviction_delta": round(cd, 4),
            "lesson": "Micro Pullback favors a leading momentum stock pulling back near a half/whole dollar with a known low to anchor the stop.",
        },
        adversary={
            "challenge_required": bool(objections),
            "challenge_strength": round(challenge_strength, 4),
            "objections": objections,
            "lesson": "Attack non-leaders disguised as pullbacks, entries far from round-dollar levels, faded momentum, and unknown stop references.",
        },
        governor={
            "risk_multiplier": round(risk_mult, 4),
            "governor_action": "modulate",  # doctrine (c): never "block"
            "block_reasons": block_reasons,  # empty under (c)
            "lesson": "Governor sizes risk. Missing stop reference dampens; RoadGuard owns hard kills.",
        },
        execution_judge={
            "execution_ready": all(execution_checks.values()),
            "execution_checks": execution_checks,
            "failed_checks": _ej_failed,
            "not_ready_reason": (
                None
                if not _ej_failed
                else "; ".join(_ej_failed)
            ),
            "lesson": "Only execute when the leader is pulling back near a round-dollar level with a known low and active momentum.",
        },
    )


# ─── shared packet assembly ─────────────────────────────────────────

def _packet(*, doctrine_version, base, seat_holders, strategist, adversary, governor, execution_judge):
    holders = seat_holders or {}
    return {
        "event_type": "BRAIN_DOCTRINE_SIDECAR_PACKET",
        "doctrine_version": doctrine_version,
        "lane": "equity",
        "symbol": base.symbol,
        "base_labels": {
            "score": base.score,
            "quality": base.quality,
            "labels": base.labels,
            "reasons": base.reasons,
        },
        "seats": {
            "strategist": {
                "role": "strategist",
                "seat": EQUITY_SEAT_MAP["strategist"],
                "holder": holders.get(EQUITY_SEAT_MAP["strategist"]),
                "may_execute": False,
                "may_override_direction": False,
                **strategist,
            },
            "adversary": {
                "role": "adversary",
                "seat": EQUITY_SEAT_MAP["adversary"],
                "holder": holders.get(EQUITY_SEAT_MAP["adversary"]),
                "may_execute": False,
                "may_override_direction": False,
                **adversary,
            },
            "governor": {
                "role": "governor",
                "seat": EQUITY_SEAT_MAP["governor"],
                "holder": holders.get(EQUITY_SEAT_MAP["governor"]),
                "may_execute": False,
                "may_override_direction": False,
                **governor,
            },
            "execution_judge": {
                "role": "execution_judge",
                "seat": EQUITY_SEAT_MAP["execution_judge"],
                "holder": holders.get(EQUITY_SEAT_MAP["execution_judge"]),
                "may_execute": False,
                "may_create_direction": False,
                "requires_existing_trade_intent": True,
                **execution_judge,
            },
        },
    }

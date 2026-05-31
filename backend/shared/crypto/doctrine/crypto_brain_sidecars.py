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

    Doctrine (c, 2026-05-20): Chevelle/Governor = SIZE ONLY.
    No hard blocks emitted from this sidecar. Wide spread, low
    volume, consecutive losses, etc. all become risk dampeners.
    The smallest (most cautious) dampener combines with the
    score-based base multiplier. RoadGuard owns deterministic
    safety; opponent seat owns directional veto.
    """
    base_mult = _chevelle_risk_multiplier(base.score)
    dampeners = _chevelle_dampeners(labels, snapshot)

    # Combine: base score mult × strongest applicable dampener (lowest).
    # Informational-only dampeners (e.g. WRONG_LANE → 0.0) are excluded
    # from the multiplication so the sidecar never zeroes by itself.
    applied = [d for (_name, d) in dampeners if d > 0.0]
    dampener_mult = min(applied) if applied else 1.0
    risk_multiplier = base_mult * dampener_mult

    # Score-zero with no fatal blocks → never zero out the sidecar.
    # Floor at 0.25 so the operator can see Chevelle's dissent in
    # the ledger AND the trade can still proceed at minimum size if
    # every other gate passes.
    if risk_multiplier == 0.0 and not labels.intersection({"WRONG_LANE"}):
        risk_multiplier = 0.25

    dampener_names = [n for (n, _d) in dampeners]
    display_status = (
        "RISK_DOWN" if risk_multiplier < 1.0 else "ALLOW"
    )
    primary_reason = dampener_names[0] if dampener_names else (
        "low_score" if risk_multiplier < 1.0 else None
    )
    return {
        "role": "governor",
        "seat": CRYPTO_SEAT_MAP["governor"],
        "holder": holder,
        "risk_multiplier": risk_multiplier,
        "governor_action": "modulate",  # never "block" under doctrine (c)
        "block_reasons": [],            # retained for back-compat; empty under (c)
        "dampeners": dampeners,         # NEW — (name, mult) pairs
        "display_status": display_status,
        "reason": primary_reason,
        "execution_effect": "RISK_DOWN_ONLY" if risk_multiplier < 1.0 else "ALLOW",
        "lesson": "Governor sizes risk. Hard veto lives at the opponent seat; safety lives at RoadGuard.",
        "may_execute": False,
        "may_override_direction": False,
    }


def _build_setup_quality_summary(base, labels, holder, snapshot):
    """Crypto setup-quality summary — ADVISORY ONLY (2026-05-31).

    Previously named `execution_judge`; renamed to make clear this is
    a Patent J quality summary, not authority. It does NOT gate trades:
    no MC gate, no auto-router decision, and no broker call reads this.
    It is rendered as a small badge under the DOCTRINE strip so the
    operator can see why a setup was scored REJECT without drilling
    into raw labels.

    The four real seats (strategist · governor · auditor · executor)
    remain unchanged. This is doctrine output, not a seat.
    """
    execution_checks = {
        "has_existing_intent": bool(snapshot.get("existing_intent")),
        "spread_ok": "WIDE_SPREAD" not in labels,
        "liquidity_ok": "EXCHANGE_LIQUIDITY_OK" in labels,
        "quality_ok": base.quality in {"A_QUALITY", "B_QUALITY"},
        "score_ok": base.score >= 0.60,
    }
    summary_ok = (
        bool(snapshot.get("existing_intent")) and base.score >= 0.60
    )
    failed_checks = [k for k, v in execution_checks.items() if not v]
    return {
        "role": "setup_quality_summary",
        "advisory_only": True,
        "blocks_execution": False,
        # Legacy seat field kept so the doctrine packet schema stays
        # backward-compatible with historical audit rows for the
        # scorecard's correlation joins. Operator should treat
        # `role: "setup_quality_summary"` as authoritative going
        # forward; the `seat` field is no longer a real seat.
        "seat": CRYPTO_SEAT_MAP["execution_judge"],
        "holder": holder,
        "summary_ok": summary_ok,
        # Deprecated alias retained for the scorecard's outcome-join
        # over historical intent rows. Not used by any gate or router.
        "execution_ready": summary_ok,
        "execution_checks": execution_checks,
        "failed_checks": failed_checks,
        "not_ready_reason": (
            None
            if summary_ok
            else "; ".join(failed_checks) or "no_failing_check_recorded"
        ),
        "lesson": "Setup-quality summary. Doctrine labels and Patent J score together. ADVISORY ONLY — does not gate execution.",
        # Authority invariants (preserved from the legacy role). The
        # demoted role STILL cannot execute or create direction — these
        # pins are doctrine, not implementation detail. Keep them.
        "may_execute": False,
        "may_create_direction": False,
        "requires_existing_trade_intent": True,
    }


# Backward-compat alias — internal callers in `lane_doctrine_router`
# still expect the old symbol name. Removing it would require a
# coordinated edit across all callers (and the test fixtures). The
# alias is one line, the cost of removal is not worth it today.
_build_execution_judge = _build_setup_quality_summary


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


# Doctrine (c, 2026-05-20): GOVERNOR = SIZE ONLY.
# Chevelle's role is graduated risk modulation, not directional veto.
# Wide spread / low volume / quality dampeners drop size; they do not
# kill. Truly unsafe market structure is the job of RoadGuard, and
# directional contradiction is the job of the OPPONENT seat.
GOVERNOR_DAMPENERS: dict[str, float] = {
    "WIDE_SPREAD": 0.50,        # was BLOCK_WIDE_SPREAD
    "LOW_VOLUME": 0.60,
    "LOW_QUALITY": 0.70,
    "UNCERTAIN": 0.75,
    "THREE_CONSECUTIVE_LOSSES": 0.50,
    "DAILY_LOSS_LIMIT": 0.25,   # severe damp but not zero — RoadGuard kills if truly unsafe
}


def _chevelle_risk_multiplier(score: float) -> float:
    if score >= 0.80:
        return 1.00
    if score >= 0.60:
        return 0.85
    if score >= 0.40:
        return 0.65
    return 0.00


def _chevelle_dampeners(labels, snapshot) -> List[tuple[str, float]]:
    """Doctrine (c): governor returns SIZE MULTIPLIERS, not blocks.

    Each non-fatal condition contributes its dampener. The strongest
    (smallest) dampener wins. RoadGuard still kills on hard
    unsafety; this function never returns a block.
    """
    out: List[tuple[str, float]] = []
    if "WIDE_SPREAD" in labels:
        out.append(("WIDE_SPREAD", GOVERNOR_DAMPENERS["WIDE_SPREAD"]))
    if "WRONG_LANE" in labels:
        # Wrong-lane is an AUTHORITY error owned by MC's executor_seat_check;
        # governor sidecar surfaces it as a dampener so the operator sees
        # the diagnostic but it never reaches gating.
        out.append(("WRONG_LANE", 0.0))  # informational; gating ignores
    if int(snapshot.get("consecutive_losses", 0) or 0) >= 3:
        out.append(("THREE_CONSECUTIVE_LOSSES", GOVERNOR_DAMPENERS["THREE_CONSECUTIVE_LOSSES"]))
    if float(snapshot.get("daily_pnl_usd", 0.0) or 0.0) <= -100:
        out.append(("DAILY_LOSS_LIMIT", GOVERNOR_DAMPENERS["DAILY_LOSS_LIMIT"]))
    return out


def _chevelle_blocks(labels, snapshot) -> List[str]:
    """Doctrine (c, 2026-05-20): Chevelle no longer emits hard blocks.

    Retained for backward-compatibility with any UI that still
    reads `block_reasons`; always returns an empty list under the
    new doctrine. See `_chevelle_dampeners` for the live behavior.

    Hard-veto authority moved to the OPPONENT seat
    (`HARD_VETO_OPPONENT`) and to RoadGuard (deterministic market-
    structure caps).
    """
    return []

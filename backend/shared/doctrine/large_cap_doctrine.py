"""Large-cap equity doctrine packet.

Doctrine pin (2026-02-18, source-aligned):
    The small-account / gap-and-go / micro-pullback doctrines are
    designed for sub-$25k accounts trading $1-$20 stocks with ≥10%
    gaps, ≥5x relative volume, and float ≤20M. Applying them to
    AMZN / GOOGL / NVDA / AAPL is a category error — those names
    will ALWAYS score REJECT under that rubric, which polluted the
    advisory chips and made Camaro's snapshots look like uniform
    rejects.

    `large_cap_equity_v1` is a separate doctrine version for
    mega-cap day trading. Same role-keyed seat shape as the other
    equity doctrines — strategist / adversary / governor /
    execution_judge — but with thresholds appropriate to liquid
    large-caps.

    Triggers (any of):
      • snapshot.strategy == "large_cap"
      • snapshot.market_cap_band in ("large", "mega")
      • Router dispatch via `lane_doctrine_router.py`

    Restrictions still on the SEAT, not the brain. `may_execute=False`
    everywhere. Advisory only. Patent J grades this slice
    independently from small-account doctrines.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional


DOCTRINE_VERSION = "large_cap_equity_v1"

# Canonical 8-seat IP (2026-05-27 doctrine refresh). `decider` →
# `strategist`, `opponent` → `auditor`. Labels reflect canonical
# seats so the doctrine packet matches what the live roster stores.
# Critical: `fetch_seat_holders()` returns holders keyed by canonical
# seat names — these MUST match or every holder lookup returns None
# and the UI shows "holder: vacant" even when seats are filled
# (2026-06-18 Prod bug fix).
EQUITY_SEAT_MAP = {
    "strategist": "strategist",
    "adversary": "auditor",
    "governor": "governor",
    "execution_judge": "executor",
}


# Quality thresholds tuned for mega-cap liquidity. Used internally to
# label the snapshot; downstream consumers read `quality` as if it were
# a small-account label so the existing scoring axes work unchanged.
@dataclass
class _LargeCapLabels:
    symbol: str
    score: float
    quality: str
    labels: List[str]
    reasons: List[str]


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _build_large_cap_labels(snapshot: Dict[str, Any]) -> _LargeCapLabels:
    """Quality labeler tuned for large-caps. Differences vs
    `base_labels.py`:
      * Price band irrelevant (large-caps live above $20).
      * Gap threshold relaxed to ≥1% (mega-caps rarely gap ≥10%).
      * RVOL threshold relaxed to ≥1.5× (more liquidity, less spike).
      * Float threshold removed (large-caps have huge floats by def).
      * News catalyst still counts but absence is not punished.
      * Spread: still labeled, but the cap is bps (already tight for
        mega-caps), so SPREAD_TOO_WIDE rarely fires.

    2026-02-19 momentum-origination enhancement (operator directive):
    Large-caps rarely gap ≥1% and rarely see 3x RVOL. The doctrine
    used to plateau at C_QUALITY for the whole watchlist because the
    only positive signals were flags no mega-cap actually hits during
    RTH. New scoring signals — all sourced from the equity enricher,
    all optional (missing = silent, never penalizing):

      * `vwap_distance_pct` — price vs VWAP (institutional midline).
        Positive = above VWAP → bullish tilt; negative = below →
        bearish tilt. Small band around zero is neutral.
      * `velocity_1m` / `velocity_5m` — momentum acceleration. High
        positive velocity = origination; sustained negative =
        breakdown. Parabolic-phase penalties still apply on top.
      * `rvol_acceleration` — RVOL delta over the last few minutes.
        Volume expanding INTO the move is the mega-cap origination
        signature. Contracting on the move = fade risk.
      * `price_above_emas` — daily 20/50/200 EMA stack. When True
        AND regime not weak, the tape has structural bull tilt.
    """
    symbol = str(snapshot.get("symbol", "UNKNOWN"))
    gap_pct = float(snapshot.get("gap_pct", 0.0))
    rvol = float(snapshot.get("relative_volume", 0.0))
    has_news = bool(snapshot.get("has_news", False))
    market_regime = str(snapshot.get("market_regime", "unknown")).lower()
    spread_bps = float(snapshot.get("spread_bps", 999.0))
    fractional_supported = bool(snapshot.get("fractional_supported", False))
    # New momentum-origination signals (all optional).
    vwap_distance_pct = snapshot.get("vwap_distance_pct")
    velocity_1m = snapshot.get("velocity_1m")
    velocity_5m = snapshot.get("velocity_5m")
    rvol_acceleration = snapshot.get("rvol_acceleration")
    price_above_emas = snapshot.get("price_above_emas")

    # 2026-02-20: baseline raised 0.30 → 0.40 per operator directive.
    # A large-cap on a sleepy day now clears C_QUALITY (≥0.40) on
    # liquidity-alone, instead of REJECTing at exactly the baseline.
    # The brain still emits with LOW conviction (strategist conviction
    # delta below scales to score); the BASELINE_ONLY_TOEHOLD rule
    # downstream forces toehold-size sizing when only the baseline
    # fired, so "no real signal" days don't trade at normal size.
    score = 0.40
    labels: List[str] = ["LARGE_CAP_LIQUID"]
    reasons: List[str] = []

    # ── fractional-trading capability (2026-02-20) ──
    # Doctrine pin (operator, 2026-02-20):
    #     "Fractional does not make the signal better.
    #      Fractional makes the risk smaller."
    # We give a SMALL (+0.05) doctrine credit when fractional is
    # supported because it means the broker can fill ANY notional
    # at this price — i.e., the per-order budget is not bounded by
    # the share price. The real benefit lands at the SEAT layer
    # (see `shared/broker/fractional_sizing.py`), which converts
    # notional → fractional quantity. This label is a sizing
    # unlock, not a conviction unlock — paired with the score
    # bump it tilts the lane from "needs perfect setup" to
    # "tradable at this account size", but it does NOT lift the
    # B_QUALITY (0.60) / A_QUALITY (0.80) thresholds.
    if fractional_supported:
        score += 0.05
        labels.append("FRACTIONAL_SUPPORTED")

    # ── gap (relaxed) ──
    if gap_pct >= 1.0:
        score += 0.15
        labels.append("GAPPER_LARGE_CAP")
        if gap_pct >= 3.0:
            score += 0.10
            labels.append("STRONG_GAPPER_LARGE_CAP")
    else:
        reasons.append("gap_below_1_pct")

    # ── relative volume (relaxed) ──
    if rvol >= 1.5:
        score += 0.15
        labels.append("ELEVATED_RELATIVE_VOLUME")
        if rvol >= 3.0:
            score += 0.05
            labels.append("HIGH_RELATIVE_VOLUME")
    else:
        reasons.append("relative_volume_below_1_5x")

    # ── news catalyst (bonus only) ──
    if has_news:
        score += 0.10
        labels.append("NEWS_CATALYST")
    # Absence is silent — large-caps move on flow, not news, every day

    # ── VWAP tilt (institutional midline) ──
    # Doctrine pin (2026-02-19): VWAP is the mega-cap operator's
    # tell for institutional participation. Above VWAP on a
    # green-light regime = origination-friendly; below VWAP = fade
    # / mean-reversion tape. Small band (<0.10%) around zero is
    # neutral. Missing = silent (do NOT default to 0 — that would
    # register as "at VWAP" which is a real signal).
    if isinstance(vwap_distance_pct, (int, float)):
        vd = float(vwap_distance_pct)
        if vd >= 0.5:
            score += 0.10
            labels.append("VWAP_BULL_TILT")
            if vd >= 1.5:
                score += 0.05
                labels.append("VWAP_STRONG_BULL_TILT")
        elif vd <= -0.5:
            score += 0.05  # still a signal — direction is DOWN
            labels.append("VWAP_BEAR_TILT")
            if vd <= -1.5:
                score += 0.03
                labels.append("VWAP_STRONG_BEAR_TILT")

    # ── velocity / momentum origination ──
    # Doctrine pin (2026-02-19): Sustained 5m velocity is the
    # mega-cap continuation signal. Absolute value counts because
    # the brain emits directional (BUY on +vel, SELL on -vel);
    # the score-boost here just says "there IS a directional
    # move" — the strategist seat converts velocity sign to
    # BUY/SELL conviction.
    if isinstance(velocity_5m, (int, float)):
        v = abs(float(velocity_5m))
        if v >= 0.5:
            score += 0.05
            labels.append("MOMENTUM_5M_ACTIVE")
            if v >= 1.5:
                score += 0.05
                labels.append("MOMENTUM_5M_STRONG")
    if isinstance(velocity_1m, (int, float)):
        v = abs(float(velocity_1m))
        if v >= 0.25:
            labels.append("MOMENTUM_1M_ACTIVE")  # informational only

    # ── RVOL acceleration (volume expanding into the move) ──
    # Doctrine pin (2026-02-19): Volume expanding INTO price motion
    # is the origination fingerprint. Contracting on price motion
    # = late move / fade risk. `rvol_acceleration` is a positive
    # number when RVOL is accelerating over the last N bars.
    if isinstance(rvol_acceleration, (int, float)):
        ra = float(rvol_acceleration)
        if ra >= 0.20:
            score += 0.05
            labels.append("RVOL_ACCELERATING")
            if ra >= 0.50:
                score += 0.03
                labels.append("RVOL_STRONG_ACCELERATION")

    # ── EMA stack alignment (structural bull tilt) ──
    # Doctrine pin (2026-02-19): 20/50/200 daily EMA stack in order
    # means the mega-cap is in a structural uptrend. This is a
    # dampener on downside risk more than a booster on upside
    # (large-caps in an EMA stack rarely fade hard intraday), so
    # the score bump is modest.
    if price_above_emas is True:
        score += 0.05
        labels.append("EMA_STACK_ALIGNED")
    elif price_above_emas is False:
        # Explicit False — the enricher tested and the stack is
        # broken. Small penalty to reflect trend risk.
        score -= 0.03
        labels.append("EMA_STACK_BROKEN")

    # ── regime ──
    if market_regime in {"strong", "green_light", "momentum"}:
        score += 0.10
        labels.append("MARKET_GREEN_LIGHT")
    elif market_regime in {"weak", "slow", "choppy"}:
        score -= 0.10
        labels.append("MARKET_WEAK_REDUCE_RISK")
        reasons.append("weak_market_regime")

    # ── spread ──
    # Spread quality guard (2026-07-03, operator directive):
    # Stale/sentinel quotes must not force SPREAD_TOO_WIDE. Same
    # rationale as base_labels.py — a stale after-hours quote is
    # "no fresh quote", not a real wide spread. Live quality goes
    # through the normal tight/acceptable/wide ladder.
    spread_quality = str(snapshot.get("spread_quality", "live")).lower()
    if spread_quality in ("stale", "sentinel"):
        labels.append("SPREAD_QUALITY_UNKNOWN")
        reasons.append("stale_or_sentinel_quote_no_penalty")
    elif spread_bps <= 10.0:
        labels.append("SPREAD_TIGHT")
    elif spread_bps <= 25.0:
        labels.append("SPREAD_ACCEPTABLE")
    else:
        score -= 0.10
        labels.append("SPREAD_TOO_WIDE")
        reasons.append("spread_too_wide")

    score = _clamp(score)

    # ── BASELINE_ONLY_TOEHOLD detection (2026-02-20) ──
    # Fires when NONE of the quality-positive labels triggered — i.e.,
    # the only labels are LARGE_CAP_LIQUID and (optionally)
    # FRACTIONAL_SUPPORTED. Neutral noise like SPREAD_ACCEPTABLE
    # doesn't disqualify (it's a "nothing wrong" tag, not a signal),
    # but any of {GAPPER, RVOL, NEWS, GREEN_LIGHT, VWAP, MOMENTUM,
    # RVOL_ACCEL, EMA_STACK} firing means the brain has a real lean
    # and shouldn't be toehold-clamped.
    quality_positive_labels = {
        "GAPPER_LARGE_CAP", "STRONG_GAPPER_LARGE_CAP",
        "ELEVATED_RELATIVE_VOLUME", "HIGH_RELATIVE_VOLUME",
        "NEWS_CATALYST", "MARKET_GREEN_LIGHT",
        # 2026-02-19 momentum-origination signals
        "VWAP_BULL_TILT", "VWAP_STRONG_BULL_TILT",
        "VWAP_BEAR_TILT", "VWAP_STRONG_BEAR_TILT",
        "MOMENTUM_5M_ACTIVE", "MOMENTUM_5M_STRONG",
        "RVOL_ACCELERATING", "RVOL_STRONG_ACCELERATION",
        "EMA_STACK_ALIGNED",
    }
    if not (set(labels) & quality_positive_labels):
        labels.append("BASELINE_ONLY_TOEHOLD")
        reasons.append("baseline_only_signal: toehold-size only")

    if score >= 0.80:
        quality = "A_QUALITY"
    elif score >= 0.60:
        quality = "B_QUALITY"
    elif score >= 0.40:
        quality = "C_QUALITY"
    else:
        quality = "REJECT"

    return _LargeCapLabels(
        symbol=symbol, score=round(score, 4),
        quality=quality, labels=labels, reasons=reasons,
    )


def _direction_bias(snapshot: Dict[str, Any], labels_set: set) -> Dict[str, Any]:
    """Compute a directional bias hint from the momentum signals.

    Doctrine pin (2026-02-19): Large-cap brains historically emitted
    HOLD indefinitely because the doctrine only scored SETUP QUALITY
    but never a directional sign. This hint is the missing signal —
    a `strategy_bias ∈ {BUY, SELL, NEUTRAL}` plus a `bias_strength`
    (0-1) built from velocity sign, VWAP tilt, and EMA stack. Brains
    read this off the doctrine packet to pick a direction; the seat
    layer still owns final action + sizing.
    """
    votes = 0.0
    total_weight = 0.0

    v5 = snapshot.get("velocity_5m")
    if isinstance(v5, (int, float)):
        v = float(v5)
        # Strong 5m velocity is worth 2 units; light is 1.
        w = 2.0 if abs(v) >= 1.5 else (1.0 if abs(v) >= 0.5 else 0.0)
        if w > 0.0:
            votes += w if v > 0 else -w
            total_weight += w

    vd = snapshot.get("vwap_distance_pct")
    if isinstance(vd, (int, float)):
        d = float(vd)
        w = 1.5 if abs(d) >= 1.5 else (1.0 if abs(d) >= 0.5 else 0.0)
        if w > 0.0:
            votes += w if d > 0 else -w
            total_weight += w

    if "EMA_STACK_ALIGNED" in labels_set:
        votes += 0.5
        total_weight += 0.5
    elif "EMA_STACK_BROKEN" in labels_set:
        votes -= 0.5
        total_weight += 0.5

    # Parabolic-phase overrides — topping/fade skew short, accumulation long.
    phase = str(snapshot.get("parabolic_phase") or "").lower()
    if phase == "topping" or phase == "fade":
        votes -= 1.0
        total_weight += 1.0
    elif phase == "accumulation":
        votes += 0.5
        total_weight += 0.5

    if total_weight <= 0.0:
        return {
            "strategy_bias": "NEUTRAL",
            "bias_strength": 0.0,
            "bias_reasons": ["no_directional_signals"],
        }

    strength = abs(votes) / total_weight
    if strength < 0.20:
        return {
            "strategy_bias": "NEUTRAL",
            "bias_strength": round(strength, 4),
            "bias_reasons": ["signals_conflict_below_threshold"],
        }
    return {
        "strategy_bias": "BUY" if votes > 0 else "SELL",
        "bias_strength": round(strength, 4),
        "bias_reasons": [],
    }


def build_large_cap_doctrine_packet(
    snapshot: Dict[str, Any],
    seat_holders: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Compose the role-keyed packet (parity with
    `brain_sidecars.build_all_brain_doctrine_packets`)."""
    base = _build_large_cap_labels(snapshot)
    labels = set(base.labels)
    holders = seat_holders or {}

    strategist = _build_strategist(base, labels, holders.get(EQUITY_SEAT_MAP["strategist"]))
    adversary = _build_adversary(base, labels, holders.get(EQUITY_SEAT_MAP["adversary"]))
    governor = _build_governor(base, labels, holders.get(EQUITY_SEAT_MAP["governor"]), snapshot)
    execution_judge = _build_execution_judge(
        base, labels, holders.get(EQUITY_SEAT_MAP["execution_judge"]),
    )
    direction = _direction_bias(snapshot, labels)

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
        "direction": direction,
        "seats": {
            "strategist": strategist,
            "adversary": adversary,
            "governor": governor,
            "execution_judge": execution_judge,
        },
    }


def _build_strategist(base, labels, holder):
    cd = 0.0
    if base.quality == "A_QUALITY":
        cd += 0.10
    elif base.quality == "B_QUALITY":
        cd += 0.06
    elif base.quality == "C_QUALITY":
        cd += 0.02
    elif base.quality == "REJECT":
        cd -= 0.12  # softer than small-account's -0.20
    if "STRONG_GAPPER_LARGE_CAP" in labels:
        cd += 0.04
    if "HIGH_RELATIVE_VOLUME" in labels:
        cd += 0.04
    if "NEWS_CATALYST" in labels:
        cd += 0.03
    # 2026-02-19 momentum-origination: reward directional signals.
    # The strategist seat converts these into BUY / SELL conviction
    # downstream; the sign lives on the intent (via strategy_bias),
    # not here — this seat only says "we have conviction, and how
    # much" (higher = more).
    if "MOMENTUM_5M_STRONG" in labels:
        cd += 0.06
    elif "MOMENTUM_5M_ACTIVE" in labels:
        cd += 0.03
    if "VWAP_STRONG_BULL_TILT" in labels or "VWAP_STRONG_BEAR_TILT" in labels:
        cd += 0.04
    elif "VWAP_BULL_TILT" in labels or "VWAP_BEAR_TILT" in labels:
        cd += 0.02
    if "RVOL_STRONG_ACCELERATION" in labels:
        cd += 0.03
    elif "RVOL_ACCELERATING" in labels:
        cd += 0.02
    if "EMA_STACK_ALIGNED" in labels:
        cd += 0.02
    return {
        "role": "strategist",
        "seat": EQUITY_SEAT_MAP["strategist"],
        "holder": holder,
        "conviction_delta": round(cd, 4),
        "lesson": (
            "Large-cap day trades reward VWAP tilt, sustained 5m "
            "velocity, and RVOL expanding INTO the move. News is a "
            "tailwind, not a requirement."
        ),
        "may_execute": False,
        "may_override_direction": False,
    }


def _build_adversary(base, labels, holder):
    objections: List[str] = []
    if "ELEVATED_RELATIVE_VOLUME" not in labels:
        objections.append("rvol_too_quiet_for_directional")
    if "SPREAD_TOO_WIDE" in labels:
        objections.append("spread_risk")
    if "MARKET_WEAK_REDUCE_RISK" in labels:
        objections.append("weak_market_regime")
    if base.quality == "REJECT":
        objections.append("setup_quality_insufficient")
    challenge_strength = min(1.0, 0.20 + 0.18 * len(objections))
    return {
        "role": "adversary",
        "seat": EQUITY_SEAT_MAP["adversary"],
        "holder": holder,
        "challenge_required": bool(objections),
        "challenge_strength": round(challenge_strength, 4),
        "objections": objections,
        "lesson": (
            "Attack large-cap trades that lack volume, fight a weak "
            "tape, or sit on a wide spread."
        ),
        "may_execute": False,
        "may_override_direction": False,
    }


def _build_governor(base, labels, holder, snapshot):
    """Doctrine (c): SIZE ONLY. Never hard-blocks. RoadGuard owns hard
    kills on unsafe market structure."""
    risk_multiplier = 1.0
    block_reasons: List[str] = []
    consecutive_losses = int(snapshot.get("consecutive_losses", 0) or 0)
    daily_pnl = float(snapshot.get("daily_pnl", 0.0) or 0.0)

    if base.quality == "A_QUALITY":
        risk_multiplier *= 1.00
    elif base.quality == "B_QUALITY":
        risk_multiplier *= 0.85
    elif base.quality == "C_QUALITY":
        risk_multiplier *= 0.60
    else:  # REJECT — dampen, don't kill
        risk_multiplier *= 0.30
        block_reasons.append("large_cap_doctrine_reject")

    if "MARKET_WEAK_REDUCE_RISK" in labels:
        risk_multiplier *= 0.60
    if "SPREAD_TOO_WIDE" in labels:
        risk_multiplier *= 0.50
    if consecutive_losses >= 3:
        risk_multiplier *= 0.40
    if daily_pnl <= -100:
        risk_multiplier *= 0.25

    risk_multiplier = max(0.0, min(1.0, risk_multiplier))
    if 0.0 < risk_multiplier < 0.10:
        risk_multiplier = 0.10

    # ── BASELINE_ONLY_TOEHOLD clamp (2026-02-20) ──
    # Doctrine pin: "Fractional makes the risk smaller, not the
    # signal better." When the label set says "only the baseline
    # fired" (no gap / no rvol / no news / no green tape), the brain
    # is allowed to BUY but only at TOEHOLD size. We clamp the
    # governor's risk_multiplier DOWN to TOEHOLD_RISK_MULTIPLIER
    # (0.20× the brain's normal sizing). The brain's confidence
    # number is unchanged — the seat / governor owns sizing.
    if "BASELINE_ONLY_TOEHOLD" in labels:
        risk_multiplier = min(risk_multiplier, 0.20)

    display_status = (
        "RISK_DOWN" if (block_reasons or risk_multiplier < 1.0) else "ALLOW"
    )
    primary_reason = block_reasons[0] if block_reasons else None
    return {
        "role": "governor",
        "seat": EQUITY_SEAT_MAP["governor"],
        "holder": holder,
        "risk_multiplier": round(risk_multiplier, 4),
        "governor_action": "modulate",  # doctrine (c): never block
        "block_reasons": block_reasons,
        "display_status": display_status,
        "reason": primary_reason,
        "execution_effect": (
            "RISK_DOWN_ONLY" if (block_reasons or risk_multiplier < 1.0)
            else "ALLOW"
        ),
        "lesson": (
            "Governor sizes risk. Quality, regime, spread, and losses "
            "become dampeners; RoadGuard owns hard kills."
        ),
        "may_execute": False,
        "may_override_direction": False,
    }


def _build_execution_judge(base, labels, holder):
    checks = {
        "quality_ok": base.quality in {"A_QUALITY", "B_QUALITY", "C_QUALITY"},
        "spread_ok": ("SPREAD_ACCEPTABLE" in labels
                      or "SPREAD_TIGHT" in labels),
        "market_not_weak": "MARKET_WEAK_REDUCE_RISK" not in labels,
        "has_volume": ("ELEVATED_RELATIVE_VOLUME" in labels
                       or "HIGH_RELATIVE_VOLUME" in labels),
    }
    return {
        "role": "execution_judge",
        "seat": EQUITY_SEAT_MAP["execution_judge"],
        "holder": holder,
        "execution_ready": all(checks.values()),
        "execution_checks": checks,
        "lesson": (
            "Only execute large-caps when liquidity, spread, regime, "
            "and the existing directional intent all align."
        ),
        "may_execute": False,
        "may_create_direction": False,
        "requires_existing_trade_intent": True,
    }


# ─── Operator reference cards (CI-enforced sync) ─────────────────────
# Anti-drift: `snapshot_fields_read` and `risk_flags_read` must be
# strings that actually appear in the function source. See
# tests/test_doctrine_integrity.py.

DOCTRINE_CARDS: Dict[str, Dict[str, Any]] = {
    "large_cap_equity": {
        "title": "Large-Cap Equity v1",
        "category": "Large-Cap Swing/Day Trade",
        "lane": "equity",
        "tagline": "Institutional footprints — liquidity, RVOL, and regime do the talking.",
        "source_attribution": "Sector Rotation v2 / Large-Cap doctrine pin 2026-02-18",
        "doctrine_version": "large_cap_equity_v1",
        "ideal_conditions": [
            "stock in large- or mega-cap band",
            "elevated relative volume (>= 1.5x)",
            "tape not in MARKET_WEAK_REDUCE_RISK regime",
            "spread tight (<= 25 bps)",
        ],
        "entries": [
            "Existing directional intent + ELEVATED_RELATIVE_VOLUME",
            "News-backed continuation with regime green light",
        ],
        "exits": [
            "Regime flip to MARKET_WEAK_REDUCE_RISK",
            "Spread widening into SPREAD_TOO_WIDE",
            "Earnings within 48h (mandatory trim)",
        ],
        "size_modifier_notes": [
            "1.00x risk on A_QUALITY",
            "0.85x on B_QUALITY, 0.60x on C_QUALITY",
            "0.30x dampen on REJECT (never zero — RoadGuard owns kill)",
            "0.40x on consecutive_losses >= 3; 0.25x on daily_pnl <= -100",
        ],
        "snapshot_fields_read": [
            "symbol",
            "gap_pct",
            "relative_volume",
            "has_news",
            "market_regime",
            "spread_bps",
        ],
        "risk_flags_read": [
            "MARKET_WEAK_REDUCE_RISK",
            "SPREAD_TOO_WIDE",
            "ELEVATED_RELATIVE_VOLUME",
            "HIGH_RELATIVE_VOLUME",
            "NEWS_CATALYST",
        ],
    },
}

_DOCTRINE_FN_MAP: Dict[str, str] = {
    "large_cap_equity": "_build_large_cap_labels",
}

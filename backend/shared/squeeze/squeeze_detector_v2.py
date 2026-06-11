"""Squeeze Detector V2 — hardened production-safe version.

Shipped by operator 2026-06-11.

Returns a SqueezeResult with:
  * `squeeze_score` (0-100, post-risk-penalty)
  * `raw_score` (0-100, pre-risk-penalty)
  * `confidence` (0-1, based on data completeness + freshness)
  * `grade` (A/B/C/D/F)
  * `action_bias` (SQUEEZE_CANDIDATE / WATCH_FOR_BREAKOUT / etc.)
  * `reasons[]` — what fired positively
  * `risk_flags[]` — what reduced the score
  * `metrics{}` — gap_pct, rel_volume, float_rotation, velocity, etc.

Hardening over v1:
  * Bad/missing hard-data fields → grade F + DATA_ERROR (not a score of 0
    masquerading as a real assessment).
  * Stale data (>5s) → grade F + WAIT_FOR_FRESH_DATA.
  * Risk flags now subtract actual penalty points from the final score
    rather than being descriptive-only.
  * Confidence reflects how many soft fields were present AND whether
    the data was stale/incomplete.

Brain wiring:
  Barracuda (camaro) + GTO (redeye) read the attached `squeeze` block
  from intent.evidence and modulate confidence/size_bias accordingly.
"""
from dataclasses import dataclass
from typing import Optional, Dict, Any, List
import time


@dataclass
class SqueezeInput:
    symbol: str
    price: float
    prev_close: float
    day_high: float
    premarket_high: Optional[float]
    volume_today: float
    avg_volume_20d: float
    float_shares: Optional[float]

    timestamp: Optional[float] = None
    data_freshness_ms: Optional[float] = None

    short_interest_pct: Optional[float] = None
    borrow_rate_pct: Optional[float] = None
    borrow_rate_change_pct: Optional[float] = None
    shares_available_to_short: Optional[float] = None

    spread_bps: Optional[float] = None
    news_catalyst: bool = False

    price_30s_ago: Optional[float] = None
    volume_last_1m: Optional[float] = None
    avg_volume_last_5m: Optional[float] = None


@dataclass
class SqueezeResult:
    symbol: str
    squeeze_score: float
    raw_score: float
    confidence: float
    grade: str
    action_bias: str
    reasons: List[str]
    risk_flags: List[str]
    metrics: Dict[str, Any]


class SqueezeDetectorV2:
    RISK_PENALTIES = {
        "wide_spread_risk": 20,
        "already_fading_from_high": 25,
        "stale_data_risk": 30,
        "sub_five_dollar_regime": 10,
        "blowoff_velocity_risk": 15,
        "late_stage_float_rotation_risk": 10,
        "data_incomplete_risk": 10,
    }

    HARD_DATA_FIELDS = [
        "price",
        "prev_close",
        "day_high",
        "volume_today",
        "avg_volume_20d",
    ]

    def analyze(self, x: SqueezeInput) -> SqueezeResult:
        data_error = self._validate_hard_fields(x)
        if data_error:
            return self._data_error(x.symbol, data_error)

        score = 0.0
        reasons: List[str] = []
        risks: List[str] = []

        gap_pct = ((x.price - x.prev_close) / x.prev_close) * 100
        rel_volume = x.volume_today / x.avg_volume_20d

        float_rotation = None
        if x.float_shares and x.float_shares > 0:
            float_rotation = x.volume_today / x.float_shares
        else:
            risks.append("data_incomplete_risk")

        price_velocity = None
        if x.price_30s_ago and x.price_30s_ago > 0:
            price_velocity = ((x.price - x.price_30s_ago) / x.price_30s_ago) * 100

        volume_accel = None
        if x.volume_last_1m and x.avg_volume_last_5m and x.avg_volume_last_5m > 0:
            volume_accel = x.volume_last_1m / x.avg_volume_last_5m

        # --- Core squeeze signals ---

        if gap_pct >= 20:
            score += 15
            reasons.append(f"large_gap_{gap_pct:.1f}%")

        if rel_volume >= 5:
            score += 20
            reasons.append(f"relative_volume_{rel_volume:.1f}x")

        if x.float_shares and x.float_shares <= 20_000_000:
            score += 20
            reasons.append(f"low_float_{x.float_shares:,.0f}")

        if float_rotation and float_rotation >= 1:
            score += 20
            reasons.append(f"float_rotation_{float_rotation:.2f}x")

        if float_rotation and float_rotation >= 3:
            score += 10
            reasons.append("extreme_float_rotation")

        if x.news_catalyst:
            score += 10
            reasons.append("news_catalyst")

        if x.premarket_high and x.price > x.premarket_high:
            score += 15
            reasons.append("premarket_high_breakout")

        if price_velocity and price_velocity >= 2:
            score += 10
            reasons.append(f"price_acceleration_{price_velocity:.2f}%_30s")

        if volume_accel and volume_accel >= 2:
            score += 10
            reasons.append(f"volume_acceleration_{volume_accel:.2f}x")

        if x.short_interest_pct and x.short_interest_pct >= 15:
            score += 10
            reasons.append(f"high_short_interest_{x.short_interest_pct:.1f}%")

        if x.borrow_rate_pct and x.borrow_rate_pct >= 20:
            score += 5
            reasons.append(f"high_borrow_rate_{x.borrow_rate_pct:.1f}%")

        if x.borrow_rate_change_pct and x.borrow_rate_change_pct >= 25:
            score += 10
            reasons.append(f"borrow_rate_spike_{x.borrow_rate_change_pct:.1f}%")

        if x.shares_available_to_short is not None and x.shares_available_to_short <= 50_000:
            score += 10
            reasons.append("low_short_share_availability")

        # --- Confluence multipliers ---

        multiplier = 1.0

        if gap_pct >= 20 and rel_volume >= 5:
            multiplier += 0.20

        if x.news_catalyst and float_rotation and float_rotation >= 1:
            multiplier += 0.20

        if x.short_interest_pct and x.short_interest_pct >= 15 and x.shares_available_to_short is not None:
            if x.shares_available_to_short <= 50_000:
                multiplier += 0.15

        raw_score = min(100.0, score * multiplier)

        # --- Risk detection ---

        if x.data_freshness_ms and x.data_freshness_ms > 5_000:
            risks.append("stale_data_risk")

        if x.price < 5:
            risks.append("sub_five_dollar_regime")

        if gap_pct >= 150:
            risks.append("parabolic_gap_risk")

        if price_velocity and price_velocity >= 10:
            risks.append("blowoff_velocity_risk")

        if x.spread_bps and x.spread_bps > 100:
            risks.append("wide_spread_risk")

        if float_rotation and float_rotation >= 5:
            risks.append("late_stage_float_rotation_risk")

        if x.price < x.day_high * 0.85:
            risks.append("already_fading_from_high")

        # --- Risk penalties ---

        final_score = raw_score
        for risk in risks:
            final_score -= self.RISK_PENALTIES.get(risk, 0)

        final_score = max(0.0, min(final_score, 100.0))

        confidence = self._confidence(x, risks)

        grade, action_bias = self._grade(final_score, risks)

        return SqueezeResult(
            symbol=x.symbol,
            squeeze_score=round(final_score, 2),
            raw_score=round(raw_score, 2),
            confidence=round(confidence, 2),
            grade=grade,
            action_bias=action_bias,
            reasons=reasons,
            risk_flags=risks,
            metrics={
                "gap_pct": round(gap_pct, 2),
                "relative_volume": round(rel_volume, 2),
                "float_rotation": round(float_rotation, 2) if float_rotation else None,
                "price_velocity_30s_pct": round(price_velocity, 2) if price_velocity else None,
                "volume_acceleration": round(volume_accel, 2) if volume_accel else None,
                "risk_penalty_total": round(raw_score - final_score, 2),
            },
        )

    def _validate_hard_fields(self, x: SqueezeInput) -> Optional[str]:
        for field in self.HARD_DATA_FIELDS:
            value = getattr(x, field)
            if value is None or value <= 0:
                return f"invalid_{field}"
        return None

    def _data_error(self, symbol: str, reason: str) -> SqueezeResult:
        return SqueezeResult(
            symbol=symbol,
            squeeze_score=0.0,
            raw_score=0.0,
            confidence=0.0,
            grade="F",
            action_bias="DATA_ERROR",
            reasons=[reason],
            risk_flags=["data_feed_failure"],
            metrics={},
        )

    def _confidence(self, x: SqueezeInput, risks: List[str]) -> float:
        fields = [
            x.float_shares,
            x.premarket_high,
            x.short_interest_pct,
            x.borrow_rate_pct,
            x.spread_bps,
            x.price_30s_ago,
            x.volume_last_1m,
            x.avg_volume_last_5m,
        ]

        present = sum(v is not None for v in fields)
        confidence = present / len(fields)

        if "stale_data_risk" in risks:
            confidence *= 0.5

        if "data_incomplete_risk" in risks:
            confidence *= 0.75

        return max(0.0, min(confidence, 1.0))

    def _grade(self, score: float, risks: List[str]) -> tuple[str, str]:
        if "data_feed_failure" in risks:
            return "F", "DATA_ERROR"

        if "stale_data_risk" in risks:
            return "F", "WAIT_FOR_FRESH_DATA"

        if "wide_spread_risk" in risks or "already_fading_from_high" in risks:
            return "C", "RISK_DOWN_OR_WAIT"

        if score >= 80:
            return "A", "SQUEEZE_CANDIDATE"

        if score >= 60:
            return "B", "WATCH_FOR_BREAKOUT"

        if score >= 40:
            return "C", "WEAK_SQUEEZE_SETUP"

        return "D", "IGNORE"


if __name__ == "__main__":
    detector = SqueezeDetectorV2()

    test = SqueezeInput(
        symbol="PAVS",
        price=18.50,
        prev_close=3.00,
        day_high=22.00,
        premarket_high=6.50,
        volume_today=120_000_000,
        avg_volume_20d=2_000_000,
        float_shares=8_000_000,
        timestamp=time.time(),
        data_freshness_ms=800,
        short_interest_pct=18.0,
        borrow_rate_pct=35.0,
        borrow_rate_change_pct=40.0,
        shares_available_to_short=25_000,
        spread_bps=45,
        news_catalyst=True,
        price_30s_ago=17.90,
        volume_last_1m=3_000_000,
        avg_volume_last_5m=1_000_000,
    )

    print(detector.analyze(test))

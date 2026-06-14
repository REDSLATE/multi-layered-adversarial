"""BrainCalibration — Bayesian shrinkage of raw confidence to historical win rate.

Per (regime, conf_bucket) the calibrator tracks:
  total_signals, wins, avg_return_bps, max_drawdown_bps.

`calibrate(raw, regime)` returns (calibrated_confidence, CalibrationKey).
The brain embeds the key in BrainVote so the verifier can later audit:
  "you claimed CalibrationKey(regime='choppy', conf_bucket=0.9) but
   that bucket has only 3 historical samples — calibration invalid".

Cold-start posture: when a bucket has fewer than `min_samples`
observations the calibrator shrinks aggressively toward 0.5 (the base
rate). Once the bucket has 100+ samples it converges to a 60/40 mix of
raw vs observed win rate (i.e. max_shrinkage=0.6 by default).

Persistence: this is in-memory by design (Paradox v2 build-order step
2 — no infrastructure yet). A Mongo-backed adapter can wrap this
class without changing the calibration math.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from shared.brain_vote import CalibrationKey


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class CalibrationRecord:
    total_signals: int
    wins: int
    avg_return_bps: float
    max_drawdown_bps: float
    last_updated: datetime


class BrainCalibration:
    def __init__(
        self,
        brain_id: str,
        min_samples: int = 20,
        max_shrinkage: float = 0.6,
    ) -> None:
        self.brain_id = brain_id
        self.min_samples = min_samples
        self.max_shrinkage = max_shrinkage
        self._history: dict[CalibrationKey, CalibrationRecord] = {}

    # ── public surface ────────────────────────────────────────────────

    def record_outcome(
        self,
        key: CalibrationKey,
        won: bool,
        return_bps: float,
    ) -> None:
        """Verifier calls this after grading a fill. Updates the bucket
        in place; future `calibrate()` calls will shrink toward the new
        observed win rate."""
        rec = self._history.get(key)
        now = _now_utc()
        if rec is None:
            self._history[key] = CalibrationRecord(
                total_signals=1,
                wins=1 if won else 0,
                avg_return_bps=return_bps,
                max_drawdown_bps=min(return_bps, 0.0),
                last_updated=now,
            )
            return
        new_total = rec.total_signals + 1
        new_wins = rec.wins + (1 if won else 0)
        # Incremental mean (Welford-style, single pass).
        new_avg = rec.avg_return_bps + (return_bps - rec.avg_return_bps) / new_total
        self._history[key] = CalibrationRecord(
            total_signals=new_total,
            wins=new_wins,
            avg_return_bps=new_avg,
            max_drawdown_bps=min(rec.max_drawdown_bps, return_bps),
            last_updated=now,
        )

    def calibrate(
        self,
        raw_confidence: float,
        regime: str,
    ) -> tuple[float, CalibrationKey]:
        """Shrink `raw_confidence` toward observed win rate for
        (regime, conf_bucket). Returns the calibrated value and the
        key used so it can be embedded in the BrainVote."""
        key = CalibrationKey(regime=regime, conf_bucket=round(raw_confidence, 1))
        rec = self._history.get(key)

        if rec is None or rec.total_signals < self.min_samples:
            # Cold start — strong shrinkage toward the 0.5 base rate.
            # raw weighs 30%, base rate 70%.
            calibrated = raw_confidence * 0.3 + 0.5 * 0.7
            return round(calibrated, 4), key

        actual_wr = rec.wins / rec.total_signals
        n = rec.total_signals
        # Shrinkage ramps from 0 at n=0 to max_shrinkage at n=100.
        shrinkage = min(n / 100.0, self.max_shrinkage)

        # Extra penalty if historical avg return is materially negative.
        if rec.avg_return_bps < -20.0:
            shrinkage = min(shrinkage + 0.1, 0.8)

        calibrated = raw_confidence * (1.0 - shrinkage) + actual_wr * shrinkage
        return round(calibrated, 4), key

    # ── inspection helpers (verifier audit) ───────────────────────────

    def sample_count(self, key: CalibrationKey) -> int:
        rec = self._history.get(key)
        return rec.total_signals if rec else 0

    def history(self, key: CalibrationKey) -> Optional[CalibrationRecord]:
        return self._history.get(key)

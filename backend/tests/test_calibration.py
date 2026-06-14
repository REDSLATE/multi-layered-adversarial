"""BrainCalibration tests — Paradox v2."""
from __future__ import annotations

import pytest

from brains.calibration import BrainCalibration
from shared.brain_vote import CalibrationKey


def test_cold_start_shrinks_toward_base_rate():
    """Empty bucket → strong shrinkage. raw 0.9 must come down sharply
    toward 0.5."""
    cal = BrainCalibration(brain_id="alpha", min_samples=20)
    calibrated, key = cal.calibrate(raw_confidence=0.9, regime="choppy")
    # raw * 0.3 + 0.5 * 0.7 = 0.27 + 0.35 = 0.62
    assert calibrated == pytest.approx(0.62, abs=1e-3)
    assert key == CalibrationKey(regime="choppy", conf_bucket=0.9)


def test_cold_start_is_symmetric_around_05():
    cal = BrainCalibration(brain_id="alpha", min_samples=20)
    calibrated_high, _ = cal.calibrate(raw_confidence=0.9, regime="r")
    calibrated_low, _ = cal.calibrate(raw_confidence=0.1, regime="r")
    # |0.9 - 0.5| ≈ |0.1 - 0.5| after symmetric shrinkage.
    assert (calibrated_high - 0.5) == pytest.approx(-(calibrated_low - 0.5), abs=1e-3)


def test_warm_bucket_uses_observed_win_rate():
    cal = BrainCalibration(brain_id="alpha", min_samples=5, max_shrinkage=0.6)
    key = CalibrationKey(regime="trending", conf_bucket=0.7)
    # Observed: 8 wins / 10 samples → 0.8 wr
    for _ in range(8):
        cal.record_outcome(key, won=True, return_bps=30.0)
    for _ in range(2):
        cal.record_outcome(key, won=False, return_bps=-15.0)
    calibrated, returned_key = cal.calibrate(raw_confidence=0.7, regime="trending")
    assert returned_key == key
    # n=10 → shrinkage = 0.1; calibrated = 0.7*0.9 + 0.8*0.1 = 0.71
    assert calibrated == pytest.approx(0.71, abs=1e-3)


def test_record_outcome_increments_history():
    cal = BrainCalibration(brain_id="alpha")
    key = CalibrationKey(regime="trending", conf_bucket=0.7)
    cal.record_outcome(key, won=True, return_bps=10.0)
    cal.record_outcome(key, won=False, return_bps=-5.0)
    rec = cal.history(key)
    assert rec is not None
    assert rec.total_signals == 2
    assert rec.wins == 1
    assert rec.avg_return_bps == pytest.approx(2.5, abs=1e-3)
    assert rec.max_drawdown_bps == pytest.approx(-5.0)


def test_negative_avg_return_increases_shrinkage():
    """If historical avg return is < -20 bps, shrinkage gets an extra
    0.1 penalty — the brain's confidence in this bucket pays a tax."""
    cal = BrainCalibration(brain_id="alpha", min_samples=5)
    key = CalibrationKey(regime="news", conf_bucket=0.8)
    # 7 wins / 3 losses but big losses → avg < -20.
    for _ in range(7):
        cal.record_outcome(key, won=True, return_bps=10.0)
    for _ in range(3):
        cal.record_outcome(key, won=False, return_bps=-100.0)
    calibrated_with_penalty, _ = cal.calibrate(raw_confidence=0.8, regime="news")
    # Compare to a bucket with the same wr but smaller losses → no penalty.
    cal2 = BrainCalibration(brain_id="alpha", min_samples=5)
    for _ in range(7):
        cal2.record_outcome(key, won=True, return_bps=10.0)
    for _ in range(3):
        cal2.record_outcome(key, won=False, return_bps=-5.0)
    calibrated_no_penalty, _ = cal2.calibrate(raw_confidence=0.8, regime="news")
    # Penalty case must be MORE shrunk toward the (lower) observed wr.
    assert calibrated_with_penalty < calibrated_no_penalty


def test_sample_count_reads_history_size():
    cal = BrainCalibration(brain_id="alpha")
    key = CalibrationKey(regime="r", conf_bucket=0.5)
    assert cal.sample_count(key) == 0
    cal.record_outcome(key, won=True, return_bps=1.0)
    assert cal.sample_count(key) == 1

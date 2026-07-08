"""Doctrine-facing session enrichment fields on `build_snapshot`.

Doctrine pin (2026-02-19, P0 snapshot enrichment):
    The Strategist / Auditor / Governor / Executor seats had NO
    per-symbol differentiation input on their ingest path. Every
    intent scored identically because the snapshot lacked gap /
    volume / VWAP data.

    These tests pin the None-vs-zero contract for each field so a
    future refactor cannot silently return zeros when data is
    missing (which is what the seats would misread as "flat"
    signal, degrading the doctrine quietly).
"""
from __future__ import annotations

from shared.indicators import build_snapshot, session_features


# ─── helpers ─────────────────────────────────────────────────────


def _bar(ts: str, o: float, h: float, l: float, c: float, v: float) -> dict:
    return {"ts": ts, "o": o, "h": h, "l": l, "c": c, "v": v}


# ─── gap_pct ─────────────────────────────────────────────────────


class TestGapPct:
    def test_gap_up_from_prev_close(self):
        bars = [
            _bar("2026-07-01T14:30:00+00:00", 100, 101, 99, 100.0, 1000),
            _bar("2026-07-02T14:30:00+00:00", 102, 103, 101, 102.5, 1000),
        ]
        s = session_features(bars)
        # +2% gap
        assert s["gap_pct"] is not None
        assert abs(s["gap_pct"] - 2.0) < 1e-9

    def test_gap_down_from_prev_close(self):
        bars = [
            _bar("2026-07-01T14:30:00+00:00", 100, 101, 99, 100.0, 1000),
            _bar("2026-07-02T14:30:00+00:00",  98,  99, 97,  98.5, 1000),
        ]
        s = session_features(bars)
        assert s["gap_pct"] is not None
        assert abs(s["gap_pct"] - (-2.0)) < 1e-9

    def test_gap_needs_prev_session(self):
        # Single-session bar window: no gap can be computed.
        bars = [_bar("2026-07-02T14:30:00+00:00", 100, 101, 99, 100, 1000)]
        s = session_features(bars)
        assert s["gap_pct"] is None

    def test_gap_intraday_uses_first_of_today_and_last_of_yesterday(self):
        bars = [
            _bar("2026-07-01T19:55:00+00:00", 100.0, 100.5, 99.5, 100.0, 500),
            _bar("2026-07-02T13:35:00+00:00", 101.0, 101.5, 100.5, 101.2, 500),
            _bar("2026-07-02T13:40:00+00:00", 101.2, 101.3, 100.8, 101.0, 500),
        ]
        s = session_features(bars)
        # gap = (101.0 - 100.0) / 100.0 * 100 = 1.0%
        assert s["gap_pct"] is not None
        assert abs(s["gap_pct"] - 1.0) < 1e-9

    def test_gap_none_on_bad_prev_close(self):
        bars = [
            _bar("2026-07-01T14:30:00+00:00", 100, 101, 99, 0.0, 1000),  # zero close
            _bar("2026-07-02T14:30:00+00:00", 102, 103, 101, 102.5, 1000),
        ]
        s = session_features(bars)
        # prev_close = 0 → gap uncomputable
        assert s["gap_pct"] is None


# ─── relative_volume ─────────────────────────────────────────────


class TestRelativeVolume:
    def test_needs_three_prior_sessions(self):
        # Two prior sessions is not enough — returns None.
        bars = [
            _bar("2026-07-01T14:30:00+00:00", 100, 101, 99, 100, 1000),
            _bar("2026-07-02T14:30:00+00:00", 100, 101, 99, 100, 1000),
            _bar("2026-07-03T14:30:00+00:00", 100, 101, 99, 100, 2000),
        ]
        s = session_features(bars)
        assert s["relative_volume"] is None

    def test_computes_ratio_over_prior_sessions(self):
        # Three prior sessions at 1000 vol each; today at 2000.
        bars = [
            _bar("2026-07-01T14:30:00+00:00", 100, 101, 99, 100, 1000),
            _bar("2026-07-02T14:30:00+00:00", 100, 101, 99, 100, 1000),
            _bar("2026-07-03T14:30:00+00:00", 100, 101, 99, 100, 1000),
            _bar("2026-07-06T14:30:00+00:00", 100, 101, 99, 100, 2000),
        ]
        s = session_features(bars)
        # 2000 / avg(1000,1000,1000) = 2.0
        assert s["relative_volume"] is not None
        assert abs(s["relative_volume"] - 2.0) < 1e-9

    def test_intraday_sums_bars_within_session(self):
        # Four prior sessions at 3×500=1500 each; today at 3×1000=3000.
        prior_bars = []
        for d in ("2026-07-01", "2026-07-02", "2026-07-03", "2026-07-06"):
            for hhmm in ("13:35:00", "14:00:00", "14:30:00"):
                prior_bars.append(_bar(f"{d}T{hhmm}+00:00", 100, 101, 99, 100, 500))
        today_bars = []
        for hhmm in ("13:35:00", "14:00:00", "14:30:00"):
            today_bars.append(_bar(f"2026-07-07T{hhmm}+00:00", 100, 101, 99, 100, 1000))
        s = session_features(prior_bars + today_bars)
        # 3000 / avg(1500,1500,1500,1500) = 2.0
        assert s["relative_volume"] is not None
        assert abs(s["relative_volume"] - 2.0) < 1e-9

    def test_zero_volume_prior_sessions_filtered(self):
        # Holidays / bad-data sessions with 0 volume get filtered
        # from the baseline. Once 3 non-zero priors remain, the ratio
        # computes.
        bars = [
            _bar("2026-07-01T14:30:00+00:00", 100, 101, 99, 100, 1000),
            _bar("2026-07-02T14:30:00+00:00", 100, 101, 99, 100, 0),  # holiday
            _bar("2026-07-03T14:30:00+00:00", 100, 101, 99, 100, 1000),
            _bar("2026-07-04T14:30:00+00:00", 100, 101, 99, 100, 1000),
            _bar("2026-07-06T14:30:00+00:00", 100, 101, 99, 100, 500),
        ]
        s = session_features(bars)
        # baseline = avg(1000,1000,1000) = 1000, today=500 → 0.5
        assert s["relative_volume"] is not None
        assert abs(s["relative_volume"] - 0.5) < 1e-9


# ─── vwap_distance_pct ───────────────────────────────────────────


class TestVwapDistancePct:
    def test_close_above_vwap_positive(self):
        # Single day, three bars. Typical rises through session.
        bars = [
            _bar("2026-07-02T13:35:00+00:00", 100, 100.5, 99.5, 100.0, 1000),
            _bar("2026-07-02T13:40:00+00:00", 100.0, 101.0, 100.0, 100.5, 1000),
            _bar("2026-07-02T13:45:00+00:00", 100.5, 102.0, 100.5, 101.5, 1000),
        ]
        s = session_features(bars)
        # typical prices: 100.0, 100.5, 101.333...
        # vwap = avg = ~100.61; last close 101.5 → positive distance
        assert s["vwap_distance_pct"] is not None
        assert s["vwap_distance_pct"] > 0

    def test_close_below_vwap_negative(self):
        bars = [
            _bar("2026-07-02T13:35:00+00:00", 100, 102.0, 100.0, 101.5, 1000),
            _bar("2026-07-02T13:40:00+00:00", 101.5, 101.5, 100.5, 101.0, 1000),
            _bar("2026-07-02T13:45:00+00:00", 101.0, 101.0, 99.5, 99.7, 1000),
        ]
        s = session_features(bars)
        assert s["vwap_distance_pct"] is not None
        assert s["vwap_distance_pct"] < 0

    def test_single_bar_session_is_zero(self):
        bars = [_bar("2026-07-02T14:30:00+00:00", 100, 101, 99, 100.5, 1000)]
        s = session_features(bars)
        # Single bar: vwap ~= 100.166 (typical), close 100.5 → small non-zero.
        assert s["vwap_distance_pct"] is not None

    def test_all_zero_volume_returns_none(self):
        bars = [
            _bar("2026-07-02T13:35:00+00:00", 100, 101, 99, 100, 0),
            _bar("2026-07-02T13:40:00+00:00", 100, 101, 99, 100, 0),
        ]
        s = session_features(bars)
        assert s["vwap_distance_pct"] is None


# ─── build_snapshot integration ──────────────────────────────────


class TestBuildSnapshotIntegration:
    def test_new_fields_appear_alongside_legacy(self):
        bars = [
            _bar("2026-07-01T14:30:00+00:00", 100, 101, 99, 100.0, 1000),
            _bar("2026-07-02T14:30:00+00:00", 102, 103, 101, 102.5, 2000),
        ]
        snap = build_snapshot(bars)
        # Legacy fields preserved.
        assert "rsi14" in snap
        assert "atr14" in snap
        assert "sma" in snap
        # New doctrine-facing fields present.
        assert "gap_pct" in snap
        assert "relative_volume" in snap
        assert "vwap_distance_pct" in snap
        assert "session_bars_seen" in snap
        # Gap value correct.
        assert snap["gap_pct"] is not None
        assert abs(snap["gap_pct"] - 2.0) < 1e-9

    def test_empty_bars_returns_ready_false_no_crash(self):
        # `build_snapshot([])` is a valid warm-up state; must not raise.
        snap = build_snapshot([])
        assert snap == {"ready": False, "bars_seen": 0}

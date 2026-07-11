"""ParityKey / BarIdentity contract tests.

Pins the hardening decisions from the 2026-07 iter-27 packet:

    * timeframe is part of the join identity;
    * intraday sources must supply boundary-aligned timestamps;
    * daily identity accepts explicit open+close only;
    * parse_parity_key rejects malformed values;
    * canonical_close_from_evaluation_time is intraday-only;
    * schema label is a plain string, NOT the feature digest.

If any of these regress, the parity endpoint will silently pair
wrong events. These tests are the tripwire.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from mc_pulse.parity_key import (
    CAMINO_SNAPSHOT_SCHEMA_VERSION,
    BarIdentity,
    ParityKey,
    canonical_close_from_evaluation_time,
    daily_bar_identity,
    intraday_bar_identity,
    parse_parity_key,
)


# ─────────────────────── ParityKey.as_string ───────────────────────


def test_parity_key_as_string_includes_timeframe():
    """tf MUST be in the join string — two evaluations at the same
    close on 1m vs 5m are DIFFERENT market events."""
    key = ParityKey(
        brain_id="camino",
        symbol="NVDA",
        timeframe="1m",
        source_bar_close_at="2026-07-11T15:00:00+00:00",
    )
    assert "1m" in key.as_string()
    assert key.as_string().count("|") == 4    # 5 parts, 4 separators


def test_parity_key_hash_is_stable():
    """Same key → same hash. Cross-run stable for storage joins."""
    k1 = ParityKey("camino", "NVDA", "1m", "2026-07-11T15:00:00+00:00")
    k2 = ParityKey("camino", "NVDA", "1m", "2026-07-11T15:00:00+00:00")
    assert k1.hash() == k2.hash()


def test_parity_key_hash_differs_by_timeframe():
    k1 = ParityKey("camino", "NVDA", "1m", "2026-07-11T15:00:00+00:00")
    k5 = ParityKey("camino", "NVDA", "5m", "2026-07-11T15:00:00+00:00")
    assert k1.hash() != k5.hash()


# ─────────────────────── canonical_close_from_evaluation_time ───────


def test_canonical_close_flooring_1m():
    """15:00:12 → 15:00:00 for tf=1m (last completed 1m bar close)."""
    ts = datetime(2026, 7, 11, 15, 0, 12, tzinfo=timezone.utc)
    close = canonical_close_from_evaluation_time(ts, "1m")
    assert close == datetime(2026, 7, 11, 15, 0, 0, tzinfo=timezone.utc)


def test_canonical_close_flooring_5m():
    ts = datetime(2026, 7, 11, 15, 3, 45, tzinfo=timezone.utc)
    close = canonical_close_from_evaluation_time(ts, "5m")
    assert close == datetime(2026, 7, 11, 15, 0, 0, tzinfo=timezone.utc)


def test_canonical_close_naive_ts_treated_as_utc():
    ts = datetime(2026, 7, 11, 15, 0, 12)  # naive
    close = canonical_close_from_evaluation_time(ts, "1m")
    assert close == datetime(2026, 7, 11, 15, 0, 0, tzinfo=timezone.utc)


def test_canonical_close_rejects_daily():
    """1d cannot be derived from wall-clock alone — must raise."""
    ts = datetime(2026, 7, 11, 15, 0, 0, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="unsupported"):
        canonical_close_from_evaluation_time(ts, "1d")


def test_canonical_close_rejects_unknown_tf():
    ts = datetime(2026, 7, 11, 15, 0, 0, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="unsupported"):
        canonical_close_from_evaluation_time(ts, "1min")   # typo


# ─────────────────────── intraday_bar_identity ────────────────────────


def test_intraday_open_labelled_bar_produces_correct_boundaries():
    ts_open = datetime(2026, 7, 11, 15, 0, 0, tzinfo=timezone.utc)
    bar = intraday_bar_identity(
        timeframe="1m", bar_timestamp=ts_open,
        timestamp_semantics="open", source="shared_ohlcv_bars",
    )
    assert bar.open_at == ts_open
    assert bar.close_at == ts_open + timedelta(minutes=1)
    assert bar.source == "shared_ohlcv_bars"


def test_intraday_close_labelled_bar_produces_correct_boundaries():
    ts_close = datetime(2026, 7, 11, 15, 1, 0, tzinfo=timezone.utc)
    bar = intraday_bar_identity(
        timeframe="1m", bar_timestamp=ts_close,
        timestamp_semantics="close", source="webull_snapshot",
    )
    assert bar.close_at == ts_close
    assert bar.open_at == ts_close - timedelta(minutes=1)


def test_intraday_rejects_non_aligned_timestamp():
    """15:00:12 for a nominal 1m bar is a feeder defect — must raise."""
    ts = datetime(2026, 7, 11, 15, 0, 12, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="non-aligned"):
        intraday_bar_identity(
            timeframe="1m", bar_timestamp=ts,
            timestamp_semantics="open", source="test_feeder",
        )


def test_intraday_rejects_daily_tf():
    ts = datetime(2026, 7, 11, 0, 0, 0, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="unsupported"):
        intraday_bar_identity(
            timeframe="1d", bar_timestamp=ts,
            timestamp_semantics="open", source="test",
        )


def test_intraday_rejects_bad_semantics():
    ts = datetime(2026, 7, 11, 15, 0, 0, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="timestamp_semantics"):
        intraday_bar_identity(
            timeframe="1m", bar_timestamp=ts,
            timestamp_semantics="midpoint", source="test",
        )


# ─────────────────────── daily_bar_identity ───────────────────────


def test_daily_identity_accepts_explicit_boundaries():
    open_at = datetime(2026, 7, 11, 14, 30, 0, tzinfo=timezone.utc)   # NYSE open
    close_at = datetime(2026, 7, 11, 21, 0, 0, tzinfo=timezone.utc)   # NYSE close
    bar = daily_bar_identity(
        open_at=open_at, close_at=close_at, source="polygon_flatfiles",
    )
    assert bar.timeframe == "1d"
    assert bar.open_at == open_at
    assert bar.close_at == close_at


def test_daily_identity_rejects_reversed_boundaries():
    open_at = datetime(2026, 7, 11, 21, 0, 0, tzinfo=timezone.utc)
    close_at = datetime(2026, 7, 11, 14, 30, 0, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="strictly after"):
        daily_bar_identity(
            open_at=open_at, close_at=close_at, source="test",
        )


def test_daily_identity_rejects_zero_length_bar():
    ts = datetime(2026, 7, 11, 0, 0, 0, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="strictly after"):
        daily_bar_identity(open_at=ts, close_at=ts, source="test")


# ─────────────────────── to_parity_key composition ───────────────────


def test_to_parity_key_normalizes_case():
    ts = datetime(2026, 7, 11, 15, 0, 0, tzinfo=timezone.utc)
    bar = intraday_bar_identity(
        timeframe="1m", bar_timestamp=ts,
        timestamp_semantics="open", source="test",
    )
    key = bar.to_parity_key(brain_id="CAMINO", symbol="nvda")
    assert key.brain_id == "camino"
    assert key.symbol == "NVDA"
    assert key.timeframe == "1m"
    assert key.snapshot_schema_version == CAMINO_SNAPSHOT_SCHEMA_VERSION


# ─────────────────────── parse_parity_key ───────────────────────


def test_parse_roundtrips_wellformed_key():
    ts = datetime(2026, 7, 11, 15, 0, 0, tzinfo=timezone.utc)
    bar = intraday_bar_identity(
        timeframe="1m", bar_timestamp=ts,
        timestamp_semantics="open", source="test",
    )
    key = bar.to_parity_key(brain_id="camino", symbol="NVDA")
    parsed = parse_parity_key(key.as_string())
    assert parsed is not None
    assert parsed.as_string() == key.as_string()


def test_parse_rejects_wrong_part_count():
    assert parse_parity_key("camino|NVDA|1m") is None
    assert parse_parity_key("a|b|c|d|e|f") is None


def test_parse_rejects_unsupported_tf():
    raw = "camino|NVDA|1min|2026-07-11T15:00:00+00:00|camino-feature-v1"
    assert parse_parity_key(raw) is None


def test_parse_rejects_naive_close():
    raw = "camino|NVDA|1m|2026-07-11T15:00:00|camino-feature-v1"
    assert parse_parity_key(raw) is None


def test_parse_rejects_unparseable_close():
    raw = "camino|NVDA|1m|not-a-timestamp|camino-feature-v1"
    assert parse_parity_key(raw) is None


def test_parse_rejects_empty_fields():
    raw = "camino||1m|2026-07-11T15:00:00+00:00|camino-feature-v1"
    assert parse_parity_key(raw) is None
    raw = "|NVDA|1m|2026-07-11T15:00:00+00:00|camino-feature-v1"
    assert parse_parity_key(raw) is None


def test_parse_rejects_non_string_input():
    assert parse_parity_key(None) is None
    assert parse_parity_key(123) is None    # type: ignore[arg-type]


def test_parse_normalizes_casing():
    """A stored key with unusual casing must compare equal to a
    freshly built one for the same (brain, symbol, tf, close)."""
    raw = "CAMINO|nvda|1m|2026-07-11T15:00:00+00:00|camino-feature-v1"
    parsed = parse_parity_key(raw)
    assert parsed is not None
    assert parsed.brain_id == "camino"
    assert parsed.symbol == "NVDA"


# ─────────────────────── schema label is a label, not a hash ───────────


def test_schema_version_is_literal_label():
    """Reads as a human-checkable label. Feature-content identity
    lives on `feature_digest` on the manifest, deliberately outside
    the join key so parity math can COMPARE digests."""
    assert CAMINO_SNAPSHOT_SCHEMA_VERSION == "camino-feature-v1"

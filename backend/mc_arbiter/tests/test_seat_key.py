"""seat_key canonicalization + bucket flooring."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from mc_arbiter.seat_key import (
    BUCKET_MINUTES,
    bucket_iso,
    build_seat_key,
    next_bucket_iso,
    parse_seat_key,
)


# ── bucket_iso ───────────────────────────────────────────────────

def test_bucket_iso_floors_to_5min_boundary():
    # 14:33:47 → 14:30:00
    dt = datetime(2026, 7, 11, 14, 33, 47, 500_000, tzinfo=timezone.utc)
    assert bucket_iso(dt) == "2026-07-11T14:30:00Z"


def test_bucket_iso_exact_boundary_stays():
    dt = datetime(2026, 7, 11, 14, 30, 0, tzinfo=timezone.utc)
    assert bucket_iso(dt) == "2026-07-11T14:30:00Z"


def test_bucket_iso_top_of_hour():
    dt = datetime(2026, 7, 11, 14, 0, 0, tzinfo=timezone.utc)
    assert bucket_iso(dt) == "2026-07-11T14:00:00Z"


def test_bucket_iso_last_bucket_of_hour():
    dt = datetime(2026, 7, 11, 14, 59, 59, tzinfo=timezone.utc)
    assert bucket_iso(dt) == "2026-07-11T14:55:00Z"


def test_bucket_iso_treats_naive_dt_as_utc():
    # Naive dt → UTC. NEVER local time — silent local-time
    # interpretation is the exact bug class the 3-clock work
    # existed to eliminate.
    naive = datetime(2026, 7, 11, 14, 33, 47)
    assert bucket_iso(naive) == "2026-07-11T14:30:00Z"


def test_bucket_iso_converts_non_utc_tz():
    from datetime import timedelta as td
    et = timezone(td(hours=-4))  # EDT-ish (approx, no DST logic here)
    # 10:33 ET == 14:33 UTC → 14:30 UTC bucket
    dt = datetime(2026, 7, 11, 10, 33, 47, tzinfo=et)
    assert bucket_iso(dt) == "2026-07-11T14:30:00Z"


def test_bucket_iso_custom_interval():
    # 15-minute bucket for a hypothetical Phase 2 config change.
    dt = datetime(2026, 7, 11, 14, 33, 47, tzinfo=timezone.utc)
    assert bucket_iso(dt, minutes=15) == "2026-07-11T14:30:00Z"
    dt2 = datetime(2026, 7, 11, 14, 46, 0, tzinfo=timezone.utc)
    assert bucket_iso(dt2, minutes=15) == "2026-07-11T14:45:00Z"


def test_bucket_iso_default_is_now():
    # No dt → uses current UTC. Just verify format shape; the exact
    # value moves.
    result = bucket_iso()
    assert result.endswith("Z")
    assert "T" in result
    assert len(result) == 20


# ── build_seat_key ───────────────────────────────────────────────

def test_build_seat_key_basic():
    dt = datetime(2026, 7, 11, 14, 33, 0, tzinfo=timezone.utc)
    assert (
        build_seat_key("equity", "NVDA", dt)
        == "equity:NVDA:2026-07-11T14:30:00Z"
    )


def test_build_seat_key_uppercases_symbol():
    dt = datetime(2026, 7, 11, 14, 0, 0, tzinfo=timezone.utc)
    assert build_seat_key("crypto", "eth/usd", dt) == "crypto:ETH/USD:2026-07-11T14:00:00Z"


def test_build_seat_key_lowercases_lane():
    dt = datetime(2026, 7, 11, 14, 0, 0, tzinfo=timezone.utc)
    assert build_seat_key("EQUITY", "NVDA", dt).startswith("equity:")


def test_build_seat_key_rejects_unknown_lane():
    with pytest.raises(ValueError, match="lane"):
        build_seat_key("options", "NVDA")


def test_build_seat_key_rejects_empty_symbol():
    with pytest.raises(ValueError, match="symbol"):
        build_seat_key("equity", "")
    with pytest.raises(ValueError, match="symbol"):
        build_seat_key("equity", "   ")


# ── parse_seat_key ───────────────────────────────────────────────

def test_parse_seat_key_roundtrip_equity():
    dt = datetime(2026, 7, 11, 14, 33, 0, tzinfo=timezone.utc)
    key = build_seat_key("equity", "NVDA", dt)
    lane, symbol, bucket = parse_seat_key(key)
    assert lane == "equity"
    assert symbol == "NVDA"
    assert bucket == "2026-07-11T14:30:00Z"


def test_parse_seat_key_handles_slash_symbol():
    # Kraken symbols contain a slash — must roundtrip cleanly.
    key = "crypto:ETH/USD:2026-07-11T14:30:00Z"
    lane, symbol, bucket = parse_seat_key(key)
    assert lane == "crypto"
    assert symbol == "ETH/USD"
    assert bucket == "2026-07-11T14:30:00Z"


def test_parse_seat_key_rejects_malformed():
    with pytest.raises(ValueError):
        parse_seat_key("equity:NVDA")  # missing bucket
    with pytest.raises(ValueError):
        parse_seat_key("")


# ── next_bucket_iso ──────────────────────────────────────────────

def test_next_bucket_iso_from_iso():
    assert next_bucket_iso("2026-07-11T14:30:00Z") == "2026-07-11T14:35:00Z"


def test_next_bucket_iso_from_seat_key():
    assert (
        next_bucket_iso("equity:NVDA:2026-07-11T14:30:00Z")
        == "2026-07-11T14:35:00Z"
    )


def test_next_bucket_iso_crosses_hour():
    assert next_bucket_iso("2026-07-11T14:55:00Z") == "2026-07-11T15:00:00Z"


def test_bucket_minutes_default_is_5():
    # Defensive: if anyone changes this without updating the design
    # freeze, this test fires. 5-min bucketing is a doctrine choice,
    # not an implementation detail.
    assert BUCKET_MINUTES == 5

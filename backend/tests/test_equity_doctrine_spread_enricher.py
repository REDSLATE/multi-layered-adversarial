"""Equity spread enricher — field-drift regression fence (2026-02-28).

Doctrine (operator-approved 2026-02-28):

    `_quote_age_seconds` MUST probe both legacy AND current Webull
    SDK timestamp field names.

Background: prior to 2026-02-28 the parser only probed
`mkTradeTimeTs` / `tradeTimeTs`, but the current Webull SDK payload
carries `quote_time` / `last_trade_time` instead. Result: 41% of
equity intents in preview were being sized against a 25-bps sentinel
spread because `_quote_age_seconds` always returned None.

The fix (`shared/snapshot_enrich/equity_doctrine.py::_quote_age_seconds`)
extends the probe chain to include both new fields, preferring
`quote_time` over `last_trade_time` (see sign-off doc for rationale).

If a future refactor drops the new field names from the probe, this
test catches it before intents ship against fake spreads again.

Scope:
    * Only tests the timestamp field-name probe (fix #1 of the
      sign-off package).
    * Sign-off fixes #2 (sdk_bps sanity) and #3 (sentinel cap on
      derived spread) are NOT applied here — separate defensive
      additions awaiting operator sign-off + Monday RTH verification.
"""
from __future__ import annotations

import sys
import time

sys.path.insert(0, "/app/backend")


def _now_ms() -> int:
    return int(time.time() * 1000)


# ── Fixture: current Webull SDK payload shape (2026-07) ─────────

def _current_sdk_snap(ts_ms: int, *, use_last_trade=False) -> dict:
    """Match the 33-key NVDA payload the SDK returns today."""
    payload = {
        "symbol": "NVDA",
        "bid": 236.15,
        "ask": 236.20,
        "price": 236.18,
        "bps": 2.12,
    }
    if use_last_trade:
        payload["last_trade_time"] = ts_ms
    else:
        payload["quote_time"] = ts_ms
    return payload


# ── Fixture: legacy Webull SDK payload shape (pre-2026-07) ─────

def _legacy_sdk_snap(ts_ms: int) -> dict:
    return {
        "symbol": "NVDA", "bid": 236.15, "ask": 236.20,
        "price": 236.18, "bps": 2.12,
        "mkTradeTimeTs": ts_ms,
    }


# ── Fixture: current SDK payload with NO timestamp at all ──────

def _timeless_snap() -> dict:
    return {"symbol": "NVDA", "bid": 236.15, "ask": 236.20,
            "price": 236.18, "bps": 2.12}


# ═══════════════════════════════════════════════════════════════
# 1. FIELD-DRIFT FIX — extended probe reads current SDK payload
# ═══════════════════════════════════════════════════════════════

def test_extended_parser_reads_quote_time_from_current_sdk_payload():
    """POST-FIX: current SDK payload with `quote_time` field must
    yield a real age (not None). This is the top invariant — before
    the fix, this returned None on 24/24 sampled symbols."""
    from shared.snapshot_enrich.equity_doctrine import _quote_age_seconds
    now_ms = _now_ms()
    snap = _current_sdk_snap(now_ms - 5000)  # 5s ago
    age = _quote_age_seconds(snap)
    assert age is not None, (
        "field-drift fix regressed — `quote_time` no longer in probe chain"
    )
    assert 4.0 < age < 10.0, f"expected ~5s age; got {age}"


def test_extended_parser_reads_last_trade_time_from_current_sdk_payload():
    """POST-FIX: current SDK payload with `last_trade_time` (and NO
    `quote_time`) must still yield a real age."""
    from shared.snapshot_enrich.equity_doctrine import _quote_age_seconds
    now_ms = _now_ms()
    snap = _current_sdk_snap(now_ms - 12000, use_last_trade=True)
    age = _quote_age_seconds(snap)
    assert age is not None
    assert 11.0 < age < 16.0, f"expected ~12s age; got {age}"


def test_extended_parser_prefers_quote_time_over_last_trade_time():
    """When BOTH fields present, `quote_time` wins — it's the more
    recent value during an active session. Pinning this because if
    the doctrine ever swaps the preference order, spread staleness
    scoring silently regresses to the last-print age (which stops
    updating after RTH close)."""
    from shared.snapshot_enrich.equity_doctrine import _quote_age_seconds
    now_ms = _now_ms()
    snap = {
        "symbol": "NVDA",
        "quote_time": now_ms - 3000,        # 3s ago  (should WIN)
        "last_trade_time": now_ms - 60000,  # 60s ago
    }
    age = _quote_age_seconds(snap)
    assert age is not None
    assert 2.0 < age < 6.0, (
        f"expected quote_time-derived ~3s; got {age}. Preference order "
        f"regressed — `last_trade_time` may now be winning."
    )


def test_legacy_parser_still_reads_mkTradeTimeTs():
    """Legacy Webull SDK payloads (pre-2026-07) used `mkTradeTimeTs`.
    The fix EXTENDS the probe — it does not replace. If the SDK ever
    reverts to legacy shape, we must still read the field."""
    from shared.snapshot_enrich.equity_doctrine import _quote_age_seconds
    now_ms = _now_ms()
    snap = _legacy_sdk_snap(now_ms - 7000)
    age = _quote_age_seconds(snap)
    assert age is not None
    assert 6.0 < age < 10.0


def test_legacy_field_wins_over_new_when_both_present():
    """`mkTradeTimeTs` is listed first in the `or` chain, so if BOTH
    the legacy AND new fields are somehow both present, legacy wins.
    Pinning this so a future refactor doesn't silently reorder the
    chain and break payloads that carry both."""
    from shared.snapshot_enrich.equity_doctrine import _quote_age_seconds
    now_ms = _now_ms()
    snap = {
        "symbol": "NVDA",
        "mkTradeTimeTs": now_ms - 2000,   # 2s — should WIN
        "quote_time": now_ms - 30000,     # 30s
    }
    age = _quote_age_seconds(snap)
    assert age is not None
    assert age < 5.0, (
        "probe order changed — `quote_time` won when `mkTradeTimeTs` "
        "was present; that regresses legacy payloads."
    )


def test_timeless_snapshot_still_returns_none():
    """A payload with NEITHER timestamp field must still return None
    so the downstream tagger marks the quote `stale`. The fix is
    ADDITIVE — it doesn't hallucinate timestamps."""
    from shared.snapshot_enrich.equity_doctrine import _quote_age_seconds
    assert _quote_age_seconds(_timeless_snap()) is None


def test_non_dict_input_returns_none():
    """Defensive input handling — a non-dict snap should never crash."""
    from shared.snapshot_enrich.equity_doctrine import _quote_age_seconds
    assert _quote_age_seconds(None) is None
    assert _quote_age_seconds("not a dict") is None
    assert _quote_age_seconds(12345) is None


def test_malformed_timestamp_falls_through_to_iso_probe():
    """A payload whose ms-epoch fields are non-numeric strings must
    NOT crash — the parser falls through to the ISO probe branch."""
    from shared.snapshot_enrich.equity_doctrine import _quote_age_seconds
    snap = {"symbol": "NVDA", "quote_time": "not-a-number"}
    # Doesn't crash. Returns None since no ISO field either.
    assert _quote_age_seconds(snap) is None

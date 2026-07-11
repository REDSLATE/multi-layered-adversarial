"""Input manifest contract tests.

Pins the invariants the parity endpoint relies on:

    * feature_digest is deterministic (byte-for-byte stable
      across processes for the same feature set + values);
    * feature_digest is NOT part of the ParityKey (it must be
      COMPARED across paths, not used to join them);
    * missing_fields correctly names every absent required field;
    * to_mongo output contains BarIdentity.source so the endpoint
      can report "same canonical event, different upstream
      sources" even though source is not in the join key;
    * fallback_used is preserved on the manifest so a
      runner-hot vs pulse-cold divergence is visible;
    * status + reason_codes propagate for INSUFFICIENT_DATA runs.
"""
from __future__ import annotations

from datetime import datetime, timezone

from mc_pulse.input_manifest import (
    CAMINO_REQUIRED_FIELDS,
    build_camino_manifest,
)
from mc_pulse.parity_key import intraday_bar_identity


def _bar():
    ts = datetime(2026, 7, 11, 15, 0, 0, tzinfo=timezone.utc)
    return intraday_bar_identity(
        timeframe="1m", bar_timestamp=ts,
        timestamp_semantics="open", source="shared_ohlcv_bars",
    )


def _full_snapshot():
    """Snapshot dict with every required field populated. Used to
    exercise the OK-path manifest."""
    return {
        "trend_score": 0.42,
        "price_change_pct": 1.23,
        "volume_change_pct": 8.1,
        "rsi": 55.0,
        "spread_bps": 3.0,
        "volatility": 0.15,
        "liquidity_score": 0.85,
        "setup_score": 0.71,
        "gap_pct": 0.5,
        "relative_volume": 1.8,
        "vwap_distance_pct": 0.2,
        "market_regime": "trending",
        "spread_quality": "live",
    }


# ─────────────────────── digest determinism ─────────────────────────


def test_feature_digest_identical_for_identical_inputs():
    """Byte-for-byte stable across two independent builds — the
    whole join layer breaks if this regresses."""
    bar = _bar()
    key = bar.to_parity_key(brain_id="camino", symbol="NVDA")
    snap = _full_snapshot()

    a = build_camino_manifest(
        parity_key=key, path="runner", bar=bar,
        source_bar_id="bar-1", snapshot=snap,
        fallback_used=False, position_context_present=False,
        bar_count=30, action="BUY", confidence=0.68,
    )
    b = build_camino_manifest(
        parity_key=key, path="pulse", bar=bar,
        source_bar_id="bar-1", snapshot=snap,
        fallback_used=False, position_context_present=False,
        bar_count=30, action="BUY", confidence=0.68,
    )
    assert a.feature_digest == b.feature_digest


def test_feature_digest_differs_when_values_differ():
    """A 1bp shift in spread_bps → different digest. This is what
    the parity endpoint uses to say `snapshot_input_match=0.6`."""
    bar = _bar()
    key = bar.to_parity_key(brain_id="camino", symbol="NVDA")
    snap_a = _full_snapshot()
    snap_b = _full_snapshot()
    snap_b["spread_bps"] = 3.1

    a = build_camino_manifest(
        parity_key=key, path="runner", bar=bar,
        source_bar_id="x", snapshot=snap_a,
        fallback_used=False, position_context_present=False,
        bar_count=30, action="BUY", confidence=0.5,
    )
    b = build_camino_manifest(
        parity_key=key, path="pulse", bar=bar,
        source_bar_id="x", snapshot=snap_b,
        fallback_used=False, position_context_present=False,
        bar_count=30, action="BUY", confidence=0.5,
    )
    assert a.feature_digest != b.feature_digest


def test_feature_digest_differs_when_field_absent_vs_present():
    """`price_change_pct=None` (absent) is a distinct manifest
    from `price_change_pct=0.0` (real zero). If we hashed these
    the same, a starved-input path could masquerade as identical
    to a fresh-input path."""
    bar = _bar()
    key = bar.to_parity_key(brain_id="camino", symbol="NVDA")
    with_val = _full_snapshot()
    with_val["price_change_pct"] = 0.0
    without_val = _full_snapshot()
    without_val.pop("price_change_pct")

    a = build_camino_manifest(
        parity_key=key, path="runner", bar=bar,
        source_bar_id="x", snapshot=with_val,
        fallback_used=False, position_context_present=False,
        bar_count=30, action="HOLD", confidence=0.5,
    )
    b = build_camino_manifest(
        parity_key=key, path="pulse", bar=bar,
        source_bar_id="x", snapshot=without_val,
        fallback_used=False, position_context_present=False,
        bar_count=30, action="HOLD", confidence=0.5,
    )
    assert a.feature_digest != b.feature_digest


def test_feature_digest_absent_from_parity_key():
    """Feature digest MUST NOT sneak into the join key — parity
    math needs to compare digests across paths, not use them to
    join records."""
    bar = _bar()
    key = bar.to_parity_key(brain_id="camino", symbol="NVDA")
    snap = _full_snapshot()
    m = build_camino_manifest(
        parity_key=key, path="runner", bar=bar,
        source_bar_id="x", snapshot=snap,
        fallback_used=False, position_context_present=False,
        bar_count=30, action="BUY", confidence=0.6,
    )
    # Neither field of the ParityKey should be the digest, and
    # the digest should not appear inside the join string.
    assert m.feature_digest not in key.as_string()


# ─────────────────────── available/missing detection ─────────────────


def test_missing_fields_lists_absent_required():
    """Path-side sanity: the manifest names EXACTLY which required
    fields the snapshot lacked. Diagnosis of the HOLD@1.0 signature
    depends on this."""
    bar = _bar()
    key = bar.to_parity_key(brain_id="camino", symbol="NVDA")
    partial = {
        "trend_score": 0.4, "price_change_pct": 1.0,
        "rsi": 50.0, "liquidity_score": 0.85,
        # missing: volume_change_pct, spread_bps, volatility, setup_score
    }
    m = build_camino_manifest(
        parity_key=key, path="pulse", bar=bar,
        source_bar_id="x", snapshot=partial,
        fallback_used=False, position_context_present=False,
        bar_count=30, action="HOLD", confidence=0.0,
    )
    expected_missing = {"volume_change_pct", "spread_bps", "volatility", "setup_score"}
    assert expected_missing.issubset(set(m.missing_fields))


def test_available_fields_excludes_none_values():
    """`field=None` is NOT considered present — parity must not
    mask a starved input as "field there, value zero"."""
    bar = _bar()
    key = bar.to_parity_key(brain_id="camino", symbol="NVDA")
    snap = _full_snapshot()
    snap["spread_bps"] = None
    m = build_camino_manifest(
        parity_key=key, path="pulse", bar=bar,
        source_bar_id="x", snapshot=snap,
        fallback_used=False, position_context_present=False,
        bar_count=30, action="HOLD", confidence=0.0,
    )
    assert "spread_bps" in m.missing_fields
    assert "spread_bps" not in m.available_fields


# ─────────────────────── to_mongo shape ──────────────────────


def test_to_mongo_preserves_source_from_bar_identity():
    """`source` is EXCLUDED from the ParityKey (per operator
    directive — different upstream sources for the same market
    event are a finding, not a mismatch). It MUST be preserved on
    the manifest so the endpoint can surface the divergence."""
    bar = _bar()
    key = bar.to_parity_key(brain_id="camino", symbol="NVDA")
    m = build_camino_manifest(
        parity_key=key, path="pulse", bar=bar,
        source_bar_id="bar-1", snapshot=_full_snapshot(),
        fallback_used=False, position_context_present=False,
        bar_count=30, action="BUY", confidence=0.7,
    )
    doc = m.to_mongo()
    assert doc["source"] == "shared_ohlcv_bars"
    assert doc["parity_key"] == key.as_string()
    assert doc["path"] == "pulse"


def test_to_mongo_carries_action_confidence_status_reason_codes():
    bar = _bar()
    key = bar.to_parity_key(brain_id="camino", symbol="NVDA")
    m = build_camino_manifest(
        parity_key=key, path="pulse", bar=bar,
        source_bar_id="x", snapshot=_full_snapshot(),
        fallback_used=True, position_context_present=True,
        bar_count=12, action="HOLD", confidence=0.0,
        status="INSUFFICIENT_DATA",
        reason_codes=("MISSING_REQUIRED_FEATURES", "trend_score"),
    )
    doc = m.to_mongo()
    assert doc["action"] == "HOLD"
    assert doc["confidence"] == 0.0
    assert doc["status"] == "INSUFFICIENT_DATA"
    assert "MISSING_REQUIRED_FEATURES" in doc["reason_codes"]
    assert doc["fallback_used"] is True
    assert doc["position_context_present"] is True
    assert doc["bar_count"] == 12


def test_to_mongo_carries_source_bar_open_and_close():
    """Open + close BOTH shipped on the manifest so a subsequent
    audit can reconstruct the bar without another Mongo hop."""
    bar = _bar()
    key = bar.to_parity_key(brain_id="camino", symbol="NVDA")
    m = build_camino_manifest(
        parity_key=key, path="runner", bar=bar,
        source_bar_id="bar-1", snapshot=_full_snapshot(),
        fallback_used=False, position_context_present=False,
        bar_count=30, action="BUY", confidence=0.7,
    )
    doc = m.to_mongo()
    assert doc["source_bar_open_at"] == bar.open_at.isoformat()
    assert doc["source_bar_close_at"] == bar.close_at.isoformat()


# ─────────────────────── numeric stability ───────────────────


def test_feature_values_rounded_for_stability():
    """Bit-level float jitter across OS/Python builds MUST NOT
    produce different digests for what is effectively the same
    value. Round to 4dp for parity stability."""
    bar = _bar()
    key = bar.to_parity_key(brain_id="camino", symbol="NVDA")
    snap_a = _full_snapshot()
    snap_b = _full_snapshot()
    # trend_score differs only at the 6th decimal — should NOT
    # produce a distinct digest.
    snap_a["trend_score"] = 0.420001
    snap_b["trend_score"] = 0.420002
    a = build_camino_manifest(
        parity_key=key, path="runner", bar=bar,
        source_bar_id="x", snapshot=snap_a,
        fallback_used=False, position_context_present=False,
        bar_count=30, action="BUY", confidence=0.5,
    )
    b = build_camino_manifest(
        parity_key=key, path="pulse", bar=bar,
        source_bar_id="x", snapshot=snap_b,
        fallback_used=False, position_context_present=False,
        bar_count=30, action="BUY", confidence=0.5,
    )
    assert a.feature_digest == b.feature_digest

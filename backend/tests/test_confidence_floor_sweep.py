"""Confidence-floor calibration sweep — endpoint tests.

The load-bearing invariant under test: HOLD intents NEVER count toward
any floor's pass count, regardless of confidence. Everything else
(dampener accounting, outcome join, brain breakdown) is supporting
behavior that must not undermine the invariant.
"""
from __future__ import annotations

import requests

from shared.calibration.confidence_floor_sweep import DIRECTIONAL_ACTIONS


# ───── auth / shape ───────────────────────────────────────────────────


def test_endpoint_requires_auth(base_url):
    r = requests.get(
        f"{base_url}/api/admin/calibration/confidence-floor-sweep",
        timeout=15,
    )
    assert r.status_code in (401, 403)


def test_default_sweep_shape(auth_client, base_url):
    r = auth_client.get(
        f"{base_url}/api/admin/calibration/confidence-floor-sweep?hours=24",
        timeout=20,
    )
    assert r.status_code == 200
    body = r.json()
    # Required top-level fields
    for k in [
        "lane",
        "hours",
        "directional_actions",
        "total_intents_in_window",
        "total_directional",
        "total_hold",
        "rejected_at_ingest",
        "floor_bites",
        "data_quality",
        "floors",
        "by_brain",
        "outcome_join",
        "notes",
    ]:
        assert k in body, f"missing top-level key: {k}"

    # The directional set surfaced on the response must match the
    # doctrine constant exactly (no string drift between docs & code).
    assert set(body["directional_actions"]) == DIRECTIONAL_ACTIONS

    # Each floor bucket has the expected shape.
    for f in body["floors"]:
        for k in [
            "floor",
            "raw_pass",
            "effective_pass",
            "dampener_drop",
            "resolved",
            "wins",
            "losses",
            "win_rate",
            "pnl_usd",
        ]:
            assert k in f, f"missing floor bucket key: {k}"


# ───── HOLD invariant — load-bearing ──────────────────────────────────


def test_hold_invariant_directional_actions_set(auth_client, base_url):
    """Sanity: the directional set in the response must not silently
    expand to include HOLD. If it does, the invariant has rotted."""
    r = auth_client.get(
        f"{base_url}/api/admin/calibration/confidence-floor-sweep?hours=24",
        timeout=20,
    )
    body = r.json()
    actions = set(body["directional_actions"])
    assert "HOLD" not in actions, (
        "doctrine violation: HOLD must NEVER be in directional_actions"
    )
    assert actions == {"BUY", "SELL", "SHORT", "COVER"}


def test_hold_intents_never_counted_in_passes(auth_client, base_url):
    """If `total_hold > 0` and floor=0.00, the maximum possible
    `effective_pass` is bounded by `total_directional` MINUS rejected
    rows. HOLD must contribute zero passes even at floor 0.00."""
    r = auth_client.get(
        f"{base_url}/api/admin/calibration/confidence-floor-sweep?hours=168&floors=0.00",
        timeout=20,
    )
    body = r.json()
    if body["total_directional"] == 0 and body["total_hold"] == 0:
        return  # nothing to assert in an empty window

    floor_zero = body["floors"][0]
    # At floor 0.00, pass count cannot exceed the directional executable
    # population (the data_quality counter, which already excludes HOLD
    # and rejected_at_ingest).
    cap = body["data_quality"]["directional_executable"]
    assert floor_zero["effective_pass"] <= cap
    assert floor_zero["raw_pass"] <= cap


# ───── dampener accounting — no negative drops ────────────────────────


def test_dampener_drop_never_negative(auth_client, base_url):
    """Legacy rows missing raw_confidence must not cause a negative
    `dampener_drop`. The fix: count drops only on paired rows."""
    r = auth_client.get(
        f"{base_url}/api/admin/calibration/confidence-floor-sweep?hours=720",
        timeout=20,
    )
    body = r.json()
    for f in body["floors"]:
        assert f["dampener_drop"] >= 0, (
            f"negative dampener_drop at floor {f['floor']}: {f['dampener_drop']}"
        )


# ───── custom floors / lane filter ────────────────────────────────────


def test_custom_floors_honored(auth_client, base_url):
    r = auth_client.get(
        f"{base_url}/api/admin/calibration/confidence-floor-sweep"
        f"?hours=24&floors=0.10,0.50,0.90",
        timeout=20,
    )
    body = r.json()
    assert [f["floor"] for f in body["floors"]] == [0.10, 0.50, 0.90]


def test_malformed_floors_falls_back_to_default(auth_client, base_url):
    r = auth_client.get(
        f"{base_url}/api/admin/calibration/confidence-floor-sweep"
        f"?hours=24&floors=garbage,abc,xyz",
        timeout=20,
    )
    body = r.json()
    # Default sweep should be applied
    assert [f["floor"] for f in body["floors"]] == [
        0.00, 0.10, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45,
    ]


def test_bad_lane_rejected(auth_client, base_url):
    r = auth_client.get(
        f"{base_url}/api/admin/calibration/confidence-floor-sweep?lane=fx",
        timeout=20,
    )
    assert r.status_code == 422


def test_lane_filter_narrows_population(auth_client, base_url):
    all_r = auth_client.get(
        f"{base_url}/api/admin/calibration/confidence-floor-sweep?hours=168",
        timeout=20,
    ).json()
    crypto_r = auth_client.get(
        f"{base_url}/api/admin/calibration/confidence-floor-sweep?hours=168&lane=crypto",
        timeout=20,
    ).json()
    # Lane filter narrowing the window must not produce MORE rows.
    assert crypto_r["total_intents_in_window"] <= all_r["total_intents_in_window"]


# ───── floor_bites diagnostic ─────────────────────────────────────────


def test_floor_bites_false_when_range_flat(auth_client, base_url):
    """If the floor range admits identical pass counts across every
    bucket, `floor_bites=False` so the operator doesn't mistake a flat
    curve for an informative one."""
    # A single-element sweep — by definition there's no variation, so
    # bites must be False.
    r = auth_client.get(
        f"{base_url}/api/admin/calibration/confidence-floor-sweep?hours=24&floors=0.30",
        timeout=20,
    )
    body = r.json()
    assert body["floor_bites"] is False


def test_by_brain_breakdown_sums_consistent(auth_client, base_url):
    """At any given floor, the sum of `effective_pass` across all
    brains must equal the aggregate `effective_pass`. (Same rows
    partitioned by `stack`; no double-counting allowed.)"""
    r = auth_client.get(
        f"{base_url}/api/admin/calibration/confidence-floor-sweep?hours=168",
        timeout=20,
    )
    body = r.json()
    for i, agg in enumerate(body["floors"]):
        brain_sum = sum(
            (body["by_brain"][b][i]["effective_pass"]) for b in body["by_brain"]
        )
        assert brain_sum == agg["effective_pass"], (
            f"floor {agg['floor']}: brain sum {brain_sum} != aggregate {agg['effective_pass']}"
        )


# ───── read-only contract ─────────────────────────────────────────────


def test_endpoint_is_idempotent(auth_client, base_url):
    """Two back-to-back calls return the same total counts. This is the
    read-only contract — the endpoint must never mutate state in a way
    that changes subsequent reads."""
    r1 = auth_client.get(
        f"{base_url}/api/admin/calibration/confidence-floor-sweep?hours=168",
        timeout=20,
    ).json()
    r2 = auth_client.get(
        f"{base_url}/api/admin/calibration/confidence-floor-sweep?hours=168",
        timeout=20,
    ).json()
    assert r1["total_intents_in_window"] == r2["total_intents_in_window"]
    assert r1["total_directional"] == r2["total_directional"]
    assert r1["rejected_at_ingest"] == r2["rejected_at_ingest"]
    for f1, f2 in zip(r1["floors"], r2["floors"]):
        assert f1["raw_pass"] == f2["raw_pass"]
        assert f1["effective_pass"] == f2["effective_pass"]

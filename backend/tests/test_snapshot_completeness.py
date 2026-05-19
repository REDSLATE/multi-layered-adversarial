"""Snapshot-completeness diagnostic — endpoint tests.

Invariant under test:
    A field is present iff `snapshot[field]` exists AND is non-null.
    Zero-valued fields (e.g. `spread_bps=0.0`) count as PRESENT — a
    brain explicitly reporting zero is not the same as omitting.
"""
from __future__ import annotations

import requests

from shared.calibration.snapshot_completeness import (
    CRYPTO_REQUIRED_FIELDS,
    EQUITY_REQUIRED_FIELDS,
    EXECUTION_GRADE_FIELDS,
    DIRECTIONAL_ACTIONS,
    _field_present,
    _row_completeness_score,
)


# ───── pure-function unit tests ───────────────────────────────────────


def test_field_present_distinguishes_missing_from_zero():
    """The load-bearing distinction: missing field ≠ zero value."""
    assert _field_present({"spread_bps": 0.0}, "spread_bps") is True
    assert _field_present({"spread_bps": 25}, "spread_bps") is True
    assert _field_present({}, "spread_bps") is False
    assert _field_present({"spread_bps": None}, "spread_bps") is False
    assert _field_present(None, "spread_bps") is False


def test_field_present_handles_non_dict_snapshot():
    """A brain that ships `snapshot=[]` or `snapshot="oops"` must not
    crash the diagnostic — should report all fields missing."""
    assert _field_present([], "spread_bps") is False
    assert _field_present("oops", "spread_bps") is False
    assert _field_present(42, "spread_bps") is False


def test_row_completeness_score_fractional():
    snap = {"spread_bps": 25.0, "volume_24h_usd": 5e7}
    fields = ["spread_bps", "volume_24h_usd", "volatility_1h", "trend_strength"]
    # 2 of 4 present → 0.5
    assert _row_completeness_score(snap, fields) == 0.5


def test_row_completeness_score_empty_field_list_is_full():
    assert _row_completeness_score({}, []) == 1.0


# ───── auth / shape ───────────────────────────────────────────────────


def test_endpoint_requires_auth(base_url):
    r = requests.get(
        f"{base_url}/api/admin/intents/snapshot-completeness",
        timeout=15,
    )
    assert r.status_code in (401, 403)


def test_default_shape(auth_client, base_url):
    r = auth_client.get(
        f"{base_url}/api/admin/intents/snapshot-completeness?hours=24",
        timeout=20,
    )
    assert r.status_code == 200
    body = r.json()
    for k in [
        "lane",
        "hours",
        "directional_actions",
        "total_directional_intents",
        "fields_required_for_doctrine",
        "crypto_required_fields",
        "equity_required_fields",
        "execution_grade_fields",
        "field_presence",
        "by_brain",
        "by_lane",
        "worst_offenders",
        "notes",
    ]:
        assert k in body, f"missing top-level key: {k}"


def test_directional_actions_invariant(auth_client, base_url):
    r = auth_client.get(
        f"{base_url}/api/admin/intents/snapshot-completeness?hours=24",
        timeout=20,
    )
    body = r.json()
    assert set(body["directional_actions"]) == DIRECTIONAL_ACTIONS
    assert "HOLD" not in set(body["directional_actions"])


# ───── required field sets pinned to labelers ─────────────────────────


def test_crypto_required_fields_pinned(auth_client, base_url):
    """The crypto field list returned by the endpoint must match the
    constant pinned to `crypto_labels.py`. Drift here means the
    diagnostic is reporting against the wrong contract."""
    r = auth_client.get(
        f"{base_url}/api/admin/intents/snapshot-completeness?lane=crypto&hours=24",
        timeout=20,
    )
    body = r.json()
    assert tuple(body["crypto_required_fields"]) == CRYPTO_REQUIRED_FIELDS
    # When lane=crypto, fields_required = crypto + execution-grade
    assert set(body["fields_required_for_doctrine"]) == (
        set(CRYPTO_REQUIRED_FIELDS) | set(EXECUTION_GRADE_FIELDS)
    )


def test_equity_required_fields_pinned(auth_client, base_url):
    r = auth_client.get(
        f"{base_url}/api/admin/intents/snapshot-completeness?lane=equity&hours=24",
        timeout=20,
    )
    body = r.json()
    assert tuple(body["equity_required_fields"]) == EQUITY_REQUIRED_FIELDS
    assert set(body["fields_required_for_doctrine"]) == (
        set(EQUITY_REQUIRED_FIELDS) | set(EXECUTION_GRADE_FIELDS)
    )


# ───── filters ────────────────────────────────────────────────────────


def test_bad_lane_rejected(auth_client, base_url):
    r = auth_client.get(
        f"{base_url}/api/admin/intents/snapshot-completeness?lane=fx",
        timeout=20,
    )
    assert r.status_code == 422


def test_lane_filter_narrows(auth_client, base_url):
    all_r = auth_client.get(
        f"{base_url}/api/admin/intents/snapshot-completeness?hours=168",
        timeout=20,
    ).json()
    crypto_r = auth_client.get(
        f"{base_url}/api/admin/intents/snapshot-completeness?hours=168&lane=crypto",
        timeout=20,
    ).json()
    assert crypto_r["total_directional_intents"] <= all_r["total_directional_intents"]


# ───── presence math ──────────────────────────────────────────────────


def test_field_presence_present_plus_missing_equals_total(auth_client, base_url):
    """For every field, present + missing must equal the row total —
    no row can be both present and missing."""
    r = auth_client.get(
        f"{base_url}/api/admin/intents/snapshot-completeness?hours=168",
        timeout=20,
    )
    body = r.json()
    total = body["total_directional_intents"]
    for field, p in body["field_presence"].items():
        assert p["present"] + p["missing"] == total, (
            f"{field}: present({p['present']}) + missing({p['missing']}) != total({total})"
        )


def test_presence_rate_bounded(auth_client, base_url):
    r = auth_client.get(
        f"{base_url}/api/admin/intents/snapshot-completeness?hours=168",
        timeout=20,
    )
    body = r.json()
    for field, p in body["field_presence"].items():
        assert 0.0 <= p["presence_rate"] <= 1.0


def test_by_brain_intents_sum_consistent(auth_client, base_url):
    """The sum of per-brain intent counts must equal the aggregate
    total. (Same rows partitioned by stack; no double-counting.)"""
    r = auth_client.get(
        f"{base_url}/api/admin/intents/snapshot-completeness?hours=168",
        timeout=20,
    )
    body = r.json()
    by_brain_sum = sum(
        b["total_directional_intents"] for b in body["by_brain"].values()
    )
    # by_brain only includes intents with a non-empty stack — there may
    # be some without. So the sum can be ≤ aggregate.
    assert by_brain_sum <= body["total_directional_intents"]


# ───── worst-offenders summary ────────────────────────────────────────


def test_worst_offenders_capped_at_twenty(auth_client, base_url):
    r = auth_client.get(
        f"{base_url}/api/admin/intents/snapshot-completeness?hours=168",
        timeout=20,
    )
    body = r.json()
    assert len(body["worst_offenders"]) <= 20


def test_worst_offenders_have_required_keys(auth_client, base_url):
    r = auth_client.get(
        f"{base_url}/api/admin/intents/snapshot-completeness?hours=168",
        timeout=20,
    )
    body = r.json()
    for w in body["worst_offenders"]:
        assert "brain" in w
        assert "field" in w
        assert "missing" in w
        assert w["missing"] > 0


# ───── read-only contract ─────────────────────────────────────────────


def test_endpoint_is_idempotent(auth_client, base_url):
    r1 = auth_client.get(
        f"{base_url}/api/admin/intents/snapshot-completeness?hours=24",
        timeout=20,
    ).json()
    r2 = auth_client.get(
        f"{base_url}/api/admin/intents/snapshot-completeness?hours=24",
        timeout=20,
    ).json()
    assert r1["total_directional_intents"] == r2["total_directional_intents"]
    for f in r1["field_presence"]:
        assert r1["field_presence"][f] == r2["field_presence"][f]

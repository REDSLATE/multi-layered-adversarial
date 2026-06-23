"""Tests for the seat-doctrinal Auto-Retire endpoint.

Doctrine pin: retirement is for (lane, seat, doctrine_version) — never
for a brain. These tests assert the scoring axes are seat-keyed and
holders appear only as metadata.

Uses sync `requests` via the `auth_client` fixture. Synthetic DB rows
are seeded/cleaned via pymongo (sync) so we don't conflict with
Motor's session loop.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

from pymongo import MongoClient

from tests.conftest import BASE_URL

DOCTRINE_SIDECARS = "doctrine_sidecars"


def _db():
    client = MongoClient(os.environ["MONGO_URL"])
    return client[os.environ["DB_NAME"]]


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _row(
    *, intent_id, lane, doctrine_version, quality,
    governor_action, adversary_challenge_required, execution_judge_ready,
    outcome_label,
    governor_holder="chevelle", adversary_holder="redeye",
    execution_judge_holder="alpha", strategist_holder="camaro",
):
    return {
        "intent_id": intent_id,
        "lane": lane,
        "doctrine_version": doctrine_version,
        "quality": quality,
        "stack": "alpha",
        "symbol": "TEST",
        "action": "BUY",
        "governor_action": governor_action,
        "adversary_challenge_required": adversary_challenge_required,
        "execution_judge_ready": execution_judge_ready,
        "governor_holder": governor_holder,
        "adversary_holder": adversary_holder,
        "execution_judge_holder": execution_judge_holder,
        "strategist_holder": strategist_holder,
        "ts": _now_iso(),
        "outcome_join": {
            "joined_at": _now_iso(),
            "outcome_label": outcome_label,
            "pnl_usd": 1.0 if outcome_label == "win" else -1.0,
        },
    }


def _seed(rows):
    _db()[DOCTRINE_SIDECARS].insert_many([dict(r) for r in rows])


def _cleanup(prefix):
    _db()[DOCTRINE_SIDECARS].delete_many(
        {"intent_id": {"$regex": f"^{prefix}"}},
    )


# ─── endpoint shape ─────────────────────────────────────────────────

def test_retirement_candidates_endpoint_shape(auth_client):
    r = auth_client.get(f"{BASE_URL}/api/admin/doctrine/retirement-candidates")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "candidates" in body
    assert isinstance(body["candidates"], list)
    assert "doctrine_note" in body
    assert "lane, seat, doctrine_version" in body["doctrine_note"]
    assert body["endpoint_version"] == "auto_retire_v1_seat_doctrinal"


def test_retirement_requires_auth(api_client):
    r = api_client.get(f"{BASE_URL}/api/admin/doctrine/retirement-candidates")
    assert r.status_code in (401, 403)


# ─── doctrinal correctness: seat-branch underperformance ────────────

def test_governor_block_underperformance_emits_candidate(auth_client):
    """governor.block should have HIGHER loss_rate than .modulate; if
    not, the block heuristic is noise → emit a retirement candidate
    keyed on (lane, seat, doctrine_version).

    2026-02-19: isolated to a unique doctrine_version per test run.
    The endpoint aggregates by `(lane, seat, doctrine_version)`, so a
    test using the shared `small_account_sidecar_v1` key would slice
    across thousands of prod rows and dilute the synthetic 30/30
    ratio into noise (the original failure mode — `comparator_loss_
    rate` came back as 0.57 instead of the expected 1.0 because the
    prod modulate rows have a real ~57% loss rate). Using a unique
    `_test_<prefix>` doctrine_version cuts a clean slice that contains
    ONLY the test's seeded rows.
    """
    prefix = f"art-gov-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    doctrine_v = f"small_account_sidecar_v1_test_{prefix}"
    rows = []
    for i in range(30):
        rows.append(_row(
            intent_id=f"{prefix}-b-{i}", lane="equity",
            doctrine_version=doctrine_v, quality="C_QUALITY",
            governor_action="block",
            adversary_challenge_required=False,
            execution_judge_ready=False,
            outcome_label="win",
        ))
    for i in range(30):
        rows.append(_row(
            intent_id=f"{prefix}-m-{i}", lane="equity",
            doctrine_version=doctrine_v, quality="B_QUALITY",
            governor_action="modulate",
            adversary_challenge_required=False,
            execution_judge_ready=True,
            outcome_label="loss",
        ))
    _seed(rows)
    try:
        r = auth_client.get(
            f"{BASE_URL}/api/admin/doctrine/retirement-candidates",
            params={"lane": "equity", "min_samples": 50},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        gov = next(
            (c for c in body["candidates"]
             if c["seat"] == "governor" and c["branch"] == "block"
             and c["doctrine_version"] == doctrine_v
             and c["lane"] == "equity"
             and set(c["occupancy_during_window"].keys()) == {"chevelle"}),
            None,
        )
        assert gov is not None, body
        assert gov["lane"] == "equity"
        assert gov["seat"] == "governor"
        assert gov["doctrine_version"] == doctrine_v
        assert gov["branch_loss_rate"] == 0.0
        assert gov["comparator_loss_rate"] == 1.0
        assert gov["severity"] == "BLAZING"
        assert "governor" in gov["headline"]
        assert "chevelle" not in gov["headline"].lower()
        assert isinstance(gov["occupancy_during_window"], dict)
    finally:
        _cleanup(prefix)


def test_execution_judge_ready_signal_is_quarantined(auth_client):
    """`execution_judge.ready` was demoted to ADVISORY-ONLY on
    2026-06-23 after the scorecard showed it was selecting WORSE
    outcomes than its inverse (ready_loss_rate=1.00 vs
    not_ready_loss_rate=0.37). Even when the data is maximally
    inverted, the auto-retire endpoint must NOT propose retiring
    the seat holder — the failure belongs to the heuristic, not
    the seat holder.

    Pre-quarantine, this test asserted the OPPOSITE (that the
    inversion produced a candidate). The flip is intentional and
    documents the doctrine change.
    """
    prefix = f"art-judge-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    doctrine_v = f"crypto_sidecar_v1_test_{prefix}"
    rows = []
    for i in range(30):
        rows.append(_row(
            intent_id=f"{prefix}-r-{i}", lane="crypto",
            doctrine_version=doctrine_v, quality="A_QUALITY",
            governor_action="modulate",
            adversary_challenge_required=False,
            execution_judge_ready=True,
            outcome_label="loss",
        ))
    for i in range(30):
        rows.append(_row(
            intent_id=f"{prefix}-n-{i}", lane="crypto",
            doctrine_version=doctrine_v, quality="B_QUALITY",
            governor_action="modulate",
            adversary_challenge_required=False,
            execution_judge_ready=False,
            outcome_label="win",
        ))
    _seed(rows)
    try:
        r = auth_client.get(
            f"{BASE_URL}/api/admin/doctrine/retirement-candidates",
            params={"lane": "crypto", "min_samples": 50},
        )
        body = r.json()
        cand = next(
            (c for c in body["candidates"]
             if c["seat"] == "execution_judge"
             and c["branch"] == "ready"
             and c["lane"] == "crypto"
             and c["doctrine_version"] == doctrine_v),
            None,
        )
        assert cand is None, (
            f"execution_judge.ready is QUARANTINED — auto-retire must "
            f"never produce candidates for this seat regardless of "
            f"data inversion. Got candidate: {cand!r}"
        )
    finally:
        _cleanup(prefix)


# ─── scorecard seat-doctrinal slicing ─────────────────────────────────

def test_scorecard_by_lane_seat_doctrine_present(auth_client):
    r = auth_client.get(f"{BASE_URL}/api/admin/doctrine/scorecard")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "by_lane_seat_doctrine" in body
    assert "seat_occupancy" in body
    assert body["scorecard_version"] == "scorecard_v2_seat_doctrinal"
    assert "(lane, seat, doctrine_version)" in body["scoring_axis_doctrine"]


def test_seat_occupancy_endpoint(auth_client):
    r = auth_client.get(f"{BASE_URL}/api/admin/doctrine/seat-occupancy")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "occupancy" in body
    assert isinstance(body["occupancy"], dict)

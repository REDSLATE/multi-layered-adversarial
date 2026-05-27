"""Tripwires for the 5-Shelly memory/reasoning pipeline (pass #13).

Doctrine pins locked by these tests:
    Shelly = memory + reasoning ONLY.
    Brain = decision authority.
    MC core = verifier / notary.
    RoadGuard = safety.

    Shelly CAN say: support / warn / neutral / seen_before
    Shelly CANNOT say: execute / block / override / promote /
                       approve / reject / kill / force

    Every artifact must carry `authority="memory_reasoning_only"`.
    Confidence delta bounded [-0.25, +0.10].

Implementation: SYNC pymongo. Shelly intentionally runs outside the
async hot path to keep tests + ops simple and to keep a Shelly hiccup
from blocking the live trading critical path.
"""
from __future__ import annotations

import os
import uuid

import pytest
import requests

from shelly.contracts import (
    AUTHORITY_MEMORY_REASONING_ONLY,
    RECOMMENDATIONS_ALLOWED,
    RECOMMENDATIONS_BANNED,
    ShellyMemoryEvent,
    ShellyReasoningReceipt,
    stable_hash,
)
from shelly.local_shelly import LocalShelly
from shelly.mc_shelly import MCShelly
from shelly.pipeline import shelly_pipeline
from shelly.sync_db import get_db


# ──────────────────────── contract pins ────────────────────────


@pytest.mark.tripwire
def test_authority_tag_is_pinned():
    assert AUTHORITY_MEMORY_REASONING_ONLY == "memory_reasoning_only"
    assert LocalShelly.doctrine_authority_tag() == AUTHORITY_MEMORY_REASONING_ONLY
    assert MCShelly.doctrine_authority_tag() == AUTHORITY_MEMORY_REASONING_ONLY


@pytest.mark.tripwire
def test_recommendation_vocabulary_pinned():
    assert RECOMMENDATIONS_ALLOWED == frozenset({
        "support", "warn", "neutral", "seen_before",
    })
    expected_banned = frozenset({
        "execute", "block", "override", "promote",
        "approve", "reject", "kill", "force",
    })
    assert RECOMMENDATIONS_BANNED == expected_banned
    assert RECOMMENDATIONS_BANNED.isdisjoint(RECOMMENDATIONS_ALLOWED)


@pytest.mark.tripwire
def test_memory_event_stamps_authority_tag():
    evt = ShellyMemoryEvent(
        brain="alpha", symbol="AAPL", direction="BUY",
        confidence=0.7, decision="EMIT",
    )
    doc = evt.to_doc()
    assert doc["authority"] == AUTHORITY_MEMORY_REASONING_ONLY
    assert "event_hash" in doc


@pytest.mark.tripwire
def test_memory_event_hash_excludes_created_at():
    """event_hash MUST hash only semantic content — `created_at` is
    excluded so the same event remembered twice dedupes correctly.

    This is the regression guard against the idempotent-upsert bug
    that surfaced in initial implementation (pass #13 follow-up):
    if `created_at` were in the hash, every replay would generate
    a fresh hash and the upsert wouldn't dedupe."""
    evt = ShellyMemoryEvent(
        brain="alpha", symbol="X", direction="BUY",
        confidence=0.5, decision="EMIT",
    )
    doc1 = evt.to_doc()
    # Force a different created_at by stamping a value explicitly.
    import dataclasses
    evt2 = dataclasses.replace(evt, created_at="2099-01-01T00:00:00+00:00")
    doc2 = evt2.to_doc()
    assert doc1["event_hash"] == doc2["event_hash"], (
        "event_hash must be stable across created_at variations"
    )
    assert doc1["created_at"] != doc2["created_at"]


@pytest.mark.tripwire
@pytest.mark.parametrize("banned", sorted(RECOMMENDATIONS_BANNED))
def test_receipt_rejects_banned_recommendation(banned):
    """Banned vocab is forbidden — the doctrine guardrail."""
    with pytest.raises(ValueError, match="BANNED"):
        ShellyReasoningReceipt(
            brain="alpha", symbol="X",
            recommendation=banned,
            confidence_delta=0.0,
        ).to_doc()


@pytest.mark.tripwire
def test_receipt_rejects_unknown_recommendation():
    with pytest.raises(ValueError, match="not in allowed vocabulary"):
        ShellyReasoningReceipt(
            brain="alpha", symbol="X",
            recommendation="probably_yes",
            confidence_delta=0.0,
        ).to_doc()


@pytest.mark.tripwire
@pytest.mark.parametrize("delta", [-0.30, -1.0, 0.15, 0.50, 99.0])
def test_receipt_rejects_out_of_bounds_confidence_delta(delta):
    with pytest.raises(ValueError, match="outside bounds"):
        ShellyReasoningReceipt(
            brain="alpha", symbol="X",
            recommendation="neutral",
            confidence_delta=delta,
        ).to_doc()


@pytest.mark.tripwire
def test_receipt_rejects_tampered_authority_field():
    with pytest.raises(ValueError, match="authority"):
        ShellyReasoningReceipt(
            brain="alpha", symbol="X",
            recommendation="neutral", confidence_delta=0.0,
            authority="executor",   # tampered
        ).to_doc()


# ──────────────────────── LocalShelly behavior ────────────────────────


@pytest.mark.tripwire
def test_local_shelly_remember_is_idempotent():
    """Same event_hash upserts to a single row."""
    db = get_db()
    brain = "alpha"
    sym = f"TW-{uuid.uuid4().hex[:8]}"
    local = LocalShelly(brain)

    evt = ShellyMemoryEvent(
        brain=brain, symbol=sym, direction="BUY",
        confidence=0.7, decision="EMIT",
    )
    doc1 = local.remember(evt)
    doc2 = local.remember(evt)
    try:
        assert doc1["event_hash"] == doc2["event_hash"]
        cnt = db[local.memories_coll_name].count_documents(
            {"event_hash": doc1["event_hash"]},
        )
        assert cnt == 1
    finally:
        db[local.memories_coll_name].delete_one(
            {"event_hash": doc1["event_hash"]},
        )


@pytest.mark.tripwire
def test_local_shelly_reasoning_neutral_on_empty_history():
    sym = f"TW-{uuid.uuid4().hex[:8]}"
    local = LocalShelly("alpha")
    rcpt = local.reason({"symbol": sym, "direction": "BUY"})
    assert rcpt["recommendation"] == "neutral"
    assert rcpt["confidence_delta"] == 0.0
    assert rcpt["authority"] == AUTHORITY_MEMORY_REASONING_ONLY


@pytest.mark.tripwire
def test_local_shelly_warns_on_high_loss_rate():
    db = get_db()
    sym = f"TW-{uuid.uuid4().hex[:8]}"
    local = LocalShelly("camaro")

    for i in range(6):
        evt = ShellyMemoryEvent(
            brain="camaro", symbol=sym, direction="BUY",
            confidence=0.6, decision="EMIT",
            outcome={"pnl_pct": -0.02, "case": i},
        )
        local.remember(evt)
    try:
        rcpt = local.reason({"symbol": sym, "direction": "BUY"})
        assert rcpt["recommendation"] == "warn"
        assert rcpt["confidence_delta"] == -0.15
        assert any("loss rate" in r for r in rcpt["reasons"])
    finally:
        db[local.memories_coll_name].delete_many({"symbol": sym})
        db[local.receipts_coll_name].delete_many({"symbol": sym})


@pytest.mark.tripwire
def test_local_shelly_rollup_state_machine():
    db = get_db()
    sym = f"TW-{uuid.uuid4().hex[:8]}"
    local = LocalShelly("chevelle")

    evt = ShellyMemoryEvent(
        brain="chevelle", symbol=sym, direction="BUY",
        confidence=0.6, decision="EMIT",
    )
    doc = local.remember(evt)
    try:
        unrolled = local.rollup_for_mc(limit=500)
        hashes = [m["event_hash"] for m in unrolled
                  if m["event_hash"] == doc["event_hash"]]
        assert hashes, "memory event must appear in unrolled batch"

        local.mark_rolled_to_mc([doc["event_hash"]])
        unrolled2 = local.rollup_for_mc(limit=500)
        hashes2 = [m["event_hash"] for m in unrolled2
                   if m["event_hash"] == doc["event_hash"]]
        assert not hashes2, "rolled-up event must not appear again"
    finally:
        db[local.memories_coll_name].delete_many({"symbol": sym})


# ──────────────────────── MCShelly behavior ────────────────────────


@pytest.mark.tripwire
def test_mc_shelly_dedupes_on_event_hash():
    db = get_db()
    sym = f"TW-{uuid.uuid4().hex[:8]}"
    mc = MCShelly()

    evt = ShellyMemoryEvent(
        brain="alpha", symbol=sym, direction="BUY",
        confidence=0.7, decision="EMIT",
    )
    doc = evt.to_doc()

    r1 = mc.ingest_rollup("alpha", [doc])
    r2 = mc.ingest_rollup("camaro", [doc])
    try:
        assert r1["inserted"] == 1
        assert r2["inserted"] == 0
        assert r2["duplicates"] == 1
        cnt = db[mc.SHARED_MEMORY_COLL].count_documents(
            {"event_hash": doc["event_hash"]},
        )
        assert cnt == 1
    finally:
        db[mc.SHARED_MEMORY_COLL].delete_many(
            {"event_hash": doc["event_hash"]},
        )


@pytest.mark.tripwire
def test_mc_shelly_authority_tag_stamped_on_ingest():
    """Tampered authority tag re-stamped at the boundary."""
    db = get_db()
    sym = f"TW-{uuid.uuid4().hex[:8]}"
    mc = MCShelly()

    bad = {
        "brain": "alpha", "symbol": sym, "direction": "BUY",
        "confidence": 0.5, "decision": "EMIT",
        "event_hash": stable_hash({"tag": sym}),
        "authority": "executor",        # tampered
    }
    mc.ingest_rollup("alpha", [bad])
    try:
        row = db[mc.SHARED_MEMORY_COLL].find_one(
            {"symbol": sym}, {"_id": 0},
        )
        assert row["authority"] == AUTHORITY_MEMORY_REASONING_ONLY
    finally:
        db[mc.SHARED_MEMORY_COLL].delete_many({"symbol": sym})


@pytest.mark.tripwire
def test_mc_shelly_brain_conflict_detection():
    """Two brains, opposite outcomes → `has_brain_conflict=True`."""
    db = get_db()
    sym = f"TW-{uuid.uuid4().hex[:8]}"
    mc = MCShelly()

    rolls = []
    for i in range(6):
        rolls.append(ShellyMemoryEvent(
            brain="alpha", symbol=sym, direction="BUY",
            confidence=0.6, decision="EMIT",
            outcome={"pnl_pct": 0.05, "i": i},
        ).to_doc())
    for i in range(6):
        rolls.append(ShellyMemoryEvent(
            brain="camaro", symbol=sym, direction="BUY",
            confidence=0.6, decision="EMIT",
            outcome={"pnl_pct": -0.05, "i": i + 1000},
        ).to_doc())
    for r in rolls:
        mc.ingest_rollup(r["brain"], [r])
    try:
        rcpt = mc.reason_across_shellys(
            {"symbol": sym, "direction": "BUY"},
        )
        assert rcpt["has_brain_conflict"] is True
        assert any("disagree" in r.lower() for r in rcpt["reasons"])
        assert rcpt["authority"] == AUTHORITY_MEMORY_REASONING_ONLY
    finally:
        db[mc.SHARED_MEMORY_COLL].delete_many({"symbol": sym})
        db[mc.RECEIPTS_COLL].delete_many({"symbol": sym})


# ──────────────────────── Pipeline integration ────────────────────────


@pytest.mark.tripwire
def test_pipeline_record_brain_event_known_brain():
    """End-to-end happy path."""
    db = get_db()
    sym = f"TW-{uuid.uuid4().hex[:8]}"

    result = shelly_pipeline.record_brain_event(
        brain="alpha",
        receipt={
            "symbol": sym, "direction": "BUY",
            "confidence": 0.7, "decision": "EMIT",
        },
    )
    try:
        assert result["ok"] is True
        assert result["brain"] == "alpha"
        assert result["authority"] == AUTHORITY_MEMORY_REASONING_ONLY
        assert result["local_memory"]["event_hash"]
        assert result["local_reasoning"]["recommendation"] in RECOMMENDATIONS_ALLOWED
        assert result["mc_reasoning"]["recommendation"] in RECOMMENDATIONS_ALLOWED
    finally:
        db["shelly_alpha_memories"].delete_many({"symbol": sym})
        db["shelly_alpha_reasoning_receipts"].delete_many({"symbol": sym})
        db["shelly_mc_reasoning_receipts"].delete_many({"symbol": sym})


@pytest.mark.tripwire
def test_pipeline_rejects_unknown_brain():
    result = shelly_pipeline.record_brain_event(
        brain="ghost", receipt={"symbol": "X", "direction": "BUY"},
    )
    assert result["ok"] is False
    assert result["reason"] == "UNKNOWN_BRAIN"
    assert result["authority"] == AUTHORITY_MEMORY_REASONING_ONLY


@pytest.mark.tripwire
def test_pipeline_auto_extends_with_live_runtimes():
    """The pipeline initializes one LocalShelly per LIVE_RUNTIMES.
    Six-brain refactor (SIX_BRAIN_REFACTOR_PLAN.md) must not require
    touching this file — assertion guards that contract."""
    from namespaces import LIVE_RUNTIMES
    assert set(shelly_pipeline.locals.keys()) == set(LIVE_RUNTIMES)


# ──────────────────────── Admin endpoints ────────────────────────


BASE_URL = (os.environ.get("REACT_APP_BACKEND_URL") or "").rstrip("/")
if not BASE_URL:
    with open("/app/frontend/.env") as f:
        for line in f:
            if line.startswith("REACT_APP_BACKEND_URL="):
                BASE_URL = line.split("=", 1)[1].strip().rstrip("/")
                break

ADMIN_EMAIL = os.environ.get("TEST_ADMIN_EMAIL", "admin@risedual.io")
ADMIN_PASSWORD = os.environ.get("TEST_ADMIN_PASSWORD", "risedual-admin-2026")


def _login() -> str:
    r = requests.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        timeout=30,
    )
    if r.status_code != 200:
        pytest.skip(f"login failed {r.status_code}: {r.text}")
    return r.json()["access_token"]


@pytest.mark.tripwire
def test_shelly_admin_endpoints_require_auth():
    for path, method in (
        ("/api/admin/shelly/status", requests.get),
        ("/api/admin/shelly/rollup", requests.post),
    ):
        r = method(f"{BASE_URL}{path}", timeout=15)
        assert r.status_code in (401, 403), (
            f"{path} must require auth; got {r.status_code}"
        )
    r = requests.post(
        f"{BASE_URL}/api/admin/shelly/reason", timeout=15,
        json={"symbol": "X", "direction": "BUY"},
    )
    assert r.status_code in (401, 403)


@pytest.mark.tripwire
def test_shelly_status_endpoint_canonical_shape():
    token = _login()
    r = requests.get(
        f"{BASE_URL}/api/admin/shelly/status",
        headers={"Authorization": f"Bearer {token}"}, timeout=30,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    for key in (
        "live_runtimes", "locals", "mc_shelly",
        "vocabulary", "authority", "doctrine_note",
    ):
        assert key in body
    assert body["authority"] == AUTHORITY_MEMORY_REASONING_ONLY
    assert set(body["vocabulary"]["allowed"]) == RECOMMENDATIONS_ALLOWED
    assert set(body["vocabulary"]["banned"]) == RECOMMENDATIONS_BANNED
    from namespaces import LIVE_RUNTIMES
    assert set(body["locals"].keys()) == set(LIVE_RUNTIMES)


@pytest.mark.tripwire
def test_shelly_rollup_endpoint_idempotent():
    token = _login()
    headers = {"Authorization": f"Bearer {token}"}
    r1 = requests.post(
        f"{BASE_URL}/api/admin/shelly/rollup", headers=headers, timeout=30,
    )
    assert r1.status_code == 200, r1.text
    r2 = requests.post(
        f"{BASE_URL}/api/admin/shelly/rollup", headers=headers, timeout=30,
    )
    assert r2.status_code == 200, r2.text
    assert r1.json()["authority"] == AUTHORITY_MEMORY_REASONING_ONLY


@pytest.mark.tripwire
def test_shelly_reason_probe_does_not_leak_authority_words():
    """The reason-probe response must never carry banned vocab."""
    token = _login()
    r = requests.post(
        f"{BASE_URL}/api/admin/shelly/reason",
        headers={"Authorization": f"Bearer {token}"},
        json={"symbol": "AAPL", "direction": "BUY"},
        timeout=20,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    rcpt = body["mc_reasoning"]
    assert rcpt["recommendation"] in RECOMMENDATIONS_ALLOWED
    assert rcpt["authority"] == AUTHORITY_MEMORY_REASONING_ONLY


# ──────────────────────── Coexistence ────────────────────────


@pytest.mark.tripwire
def test_new_shelly_collections_distinct_from_existing_mc_shelly():
    """5-Shelly collections must not collide with legacy `mc_shelly`."""
    new_collections = {
        f"shelly_{rt}_memories"
        for rt in shelly_pipeline.locals.keys()
    } | {
        f"shelly_{rt}_reasoning_receipts"
        for rt in shelly_pipeline.locals.keys()
    } | {
        "shelly_mc_shared_memory",
        "shelly_mc_reasoning_receipts",
    }
    legacy = {"mc_shelly"}
    assert new_collections.isdisjoint(legacy), (
        "5-Shelly collections must not overlap with the existing "
        f"`mc_shelly` collection. Overlap: {new_collections & legacy}"
    )

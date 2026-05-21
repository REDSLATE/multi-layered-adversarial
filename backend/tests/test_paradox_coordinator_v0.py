"""
Paradox Coordinator v0 — end-to-end tests.

Lock invariants (per user v0 spec):
  * Scan walks watchlist (or fallback), applies the FIVE filters,
    persists to `paradox_candidates`. NO trade intent produced.
  * Evaluate runs three kernel LLM calls (strategist / opponent /
    auditor), aggregates per the doctrine formula, writes a
    `paradox_records` row with kind `paradox_v0_evaluation`. HOLD
    cannot be promoted.
  * Risk check per-candidate sets `risk_blocked` and writes an
    audit row when any gate fails. Global pause only fires for
    kill_switch / broker_health / daily_loss.
  * Retrain check fires only on the three thresholds.
"""
from __future__ import annotations

import uuid

import pytest

from db import db
from namespaces import (
    PARADOX_CANDIDATES,
    PARADOX_RECORDS,
    PARADOX_RETRAIN_RECOMMENDATIONS,
    PARADOX_WATCHLIST,
)
from services.paradox_evaluator import (
    EVALUATION_KIND,
    PROMOTABLE_ACTIONS,
    _normalize_strategist,
    _normalize_opponent,
    _normalize_auditor,
    _parse_json_blob,
    aggregate_verdict,
)
from services.paradox_scanner import (
    FALLBACK_UNIVERSE,
    FILTER_PRICE_MIN,
    FILTER_RVOL_MIN,
    FILTER_SPREAD_BPS_MAX,
    FILTER_VOLUME_MIN,
    _apply_filters,
    run_scan,
)


# ─────────────── filter pinning tripwires ─────────────────────────────


@pytest.mark.tripwire
def test_filter_thresholds_pinned_exactly():
    """User v0 spec pins these. Changes are intentional + require
    updating this tripwire."""
    assert FILTER_PRICE_MIN == 2.0
    assert FILTER_VOLUME_MIN == 500_000
    assert FILTER_SPREAD_BPS_MAX == 75.0
    assert FILTER_RVOL_MIN == 1.5


@pytest.mark.tripwire
def test_promotable_actions_excludes_hold():
    """HOLD MUST NOT be promotable. Doctrine lock."""
    assert "HOLD" not in PROMOTABLE_ACTIONS
    assert set(PROMOTABLE_ACTIONS) == {"BUY", "SELL"}


@pytest.mark.tripwire
def test_fallback_universe_is_non_empty():
    assert len(FALLBACK_UNIVERSE) >= 5
    for e in FALLBACK_UNIVERSE:
        assert e["symbol"].isupper()
        assert e["lane"] in ("equity", "crypto")


# ─────────────── pure-function filter tests ───────────────────────────


def test_apply_filters_passes_clean_snapshot():
    v = _apply_filters({
        "price": 100, "volume": 1_000_000,
        "spread_bps": 20, "rvol": 2.5, "halted": False,
    })
    assert v["pass"] is True
    assert v["failures"] == []


def test_apply_filters_fails_on_low_price():
    v = _apply_filters({
        "price": 1.5, "volume": 1_000_000,
        "spread_bps": 20, "rvol": 2.0, "halted": False,
    })
    assert v["pass"] is False
    assert "price_below_min" in v["failures"]


def test_apply_filters_fails_on_halted():
    v = _apply_filters({
        "price": 100, "volume": 1_000_000,
        "spread_bps": 20, "rvol": 2.0, "halted": True,
    })
    assert v["pass"] is False
    assert "halted" in v["failures"]


def test_apply_filters_no_snapshot_is_pending():
    v = _apply_filters(None)
    assert v["pass"] is False
    assert v["reason"] == "pending_snapshot"


def test_apply_filters_stacks_multiple_failures():
    v = _apply_filters({
        "price": 1.0, "volume": 100, "spread_bps": 200, "rvol": 0.5,
        "halted": True,
    })
    assert v["pass"] is False
    # All five failure flags should fire
    assert "halted" in v["failures"]
    assert "price_below_min" in v["failures"]
    assert "volume_below_min" in v["failures"]
    assert "spread_above_max" in v["failures"]
    assert "rvol_below_min" in v["failures"]


# ─────────────── scan integration ─────────────────────────────────────


@pytest.mark.asyncio
async def test_run_scan_with_no_snapshots_marks_pending(monkeypatch):
    # Force a small universe so we don't pollute the candidates collection.
    override = [{"symbol": f"TEST{uuid.uuid4().hex[:4].upper()}", "lane": "equity"}]
    try:
        result = await run_scan(universe_override=override)
        assert result["summary"]["pending_snapshot"] == 1
        assert result["summary"]["candidates"] == 0
        # Persisted with pending_snapshot status.
        d = await db[PARADOX_CANDIDATES].find_one({"symbol": override[0]["symbol"]})
        assert d is not None
        assert d["status"] == "pending_snapshot"
    finally:
        await db[PARADOX_CANDIDATES].delete_many({"symbol": override[0]["symbol"]})


@pytest.mark.asyncio
async def test_run_scan_with_clean_snapshot_produces_candidate():
    sym = f"TST{uuid.uuid4().hex[:5].upper()}"
    override = [{"symbol": sym, "lane": "equity"}]
    snapshots = {sym: {"price": 50.0, "volume": 1_000_000,
                       "spread_bps": 30, "rvol": 2.0, "halted": False}}
    try:
        result = await run_scan(universe_override=override, snapshots=snapshots)
        assert result["summary"]["candidates"] == 1
        d = await db[PARADOX_CANDIDATES].find_one({"symbol": sym})
        assert d is not None
        assert d["status"] == "candidate"
        assert d["filter_pass"] is True
    finally:
        await db[PARADOX_CANDIDATES].delete_many({"symbol": sym})


@pytest.mark.asyncio
async def test_run_scan_filtered_out_not_persisted():
    sym = f"BAD{uuid.uuid4().hex[:5].upper()}"
    override = [{"symbol": sym, "lane": "equity"}]
    snapshots = {sym: {"price": 0.5, "volume": 100, "spread_bps": 500,
                       "rvol": 0.1, "halted": False}}
    try:
        result = await run_scan(universe_override=override, snapshots=snapshots)
        assert result["summary"]["filtered_out"] == 1
        # NOT persisted: keep the collection clean.
        d = await db[PARADOX_CANDIDATES].find_one({"symbol": sym})
        assert d is None
    finally:
        await db[PARADOX_CANDIDATES].delete_many({"symbol": sym})


# ─────────────── evaluator pure-function tests ────────────────────────


def test_parse_json_blob_handles_clean_json():
    out = _parse_json_blob('{"score": 0.8, "action": "BUY"}')
    assert out["score"] == 0.8
    assert out["action"] == "BUY"


def test_parse_json_blob_handles_embedded_json():
    out = _parse_json_blob('Here is my answer:\n{"score": 0.5, "action": "HOLD"}\nthanks')
    assert out["action"] == "HOLD"


def test_parse_json_blob_handles_garbage():
    assert _parse_json_blob("not json at all") is None
    assert _parse_json_blob("") is None
    assert _parse_json_blob(None) is None


def test_normalize_strategist_clips_score_and_defaults_to_hold():
    out = _normalize_strategist({"score": 99, "action": "INVALID"})
    assert out["score"] == 1.0
    assert out["action"] == "HOLD"
    assert out["parse_error"] is False


def test_normalize_strategist_marks_parse_error_on_none():
    out = _normalize_strategist(None)
    assert out["parse_error"] is True
    assert out["action"] == "HOLD"


def test_normalize_opponent_default():
    out = _normalize_opponent(None)
    assert out["veto"] is False
    assert out["parse_error"] is True


def test_normalize_auditor_clips_concerns_list():
    raw = {"score": 0.5, "concerns": [str(i) for i in range(50)]}
    out = _normalize_auditor(raw)
    assert len(out["concerns"]) == 10  # capped


# ─────────────── aggregator (the load-bearing one) ────────────────────


@pytest.mark.tripwire
def test_aggregate_uses_min_of_strategist_and_auditor():
    """Doctrine: final_conviction = min(strategist, auditor)."""
    v = aggregate_verdict(
        {"score": 0.9, "action": "BUY", "parse_error": False},
        {"veto": False, "parse_error": False},
        {"score": 0.3, "parse_error": False, "concerns": []},
    )
    assert v["final_conviction"] == 0.3


@pytest.mark.tripwire
def test_aggregate_opponent_veto_forces_hold():
    v = aggregate_verdict(
        {"score": 0.9, "action": "BUY", "parse_error": False},
        {"veto": True, "parse_error": False},
        {"score": 0.9, "parse_error": False, "concerns": []},
    )
    assert v["final_action"] == "HOLD"
    assert v["promotable"] is False
    assert v["status"] == "rejected"


@pytest.mark.tripwire
def test_aggregate_hold_is_never_promotable():
    """HOLD cannot be promoted, no matter the score."""
    v = aggregate_verdict(
        {"score": 0.9, "action": "HOLD", "parse_error": False},
        {"veto": False, "parse_error": False},
        {"score": 0.9, "parse_error": False, "concerns": []},
    )
    assert v["promotable"] is False
    assert v["status"] == "rejected"


def test_aggregate_clean_buy_is_promotable():
    v = aggregate_verdict(
        {"score": 0.75, "action": "BUY", "parse_error": False},
        {"veto": False, "parse_error": False},
        {"score": 0.80, "parse_error": False, "concerns": []},
    )
    assert v["final_action"] == "BUY"
    assert v["final_conviction"] == 0.75
    assert v["promotable"] is True
    assert v["status"] == "ready_for_human_review"


def test_aggregate_parse_error_rejects():
    v = aggregate_verdict(
        {"score": 0.9, "action": "BUY", "parse_error": True},
        {"veto": False, "parse_error": False},
        {"score": 0.9, "parse_error": False, "concerns": []},
    )
    assert v["promotable"] is False
    assert v["status"] == "rejected"


# ─────────────── HTTP-level integration ───────────────────────────────


def test_scan_requires_admin(base_url, api_client):
    r = api_client.post(f"{base_url}/api/admin/paradox/scan", json={}, timeout=15)
    assert r.status_code in (401, 403), r.text


def test_evaluate_requires_admin(base_url, api_client):
    r = api_client.post(
        f"{base_url}/api/admin/paradox/evaluate",
        json={"candidate_id": "x"}, timeout=15,
    )
    assert r.status_code in (401, 403), r.text


def test_risk_check_requires_admin(base_url, api_client):
    r = api_client.post(
        f"{base_url}/api/admin/risk/check", json={}, timeout=15,
    )
    assert r.status_code in (401, 403), r.text


def test_retrain_check_requires_admin(base_url, api_client):
    r = api_client.post(
        f"{base_url}/api/admin/ml/retrain/check", json={}, timeout=15,
    )
    assert r.status_code in (401, 403), r.text


def test_watchlist_requires_admin(base_url, api_client):
    r = api_client.get(f"{base_url}/api/admin/paradox/watchlist", timeout=15)
    assert r.status_code in (401, 403)


# ─────────────── Watchlist HTTP CRUD ──────────────────────────────────


@pytest.mark.asyncio
async def test_watchlist_add_list_toggle_delete(base_url, auth_client):
    sym = f"WL{uuid.uuid4().hex[:5].upper()}"
    try:
        # Add
        r = auth_client.post(
            f"{base_url}/api/admin/paradox/watchlist",
            json={"entries": [{"symbol": sym.lower(), "lane": "equity"}]},
            timeout=15,
        )
        assert r.status_code == 200, r.text
        # Symbol got uppercased.
        assert r.json()["items"][0]["symbol"] == sym

        # List shows it
        r = auth_client.get(
            f"{base_url}/api/admin/paradox/watchlist", timeout=15,
        )
        assert sym in [it["symbol"] for it in r.json()["items"]]

        # Toggle off
        r = auth_client.post(
            f"{base_url}/api/admin/paradox/watchlist/{sym}/toggle", timeout=15,
        )
        assert r.json()["active"] is False

        # active_only=true should now exclude it.
        r = auth_client.get(
            f"{base_url}/api/admin/paradox/watchlist?active_only=true", timeout=15,
        )
        assert sym not in [it["symbol"] for it in r.json()["items"]]

        # Delete
        r = auth_client.delete(
            f"{base_url}/api/admin/paradox/watchlist/{sym}", timeout=15,
        )
        assert r.status_code == 200
    finally:
        await db[PARADOX_WATCHLIST].delete_many({"symbol": sym})


def test_watchlist_rejects_bad_lane(base_url, auth_client):
    r = auth_client.post(
        f"{base_url}/api/admin/paradox/watchlist",
        json={"entries": [{"symbol": "FOO", "lane": "options"}]},
        timeout=15,
    )
    assert r.status_code == 400


def test_watchlist_delete_unknown_404(base_url, auth_client):
    r = auth_client.delete(
        f"{base_url}/api/admin/paradox/watchlist/NEVEREXISTED",
        timeout=15,
    )
    assert r.status_code == 404


# ─────────────── /paradox/scan HTTP path ──────────────────────────────


@pytest.mark.asyncio
async def test_scan_endpoint_persists_candidates(base_url, auth_client):
    sym = f"S{uuid.uuid4().hex[:5].upper()}"
    try:
        r = auth_client.post(
            f"{base_url}/api/admin/paradox/scan",
            json={
                "universe_override": [{"symbol": sym, "lane": "equity"}],
                "snapshots": {
                    sym: {"price": 50, "volume": 1_000_000,
                          "spread_bps": 20, "rvol": 2.0, "halted": False},
                },
            },
            timeout=15,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["summary"]["candidates"] == 1
        cid = body["candidates"][0]["candidate_id"]
        # Doctrine: scan does NOT post to /api/execution/submit. The
        # candidate is recorded as a candidate, NOT as a queued
        # intent. Confirm no shared_intents row was created with
        # this candidate's symbol via this scan.
        intent = await db.shared_intents.find_one(
            {"intent_id": cid},
        )
        assert intent is None
    finally:
        await db[PARADOX_CANDIDATES].delete_many({"symbol": sym})


# ─────────────── /paradox/evaluate HTTP path ──────────────────────────


@pytest.mark.asyncio
async def test_evaluate_404_on_unknown_candidate(base_url, auth_client):
    r = auth_client.post(
        f"{base_url}/api/admin/paradox/evaluate",
        json={"candidate_id": f"does-not-exist-{uuid.uuid4()}"},
        timeout=15,
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_evaluate_writes_paradox_record_and_stamps_candidate(monkeypatch):
    """Stub the kernel in-process and run the evaluator service
    directly (skips the HTTP layer because monkeypatch can't reach
    the server process)."""
    from services.paradox_evaluator import evaluate_candidate as _evaluate

    async def _fake_call(**kwargs):
        role = kwargs["role"]
        if role == "strategist":
            text = '{"score": 0.8, "action": "BUY", "rationale": "clean"}'
        elif role == "opponent":
            text = '{"veto": false, "rationale": "no veto"}'
        elif role == "auditor":
            text = '{"score": 0.7, "concerns": [], "rationale": "ok"}'
        else:
            text = "{}"
        return {
            "call_id": f"stub-{uuid.uuid4()}",
            "role": role,
            "task": kwargs.get("task"),
            "provider": "stub",
            "model": "stub",
            "response": text,
            "ok": True,
            "error": None,
            "usage": None,
            "session_id": kwargs.get("session_id"),
            "latency_ms": 0,
            "llm_authority": "ADVISORY_ONLY",
        }

    monkeypatch.setattr(
        "services.paradox_evaluator.llm_kernel.call",
        _fake_call,
    )

    sym = f"E{uuid.uuid4().hex[:5].upper()}"
    cid = str(uuid.uuid4())
    await db[PARADOX_CANDIDATES].insert_one({
        "candidate_id": cid,
        "symbol": sym,
        "lane": "equity",
        "source": "test",
        "status": "candidate",
        "reason": "test",
        "filter_pass": True,
        "filter_failures": [],
        "snapshot": {"price": 50, "volume": 1_000_000,
                     "spread_bps": 20, "rvol": 2.0, "halted": False},
        "created_at": "2026-05-21T00:00:00+00:00",
        "evaluated_at": None,
        "evaluation_id": None,
    })
    try:
        body = await _evaluate(candidate_id=cid)
        assert body["verdict"]["final_action"] == "BUY"
        assert body["verdict"]["promotable"] is True

        rec = await db[PARADOX_RECORDS].find_one({
            "evaluation_id": body["evaluation_id"],
        })
        assert rec is not None
        assert rec["evaluation_kind"] == EVALUATION_KIND
        assert rec["llm_authority"] == "ADVISORY_ONLY"

        cand = await db[PARADOX_CANDIDATES].find_one({"candidate_id": cid})
        assert cand["status"] == "evaluated"
        assert cand["evaluation_id"] == body["evaluation_id"]
    finally:
        await db[PARADOX_CANDIDATES].delete_many({"candidate_id": cid})
        await db[PARADOX_RECORDS].delete_many(
            {"candidate_id": cid, "evaluation_kind": EVALUATION_KIND},
        )


@pytest.mark.asyncio
async def test_evaluate_hold_path_via_opponent_veto(monkeypatch):
    """When opponent vetoes, evaluator stamps the candidate as
    evaluated but verdict.status == rejected (HOLD)."""
    from services.paradox_evaluator import evaluate_candidate as _evaluate

    async def _fake_call(**kwargs):
        role = kwargs["role"]
        if role == "strategist":
            text = '{"score": 0.9, "action": "BUY", "rationale": "bull"}'
        elif role == "opponent":
            text = '{"veto": true, "rationale": "earnings tomorrow"}'
        else:
            text = '{"score": 0.9, "concerns": [], "rationale": "clean"}'
        return {
            "call_id": f"stub-{uuid.uuid4()}",
            "response": text, "ok": True, "error": None,
            "role": role, "task": kwargs.get("task"),
            "provider": "stub", "model": "stub",
            "session_id": kwargs.get("session_id"),
            "latency_ms": 0, "usage": None,
            "llm_authority": "ADVISORY_ONLY",
        }

    monkeypatch.setattr(
        "services.paradox_evaluator.llm_kernel.call", _fake_call,
    )

    cid = str(uuid.uuid4())
    await db[PARADOX_CANDIDATES].insert_one({
        "candidate_id": cid,
        "symbol": "VETO",
        "lane": "equity",
        "source": "test",
        "status": "candidate",
        "filter_pass": True, "filter_failures": [],
        "snapshot": {"price": 50, "volume": 1_000_000,
                     "spread_bps": 20, "rvol": 2.0, "halted": False},
        "created_at": "2026-05-21T00:00:00+00:00",
        "evaluated_at": None, "evaluation_id": None,
    })
    try:
        body = await _evaluate(candidate_id=cid)
        assert body["verdict"]["final_action"] == "HOLD"
        assert body["verdict"]["promotable"] is False
        assert body["verdict"]["status"] == "rejected"
    finally:
        await db[PARADOX_CANDIDATES].delete_many({"candidate_id": cid})
        await db[PARADOX_RECORDS].delete_many(
            {"candidate_id": cid, "evaluation_kind": EVALUATION_KIND},
        )


# ─────────────── /paradox/risk/check HTTP path ────────────────────────


def test_risk_check_global_only(base_url, auth_client):
    """No candidate_id → return global state only."""
    r = auth_client.post(
        f"{base_url}/api/admin/risk/check", json={}, timeout=15,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "global" in body
    assert "global_triggers" in body["global"]


@pytest.mark.asyncio
async def test_risk_check_candidate_404(base_url, auth_client):
    r = auth_client.post(
        f"{base_url}/api/admin/risk/check",
        json={"candidate_id": f"missing-{uuid.uuid4()}"},
        timeout=15,
    )
    assert r.status_code == 404


# ─────────────── /paradox/ml/retrain/check HTTP path ──────────────────


def test_retrain_check_returns_stats(base_url, auth_client):
    r = auth_client.post(
        f"{base_url}/api/admin/ml/retrain/check", json={}, timeout=15,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "triggered" in body
    assert "triggers" in body
    assert "stats" in body
    assert "distillation_winners" in body["stats"]


@pytest.mark.asyncio
async def test_retrain_force_recommend_persists_row(base_url, auth_client):
    r = auth_client.post(
        f"{base_url}/api/admin/ml/retrain/check",
        json={"force_recommend": True},
        timeout=15,
    )
    assert r.status_code == 200
    body = r.json()
    rec = body.get("recommendation")
    assert rec is not None
    assert rec["recommended_target"] == "self_trained_advisory_head"
    # Cleanup
    await db[PARADOX_RETRAIN_RECOMMENDATIONS].delete_one({"rec_id": rec["rec_id"]})

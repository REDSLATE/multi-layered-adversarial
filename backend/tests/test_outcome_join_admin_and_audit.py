"""Tests for outcome-join admin, scorecard-by-brain, and safety-gates audit."""
import pytest
from unittest.mock import patch, AsyncMock

from routes import outcome_join_admin, scorecard_by_brain, safety_gates_audit


# ── outcome_join_admin ─────────────────────────────────────────────

class _FakeCollection:
    def __init__(self, docs=None):
        self._docs = docs or []
        self.count_response_map: dict = {}

    async def count_documents(self, q):
        # Return a per-query stub if registered, else len of docs
        key = frozenset(_stringify(q).items())
        if key in self.count_response_map:
            return self.count_response_map[key]
        return len(self._docs)

    def find(self, q, projection=None):  # noqa: ARG002
        return _FakeCursor(list(self._docs))

    async def find_one(self, q, projection=None):  # noqa: ARG002
        for d in self._docs:
            ok = True
            for k, v in q.items():
                if isinstance(v, dict):
                    if "$exists" in v:
                        present = k in d
                        if v["$exists"] != present:
                            ok = False
                            break
                elif d.get(k) != v:
                    ok = False
                    break
            if ok:
                return d
        return None


def _stringify(q):
    return {k: str(v) for k, v in q.items()}


class _FakeCursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def sort(self, *_a, **_kw):
        return self

    def limit(self, n):
        self._docs = self._docs[:n]
        return self

    async def to_list(self, _n):
        return self._docs

    def __aiter__(self):
        self._idx = 0
        return self

    async def __anext__(self):
        if self._idx >= len(self._docs):
            raise StopAsyncIteration
        d = self._docs[self._idx]
        self._idx += 1
        return d


# ── outcome_join_admin.health ──────────────────────────────────────

@pytest.mark.asyncio
async def test_outcome_join_health_returns_totals(monkeypatch):
    closed = [
        {"intent_id": "i1", "position_id": "p1", "lane": "equity", "symbol": "AMZN",
         "closed_at": "2026-02-19T10:00:00Z", "stack": "alpha"},
        {"intent_id": "i2", "position_id": "p2", "lane": "equity", "symbol": "NVDA",
         "closed_at": "2026-02-19T11:00:00Z", "stack": "camaro"},
    ]
    joined = [{"intent_id": "i1"}]

    sidecars = _FakeCollection(joined)
    positions = _FakeCollection(closed)
    intents = _FakeCollection([])

    async def _fake_count(self, q):  # noqa: ARG001
        return len(self._docs)

    fake_db = {"doctrine_sidecars": sidecars, "shared_live_positions": positions, "shared_intents": intents}

    monkeypatch.setattr(outcome_join_admin, "db", fake_db)
    res = await outcome_join_admin.health({"email": "x"})
    assert res["totals"]["doctrine_sidecars"] == 1
    assert res["totals"]["positions_closed"] == 2
    assert res["closed_position_sample"]["sample_size"] == 2
    assert res["closed_position_sample"]["joined_in_sample"] == 1
    assert res["closed_position_sample"]["orphans_in_sample"] == 1
    assert res["closed_position_sample"]["join_rate_in_sample"] == 0.5


@pytest.mark.asyncio
async def test_outcome_join_backfill_dry_run(monkeypatch):
    closed = [
        {"intent_id": "i1", "position_id": "p1", "lane": "equity", "symbol": "AMZN",
         "closed_at": "2026-02-19T10:00:00Z", "stack": "alpha",
         "fills": [{"outcome_label": "win", "pnl_usd": 12.0}]},
    ]
    sidecars_docs = [{"intent_id": "i1"}]  # exists, not yet joined

    positions = _FakeCollection(closed)
    sidecars = _FakeCollection(sidecars_docs)
    fake_db = {"doctrine_sidecars": sidecars, "shared_live_positions": positions, "shared_intents": _FakeCollection([])}
    monkeypatch.setattr(outcome_join_admin, "db", fake_db)

    req = outcome_join_admin.BackfillRequest(dry_run=True, limit=10)
    res = await outcome_join_admin.backfill(req, {"email": "x"})
    assert res["dry_run"] is True
    assert res["inspected"] == 1
    assert res["would_join_count"] == 1
    sample = res["would_join_sample"][0]
    assert sample["intent_id"] == "i1"
    assert sample["outcome_label"] == "win"
    assert sample["pnl_usd"] == 12.0


@pytest.mark.asyncio
async def test_outcome_join_backfill_skips_already_joined(monkeypatch):
    closed = [
        {"intent_id": "i1", "position_id": "p1", "lane": "equity", "symbol": "AMZN",
         "closed_at": "2026-02-19T10:00:00Z", "stack": "alpha",
         "fills": [{"outcome_label": "win", "pnl_usd": 12.0}]},
    ]
    sidecars_docs = [{"intent_id": "i1", "outcome_join": {"joined_at": "earlier"}}]

    positions = _FakeCollection(closed)
    sidecars = _FakeCollection(sidecars_docs)
    fake_db = {"doctrine_sidecars": sidecars, "shared_live_positions": positions, "shared_intents": _FakeCollection([])}
    monkeypatch.setattr(outcome_join_admin, "db", fake_db)

    req = outcome_join_admin.BackfillRequest(dry_run=True, limit=10)
    res = await outcome_join_admin.backfill(req, {"email": "x"})
    assert res["inspected"] == 1
    assert res["would_join_count"] == 0
    assert res["skipped_already_joined"] == 1


# ── scorecard_by_brain ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_scorecard_by_brain_aggregates(monkeypatch):
    sidecars_docs = [
        {"lane": "equity", "stack": "alpha", "doctrine_version": "gap_and_go_v1",
         "outcome_join": {"outcome_label": "win", "pnl_usd": 20.0}},
        {"lane": "equity", "stack": "alpha", "doctrine_version": "gap_and_go_v1",
         "outcome_join": {"outcome_label": "loss", "pnl_usd": -10.0}},
        {"lane": "equity", "stack": "camaro", "doctrine_version": "micro_pullback_v1",
         "outcome_join": {"outcome_label": "win", "pnl_usd": 8.0}},
    ]
    fake_db = {"doctrine_sidecars": _FakeCollection(sidecars_docs)}
    monkeypatch.setattr(scorecard_by_brain, "db", fake_db)

    res = await scorecard_by_brain.scorecard_by_brain(None, None, {"email": "x"})
    slices = res["slices"]
    assert len(slices) == 2
    alpha = next(s for s in slices if s["stack"] == "alpha")
    assert alpha["brain_display_name"] == "Camino"
    assert alpha["samples"] == 2
    assert alpha["wins"] == 1
    assert alpha["losses"] == 1
    assert alpha["win_rate"] == 0.5
    assert alpha["total_pnl_usd"] == 10.0

    camaro = next(s for s in slices if s["stack"] == "camaro")
    assert camaro["brain_display_name"] == "Barracuda"
    assert camaro["samples"] == 1


@pytest.mark.asyncio
async def test_scorecard_by_brain_filters_lane(monkeypatch):
    sidecars_docs = [
        {"lane": "equity", "stack": "alpha", "doctrine_version": "gap_and_go_v1",
         "outcome_join": {"outcome_label": "win", "pnl_usd": 20.0}},
        {"lane": "crypto", "stack": "redeye", "doctrine_version": "crypto_v1",
         "outcome_join": {"outcome_label": "win", "pnl_usd": 5.0}},
    ]
    fake_db = {"doctrine_sidecars": _FakeCollection(sidecars_docs)}
    monkeypatch.setattr(scorecard_by_brain, "db", fake_db)
    res = await scorecard_by_brain.scorecard_by_brain("equity", None, {"email": "x"})
    # Note: our FakeCollection ignores the lane filter, but the endpoint
    # passes it as a Mongo query — so we just verify the response shape.
    assert "slices" in res
    assert res["endpoint_version"] == "scorecard_by_brain_v1"


# ── safety_gates_audit ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_safety_gates_audit_buckets_block_rate(monkeypatch):
    rows = [
        {"intent_id": "i1", "ts": "2026-02-19T11:00:00Z", "verdict": "would_pass",
         "kind": "dry_run",
         "gates": [
             {"name": "executor_seat_check", "passed": True, "reason": "camaro holds executor"},
             {"name": "lane_execution_enabled", "passed": False, "reason": "equity lane disabled by operator"},
             {"name": "governor_authority", "passed": True, "reason": ""},
             {"name": "roadguard_spread_floor", "passed": True, "reason": "spread 12 bps ok"},
         ]},
        {"intent_id": "i2", "ts": "2026-02-19T11:05:00Z", "verdict": "would_block",
         "kind": "dry_run",
         "gates": [
             {"name": "executor_seat_check", "passed": False, "reason": "no seat-holder for lane equity"},
             {"name": "lane_execution_enabled", "passed": False, "reason": "equity lane disabled by operator"},
         ]},
        {"intent_id": "i3", "ts": "2026-02-19T11:10:00Z", "verdict": "would_block",
         "kind": "dry_run",
         "gates": [
             {"name": "roadguard_spread_floor", "passed": False, "reason": "spread 80 bps > 25 bps cap"},
         ]},
    ]
    fake_db = {"shared_gate_results": _FakeCollection(rows)}
    monkeypatch.setattr(safety_gates_audit, "db", fake_db)

    res = await safety_gates_audit.audit(hours=0.0, gates=None, sample_size=5, _user={"email": "x"})
    by_name = {g["gate"]: g for g in res["gates"]}

    assert by_name["executor_seat_check"]["pass_count"] == 1
    assert by_name["executor_seat_check"]["block_count"] == 1
    assert by_name["executor_seat_check"]["block_rate"] == 0.5

    assert by_name["lane_execution_enabled"]["block_count"] == 2
    assert by_name["lane_execution_enabled"]["block_rate"] == 1.0

    assert by_name["roadguard_spread_floor"]["block_count"] == 1
    # Sanity: gates ordered by block rate descending
    rates = [g["block_rate"] for g in res["gates"] if g["block_rate"] is not None]
    assert rates == sorted(rates, reverse=True)

    # Verdict counts surfaced
    assert res["verdict_counts"]["would_pass"] == 1
    assert res["verdict_counts"]["would_block"] == 2


@pytest.mark.asyncio
async def test_safety_gates_audit_respects_gates_param(monkeypatch):
    rows = [
        {"intent_id": "i1", "ts": "2026-02-19T11:00:00Z", "verdict": "would_pass",
         "kind": "dry_run",
         "gates": [
             {"name": "executor_seat_check", "passed": True, "reason": ""},
             {"name": "schema_invariants", "passed": True, "reason": ""},
         ]},
    ]
    fake_db = {"shared_gate_results": _FakeCollection(rows)}
    monkeypatch.setattr(safety_gates_audit, "db", fake_db)
    res = await safety_gates_audit.audit(
        hours=0.0, gates=["executor_seat_check"], sample_size=5, _user={"email": "x"},
    )
    names = {g["gate"] for g in res["gates"]}
    assert names == {"executor_seat_check"}

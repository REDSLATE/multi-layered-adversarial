"""Paradox-record writer — emergent-auditor artifact tests.

Locked behaviors:
  * One record per gate evaluation
  * `audit_status` follows OPPONENT_MODE: live→final, shadow→shadow,
    offline→unaudited
  * Verdict classification: all-passed + mult=1 → APPROVED;
    all-passed + mult<1 → DAMPENED; any-blocked → REJECTED
  * Anchored runtimes set correctly (executor=camaro, opponent=redeye)
"""
from __future__ import annotations

import os

import pytest

from shared.runtime.paradox_record import (
    _audit_status,
    _classify_verdict,
    _opponent_mode,
    _summarise_gates,
    write_paradox_record,
)


@pytest.fixture(autouse=True)
def _reset_opponent_mode():
    """Each test gets a clean OPPONENT_MODE env state."""
    prev = os.environ.get("OPPONENT_MODE")
    yield
    if prev is None:
        os.environ.pop("OPPONENT_MODE", None)
    else:
        os.environ["OPPONENT_MODE"] = prev


def test_audit_status_mapping():
    assert _audit_status("live") == "final"
    assert _audit_status("shadow_observation") == "shadow"
    assert _audit_status("offline") == "unaudited"
    assert _audit_status("garbage") == "unaudited"  # safe default


def test_opponent_mode_defaults_to_shadow():
    os.environ.pop("OPPONENT_MODE", None)
    assert _opponent_mode() == "shadow_observation"


def test_opponent_mode_clamps_unknown_to_offline():
    os.environ["OPPONENT_MODE"] = "bogus"
    assert _opponent_mode() == "offline"


def test_summarise_gates_handles_empty():
    s = _summarise_gates([])
    assert s["all_passed"] is False
    assert s["first_block"] is None
    assert s["gate_names"] == []


def test_summarise_gates_picks_first_block():
    s = _summarise_gates([
        {"name": "schema_invariants", "passed": True},
        {"name": "executor_seat_check", "passed": False, "reason": "vacant"},
        {"name": "broker_connected", "passed": True},
    ])
    assert s["all_passed"] is False
    assert s["first_block"] == {"name": "executor_seat_check", "reason": "vacant"}


def test_verdict_classification_approved():
    summary = {"all_passed": True}
    assert _classify_verdict(summary, 1.0) == "APPROVED"
    assert _classify_verdict(summary, None) == "APPROVED"


def test_verdict_classification_dampened():
    summary = {"all_passed": True}
    assert _classify_verdict(summary, 0.5) == "DAMPENED"


def test_verdict_classification_rejected():
    summary = {"all_passed": False}
    assert _classify_verdict(summary, 1.0) == "REJECTED"


# ───── End-to-end write ──────────────────────────────────────────────


class _FakeColl:
    def __init__(self):
        self.docs = []

    async def insert_one(self, d):
        self.docs.append(d)


class _FakeDB:
    def __init__(self):
        self.paradox_records = _FakeColl()

    def __getitem__(self, k):
        return getattr(self, k)


@pytest.mark.asyncio
async def test_write_paradox_record_approved_path(monkeypatch):
    """Clean gate chain + mult=1 + opponent shadow → APPROVED / shadow."""
    fake_db = _FakeDB()
    monkeypatch.setattr("shared.runtime.paradox_record.db", fake_db)
    os.environ["OPPONENT_MODE"] = "shadow_observation"

    rec = await write_paradox_record(
        intent={
            "intent_id": "i-1", "symbol": "BTC-USD",
            "direction": "BUY", "confidence": 0.7,
            "lane": "crypto", "stack": "camaro",
        },
        gates=[{"name": "g1", "passed": True}],
        risk_multiplier=1.0,
        evaluation_kind="dry_run",
        evaluated_by="admin@risedual.io",
    )
    assert rec["kernel_verdict"] == "APPROVED"
    assert rec["audit_status"] == "shadow"
    assert rec["executor_runtime"] == "camaro"
    assert rec["opponent_runtime"] == "redeye"
    assert rec["opponent_mode"] == "shadow_observation"
    assert rec["opponent_challenge"] is not None  # shadow still records
    assert len(fake_db.paradox_records.docs) == 1


@pytest.mark.asyncio
async def test_write_paradox_record_rejected_path(monkeypatch):
    """Any blocked gate → REJECTED. Audit_status still depends on mode."""
    fake_db = _FakeDB()
    monkeypatch.setattr("shared.runtime.paradox_record.db", fake_db)
    os.environ["OPPONENT_MODE"] = "live"

    rec = await write_paradox_record(
        intent={"intent_id": "i-2", "symbol": "AAPL", "direction": "BUY"},
        gates=[
            {"name": "schema", "passed": True},
            {"name": "executor_seat_check", "passed": False, "reason": "vacant"},
        ],
        risk_multiplier=1.0,
        evaluation_kind="submit_blocked",
    )
    assert rec["kernel_verdict"] == "REJECTED"
    assert rec["audit_status"] == "final"
    assert rec["gate_summary"]["first_block"]["name"] == "executor_seat_check"


@pytest.mark.asyncio
async def test_write_paradox_record_offline_marks_unaudited(monkeypatch):
    """When opponent is offline, the audit is `unaudited` and the
    opponent_challenge surface is None — operator must be aware."""
    fake_db = _FakeDB()
    monkeypatch.setattr("shared.runtime.paradox_record.db", fake_db)
    os.environ["OPPONENT_MODE"] = "offline"

    rec = await write_paradox_record(
        intent={"intent_id": "i-3", "symbol": "ETH-USD", "direction": "SELL"},
        gates=[{"name": "g", "passed": True}],
        risk_multiplier=0.7,
    )
    assert rec["audit_status"] == "unaudited"
    assert rec["opponent_challenge"] is None
    assert rec["kernel_verdict"] == "DAMPENED"


@pytest.mark.asyncio
async def test_writer_swallows_db_failure(monkeypatch):
    """The writer is best-effort — a DB failure must NEVER crash the
    live gate flow. It returns a stub instead."""
    class _BrokenColl:
        async def insert_one(self, d):
            raise RuntimeError("mongo down")

    class _BrokenDB:
        def __getitem__(self, k):
            return _BrokenColl()

    monkeypatch.setattr("shared.runtime.paradox_record.db", _BrokenDB())

    rec = await write_paradox_record(
        intent={"intent_id": "i-4"},
        gates=[{"name": "g", "passed": True}],
    )
    assert rec.get("ok") is False
    assert "mongo down" in rec.get("error", "")
    assert rec["intent_id"] == "i-4"


# ───── Tripwire surface ──────────────────────────────────────────────


@pytest.mark.tripwire
def test_audit_status_strings_are_locked():
    """`final` / `shadow` / `unaudited` are the operator's contract
    surface. UI code and dashboards grep for these exact strings."""
    assert _audit_status("live") == "final"
    assert _audit_status("shadow_observation") == "shadow"
    assert _audit_status("offline") == "unaudited"


@pytest.mark.tripwire
def test_verdict_labels_are_locked():
    """APPROVED / DAMPENED / REJECTED — the three legal verdicts.
    Adding a new one requires a code change to the UI + audit lake."""
    assert _classify_verdict({"all_passed": True}, 1.0) == "APPROVED"
    assert _classify_verdict({"all_passed": True}, 0.5) == "DAMPENED"
    assert _classify_verdict({"all_passed": False}, 1.0) == "REJECTED"

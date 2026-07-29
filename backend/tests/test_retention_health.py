"""Retention health sampler (2026-07-29) — catches silent pile-ups
(the mc_brain_silences 334k class) via O(1) count baselines."""
from __future__ import annotations

import sys

import pytest

sys.path.insert(0, "/app/backend")

from shared.retention_health import (
    SNAPSHOTS, compare_counts, evaluate, record_snapshot,
)


def test_compare_flags_unbounded_growth():
    baseline = {"mc_brain_silences": 10_000, "mc_pulses": 4_000}
    current = {"mc_brain_silences": 334_000, "mc_pulses": 4_100}
    flagged = compare_counts(current, baseline, factor=2.0, floor=5000)
    assert len(flagged) == 1
    assert flagged[0]["collection"] == "mc_brain_silences"
    assert flagged[0]["growth_x"] > 30


def test_compare_floor_ignores_tiny_collections():
    # 40 → 400 is 10× growth but far below the 5000-doc floor.
    flagged = compare_counts({"small": 400}, {"small": 40},
                             factor=2.0, floor=5000)
    assert flagged == []


def test_compare_stable_counts_pass():
    flagged = compare_counts({"a": 90_000}, {"a": 88_000},
                             factor=2.0, floor=5000)
    assert flagged == []


def test_compare_missing_baseline_skipped():
    flagged = compare_counts({"new_coll": 999_999}, {},
                             factor=2.0, floor=5000)
    assert flagged == []


async def test_snapshot_and_evaluate_roundtrip():
    from datetime import datetime
    from db import worker_db
    doc = await record_snapshot()
    assert doc and doc["counts"], "snapshot should sample RULES collections"
    stored = await worker_db[SNAPSHOTS].find_one({"ts": doc["ts"]})
    assert isinstance(stored.get("ttl_at"), datetime)  # BSON-Date TTL lesson
    verdict = await evaluate()
    assert verdict["status"] in {"pass", "warn"}
    assert verdict["collections_sampled"] > 20
    await worker_db[SNAPSHOTS].delete_many({"ts": doc["ts"]})

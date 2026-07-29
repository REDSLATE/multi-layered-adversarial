"""Tripwires for the retention-health monitor.

Doctrine pin (2026-07-29): `mc_brain_silences` reached 334k rows
because its TTL index keys on a STRING field, so Mongo's reaper
ignored every document — and nothing ever compared the collection's
size to its own past. These tests pin the three properties that make
the monitor able to catch that class of failure without becoming a
load problem itself:

  1. Every collection in `retention.RULES` appears in the evaluation
     (mirrors the retention-index sync tripwires — a rule added
     without coverage is a blind spot).
  2. The sampler counts with `estimated_document_count` (O(1)
     metadata) and NEVER `count_documents` (a real, possibly
     index-less scan on a saturated Atlas tier).
  3. A count above both the growth factor and the absolute floor
     produces a non-`pass` status.

Pure unit tests: no live backend, no Mongo. The Motor handles are
replaced with recording fakes.
"""
from __future__ import annotations

import inspect
import os
import sys
from datetime import datetime

import pytest

BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from routes import healthcheck_full  # noqa: E402
from shared import retention  # noqa: E402

pytestmark = pytest.mark.tripwire


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    async def to_list(self, length=None):
        return self._rows


class _FakeCollection:
    def __init__(self, name: str, count: int, recorder: dict):
        self.name = name
        self._count = count
        self._rec = recorder

    async def estimated_document_count(self, **kwargs):
        self._rec.setdefault("estimated", []).append(self.name)
        return self._count

    async def count_documents(self, *args, **kwargs):
        self._rec.setdefault("count_documents", []).append(self.name)
        raise AssertionError(
            "count_documents scans the collection — the sampler must use "
            "estimated_document_count",
        )

    async def insert_many(self, docs, ordered=True):
        self._rec.setdefault("inserted", []).extend(docs)

        class _Res:
            inserted_ids = [f"id-{i}" for i in range(len(docs))]

        return _Res()

    def aggregate(self, pipeline, **kwargs):
        self._rec.setdefault("aggregate", []).append(pipeline)
        return _FakeCursor(self._rec.get("baseline_rows", []))


class _FakeDB:
    """`worker_db` stand-in: every collection reports `count` docs."""

    def __init__(self, count: int = 10, recorder: dict | None = None):
        self.count = count
        self.recorder = recorder if recorder is not None else {}

    def __getitem__(self, name: str) -> _FakeCollection:
        return _FakeCollection(name, self.count, self.recorder)


@pytest.fixture()
def fake_worker_db(monkeypatch):
    fake = _FakeDB()
    monkeypatch.setattr(retention, "worker_db", fake)
    return fake


async def test_evaluate_covers_every_rule_collection(fake_worker_db):
    """Sync tripwire: adding a rule to `RULES` without it showing up
    in the health evaluation would re-create the silent blind spot."""
    report = await retention.evaluate_retention_health()
    covered = [r["collection"] for r in report["collections"]]
    expected = [coll for coll, _f, _d, _e in retention.RULES]
    assert covered == expected, (
        "evaluate_retention_health() must report EVERY collection in "
        f"RULES. Missing: {sorted(set(expected) - set(covered))}"
    )
    for row in report["collections"]:
        assert set(row) >= {
            "collection", "count", "baseline", "growth_ratio", "status",
        }
        assert row["status"] in ("pass", "warn", "fail")


async def test_sampler_uses_estimated_document_count(fake_worker_db):
    """`count_documents` on the fake raises — reaching it fails the
    test. Belt-and-braces source assertion catches a future edit that
    swaps the call inside a branch these fakes don't reach."""
    result = await retention.sample_retention_counts()

    assert fake_worker_db.recorder.get("estimated") == [
        coll for coll, _f, _d, _e in retention.RULES
    ]
    assert "count_documents" not in fake_worker_db.recorder
    assert result["collections_sampled"] == len(retention.RULES)
    assert result["snapshots_written"] == len(retention.RULES)

    code = inspect.getsource(retention._estimated_count).replace(
        retention._estimated_count.__doc__ or "", "",
    )
    assert "estimated_document_count" in code
    assert "count_documents" not in code


async def test_snapshots_stamp_bson_dates(fake_worker_db):
    """TTL requires a BSON Date. An isoformat string in `ttl_at` is
    exactly the bug that let 334k rows pile up under a live TTL."""
    await retention.sample_retention_counts()
    docs = fake_worker_db.recorder["inserted"]
    assert docs
    for doc in docs:
        assert isinstance(doc["ttl_at"], datetime), (
            f"ttl_at must be a datetime for the TTL index to reap it, "
            f"got {type(doc['ttl_at'])}"
        )
        assert isinstance(doc["ts"], datetime)


async def test_over_baseline_count_is_not_pass(monkeypatch):
    """A collection at 10× its baseline and far above the floor must
    surface as warn/fail, both per-collection and in the roll-up."""
    monkeypatch.setenv("RETENTION_HEALTH_MIN_COUNT", "1000")
    monkeypatch.setenv("RETENTION_HEALTH_GROWTH_FACTOR", "2.0")
    offender = retention.RULES[0][0]

    async def fake_counts():
        counts = {coll: 100 for coll, _f, _d, _e in retention.RULES}
        counts[offender] = 334_000
        return counts, {}

    async def fake_baselines(_cutoff):
        return {coll: 33_400 for coll, _f, _d, _e in retention.RULES}

    monkeypatch.setattr(retention, "_estimated_counts_for_rules", fake_counts)
    monkeypatch.setattr(retention, "_baselines", fake_baselines)

    report = await retention.evaluate_retention_health()
    rows = {r["collection"]: r for r in report["collections"]}
    assert rows[offender]["status"] != "pass"
    assert rows[offender]["growth_ratio"] == 10.0
    assert report["overall"] != "pass"
    assert report["offenders"] == [offender]

    # Every other collection sits below the 1000-doc floor, so its
    # 100-vs-33400 ratio must NOT trip the alarm.
    others = [r for c, r in rows.items() if c != offender]
    assert all(r["status"] == "pass" for r in others)


def test_growth_floor_gates_tiny_collections():
    status, ratio, _detail = retention.classify_growth(
        900, 10, growth_factor=2.0, fail_factor=4.0, min_count=1000,
    )
    assert status == "pass"
    assert ratio == 90.0

    status, _ratio, _detail = retention.classify_growth(
        None, 10, growth_factor=2.0, fail_factor=4.0, min_count=1000,
    )
    assert status == "warn", "an uncountable collection is not a pass"

    status, ratio, _detail = retention.classify_growth(
        5000, None, growth_factor=2.0, fail_factor=4.0, min_count=1000,
    )
    assert (status, ratio) == ("pass", None), "no baseline yet is not an alarm"


async def test_healthcheck_registers_retention_health(monkeypatch):
    """The check must participate in the fail>warn>pass roll-up of
    `/api/admin/healthcheck/full`, not just exist."""
    async def fake_eval():
        return {
            "growth_factor": 2.0,
            "min_count": 5000,
            "collections": [
                {"collection": "mc_brain_silences", "count": 334_000,
                 "baseline": 33_400, "growth_ratio": 10.0, "status": "fail"},
                {"collection": "mc_shelly", "count": 10, "baseline": 10,
                 "growth_ratio": 1.0, "status": "pass"},
            ],
        }

    monkeypatch.setattr(retention, "evaluate_retention_health", fake_eval)
    check = await healthcheck_full._check_retention_health()
    assert check["status"] == "fail"
    assert "elapsed_ms" in check and "detail" in check
    assert "mc_brain_silences" in check["detail"]

    src = inspect.getsource(healthcheck_full.healthcheck_full)
    assert 'checks["retention_health"] = await _check_retention_health()' in src

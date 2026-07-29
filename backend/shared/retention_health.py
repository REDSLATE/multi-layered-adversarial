"""Retention health sampler (2026-07-29 operator approval).

Catches the `mc_brain_silences` failure class: unbounded growth in a
collection that HAS a retention rule (dead TTL, dead sweep — rows
pile up silently, no error anywhere).

Mechanism: after every retention cycle, sample each RULES
collection's `estimated_document_count()` (O(1) metadata read — never
a scan) on the CAPPED worker pool into `retention_health_snapshots`
(BSON-Date ttl_at, 14d). Evaluation compares current counts against
the baseline snapshot ~24h back: growth beyond an env-tunable factor
AND absolute floor → WARN naming the collection.

Knobs: RETENTION_GROWTH_FACTOR (default 2.0),
RETENTION_GROWTH_FLOOR (default 5000 docs).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from db import worker_db
from shared.retention import RULES

logger = logging.getLogger("risedual.retention_health")

SNAPSHOTS = "retention_health_snapshots"
GROWTH_FACTOR = float(os.environ.get("RETENTION_GROWTH_FACTOR", "2.0"))
GROWTH_FLOOR = int(os.environ.get("RETENTION_GROWTH_FLOOR", "5000"))
_BASELINE_MIN_AGE_H = 20.0
_SNAPSHOT_TTL_DAYS = 14


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def sample_counts() -> dict[str, int]:
    """O(1) estimated counts for every RULES collection (worker pool)."""
    counts: dict[str, int] = {}
    for coll, _field, _is_date, _extra in RULES:
        try:
            counts[coll] = int(
                await worker_db[coll].estimated_document_count(
                    maxTimeMS=3000,
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("retention health count failed %s: %s", coll, exc)
    return counts


async def record_snapshot() -> Optional[dict]:
    """Called after each retention cycle. Fail-soft."""
    try:
        counts = await sample_counts()
        if not counts:
            return None
        doc = {
            "ts": _now().isoformat(),
            "counts": counts,
            "ttl_at": _now() + timedelta(days=_SNAPSHOT_TTL_DAYS),
        }
        await worker_db[SNAPSHOTS].insert_one(dict(doc))
        doc.pop("_id", None)
        return doc
    except Exception as exc:  # noqa: BLE001
        logger.warning("retention health snapshot failed: %s", exc)
        return None


def compare_counts(
    current: dict[str, int], baseline: dict[str, int],
    factor: float = GROWTH_FACTOR, floor: int = GROWTH_FLOOR,
) -> list[dict]:
    """Pure growth check: flag collections whose count grew past
    `factor`× baseline AND past the absolute `floor` (noise guard for
    tiny collections)."""
    flagged: list[dict] = []
    for coll, now_n in current.items():
        base_n = baseline.get(coll)
        if base_n is None or now_n < floor:
            continue
        if now_n > max(base_n, 1) * factor and now_n - base_n >= floor:
            flagged.append({
                "collection": coll,
                "baseline": base_n,
                "current": now_n,
                "growth_x": round(now_n / max(base_n, 1), 2),
            })
    return flagged


async def evaluate() -> dict:
    """Health verdict: current counts vs the ~24h-old baseline."""
    current = await sample_counts()
    cutoff = (_now() - timedelta(hours=_BASELINE_MIN_AGE_H)).isoformat()
    baseline_doc = await worker_db[SNAPSHOTS].find_one(
        {"ts": {"$lte": cutoff}}, {"_id": 0}, sort=[("ts", -1)],
    )
    if baseline_doc is None:  # first day of data — oldest we have
        baseline_doc = await worker_db[SNAPSHOTS].find_one(
            {}, {"_id": 0}, sort=[("ts", 1)],
        )
    flagged: list[dict] = []
    baseline_ts = None
    if baseline_doc and baseline_doc.get("counts"):
        baseline_ts = baseline_doc.get("ts")
        flagged = compare_counts(current, baseline_doc["counts"])
    return {
        "status": "warn" if flagged else "pass",
        "flagged": flagged,
        "baseline_ts": baseline_ts,
        "collections_sampled": len(current),
        "growth_factor": GROWTH_FACTOR,
        "growth_floor": GROWTH_FLOOR,
        "counts": current,
        "detail": (
            f"{len(flagged)} collection(s) growing despite retention "
            f"rules: {[f['collection'] for f in flagged]}"
            if flagged else
            f"{len(current)} retention collections stable vs baseline"
        ),
    }

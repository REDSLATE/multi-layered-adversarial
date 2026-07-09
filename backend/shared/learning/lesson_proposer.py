"""Lesson proposer — turns statistically-meaningful buckets into
doctrine-change proposals.

Doctrine (2026-07-09 iter-22, Stage 2, operator directive):

    Guardrails prevent early noise from becoming doctrine:
      * min_sample_size = 30 resolved experiences per bucket
      * Wilson lower bound ≥ 0.50 for EDGE lessons
      * avg_5m_bps < -10 for BLEED lessons

    Edge lesson (bucket has positive edge worth exploiting):
        samples >= 30 AND avg_5m_bps > 0 AND wilson_lower >= 0.50

    Bleed lesson (bucket is losing money — reduce exposure or block):
        samples >= 30 AND avg_5m_bps < -10

    Lessons ALWAYS land as state='proposed' — human Kernel review
    approves them; nothing self-applies to doctrine.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any

from shared.learning.bucket_analyzer import LEARNING_BUCKETS

logger = logging.getLogger("shared.learning.lesson_proposer")

LEARNING_LESSONS = "learning_lessons"

# Guardrail thresholds — operator directive; do not tune without
# Kernel review.
MIN_SAMPLE_SIZE = 30
WILSON_EDGE_FLOOR = 0.50
BLEED_BPS_THRESHOLD = -10.0


def _lesson_id(bucket_id: str, kind: str) -> str:
    """Deterministic lesson id — same bucket + same kind always
    dedupes to one lesson doc. Re-runs update in place instead of
    piling duplicates."""
    return hashlib.sha256(f"{bucket_id}|{kind}".encode()).hexdigest()[:16]


def _edge_proposal(bucket: dict) -> dict:
    """Build a proposed doctrine patch for an EDGE bucket. Kept
    intentionally coarse — the Kernel review reads the full evidence
    before approving; the proposer's job is only to flag WHICH bucket
    won and by how much."""
    dims = bucket.get("dims") or {}
    return {
        "kind": "raise_confidence_for_pattern",
        "target_pattern": dims,
        "suggested_action": (
            f"increase notional multiplier or risk cap for intents "
            f"matching {dims} — bucket showed edge over 30+ samples"
        ),
    }


def _bleed_proposal(bucket: dict) -> dict:
    """Build a proposed doctrine patch for a BLEED bucket."""
    dims = bucket.get("dims") or {}
    return {
        "kind": "reduce_exposure_for_pattern",
        "target_pattern": dims,
        "suggested_action": (
            f"downshift notional or block intents matching {dims} — "
            f"bucket is bleeding {bucket.get('avg_5m_bps'):.1f} bps/trade"
        ),
    }


async def propose_lessons(db) -> dict:
    """Scan `learning_buckets`, emit proposed lessons for any bucket
    that clears the guardrails.

    Idempotent: re-runs UPDATE the same lesson doc with fresh evidence
    — an operator who already approved a lesson keeps their approval;
    the `evidence` block just refreshes.

    Returns a counts dict.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    counts: dict[str, Any] = {
        "buckets_scanned": 0, "edge_lessons": 0, "bleed_lessons": 0,
        "skipped_undersample": 0, "skipped_noise": 0, "errors": 0,
    }

    try:
        cur = db[LEARNING_BUCKETS].find({}, {"_id": 1, "label": 1,
                                             "dims": 1, "samples": 1,
                                             "wins": 1, "losses": 1,
                                             "hit_rate": 1, "wilson_lower": 1,
                                             "avg_5m_bps": 1,
                                             "avg_15m_bps": 1,
                                             "avg_1h_bps": 1})
        buckets = [b async for b in cur]
    except Exception as exc:  # noqa: BLE001
        logger.warning("lesson_proposer: bucket scan failed: %s", exc)
        return counts

    for bucket in buckets:
        counts["buckets_scanned"] += 1
        samples = int(bucket.get("samples") or 0)

        # Guardrail 1: sample size.
        if samples < MIN_SAMPLE_SIZE:
            counts["skipped_undersample"] += 1
            continue

        avg_bps = bucket.get("avg_5m_bps") or 0.0
        wilson = bucket.get("wilson_lower") or 0.0

        proposed_lesson = None
        proposed_kind = None

        # Guardrail 2 + 3: edge lesson needs BOTH avg > 0 AND
        # Wilson-lower ≥ 0.50. Wilson-lower is the "am I sure this
        # isn't luck?" test.
        if avg_bps > 0 and wilson >= WILSON_EDGE_FLOOR:
            proposed_lesson = _edge_proposal(bucket)
            proposed_kind = "edge"
            counts["edge_lessons"] += 1

        # Guardrail 4: bleed lesson needs a real negative avg — a
        # bucket at -3 bps/trade is noise; a bucket at -15 is a leak.
        elif avg_bps < BLEED_BPS_THRESHOLD:
            proposed_lesson = _bleed_proposal(bucket)
            proposed_kind = "bleed"
            counts["bleed_lessons"] += 1
        else:
            counts["skipped_noise"] += 1
            continue

        lesson_id = _lesson_id(bucket["_id"], proposed_kind)
        lesson_doc = {
            "_id": lesson_id,
            "bucket_id": bucket["_id"],
            "bucket_label": bucket.get("label"),
            "kind": proposed_kind,
            "proposal": proposed_lesson,
            "evidence": {
                "samples": samples,
                "wins": bucket.get("wins"),
                "losses": bucket.get("losses"),
                "hit_rate": bucket.get("hit_rate"),
                "wilson_lower": wilson,
                "avg_5m_bps": avg_bps,
                "avg_15m_bps": bucket.get("avg_15m_bps"),
                "avg_1h_bps": bucket.get("avg_1h_bps"),
            },
            "state": "proposed",  # never auto-applied
            "proposed_at": now_iso,
        }
        try:
            # Upsert with $setOnInsert on state so previously-approved
            # or previously-rejected lessons don't get reset back to
            # 'proposed' when new evidence arrives.
            await db[LEARNING_LESSONS].update_one(
                {"_id": lesson_id},
                {
                    "$set": {
                        "bucket_id": lesson_doc["bucket_id"],
                        "bucket_label": lesson_doc["bucket_label"],
                        "kind": lesson_doc["kind"],
                        "proposal": lesson_doc["proposal"],
                        "evidence": lesson_doc["evidence"],
                        "updated_at": now_iso,
                    },
                    "$setOnInsert": {
                        "state": "proposed",
                        "proposed_at": now_iso,
                    },
                },
                upsert=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "lesson_proposer: write failed lesson_id=%s: %s",
                lesson_id, exc,
            )
            counts["errors"] += 1

    if counts["buckets_scanned"]:
        logger.info(
            "lesson_proposer: scanned=%d edge=%d bleed=%d "
            "undersample=%d noise=%d errors=%d",
            counts["buckets_scanned"], counts["edge_lessons"],
            counts["bleed_lessons"], counts["skipped_undersample"],
            counts["skipped_noise"], counts["errors"],
        )
    return counts

"""RISE AI Learning Loop scheduler — graded llm_calls → JSONL → checkpoint metrics.

Doctrine context (2026-02-25 — promoted from one-shot scaffolding to live worker):
    The 2026-06-02 bootstrap registered 8 seat-keyed ai_checkpoints at SHADOW
    state and created 8 empty JSONL files. The plan was: as Claude answered
    role-keyed prompts, the rows would be auto-graded, the harvester would
    dump positively-graded rows to JSONL, and a SHADOW corpus would
    accumulate so that when a trainer (OpenMythos eventually) came online,
    there would be data waiting.

    What actually shipped: the auto-grader and the harvester both existed,
    but were ONLY invoked via one-shot scripts (`scripts/run_auto_grader.py`,
    `scripts/rise_ai_bootstrap.py`). Nothing scheduled them. Six months later
    the JSONLs are still 0 bytes, 100% of llm_calls are ungraded, and 0 rows
    are training-eligible. RISE was in shadow STATE but never in shadow
    LEARNING.

    This module closes the loop. Two scheduled phases per tick:

      Phase 1 (auto-grader): pick up to N ungraded llm_calls in trainable
                             roles and grade them via the existing
                             `auto_grader.grade_batch` (which is itself a
                             Claude/anthropic call — the loop is "Claude
                             grading Claude," doctrine-checked).
      Phase 2 (harvester):   for each seat in SEATS, dump positively-graded
                             rows to /app/backend/datasets/rise_ai/{seat}.jsonl
                             via the existing `dataset_builder.build_training_jsonl`.
                             Updates the matching ai_checkpoints row's
                             metrics.rows_seeded + metrics.last_harvest_at.

    Both phases are kill-switched via env (`RISE_LEARNING_LOOP_ENABLED`).
    Default interval is 1 hour for the grader and 6 hours for the harvester
    — graders are cheap (one Claude call per row, capped at 50/cycle) and
    benefit from quick feedback; harvesters are bulk file rewrites and don't
    need to run more than a few times per day.

    No training is started by this module. No inference server is reached.
    No promotion happens. The loop's purpose is purely "make the corpus
    grow honestly from production traffic" so the deferred OpenMythos
    training run has real data when it lands.

    Authority: writes ai_checkpoints.metrics. Writes JSONL files. Calls
    Claude via auto_grader. Never touches the trading pipeline, never
    influences a brain decision, never short-circuits the LLM kernel.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from db import db
from shared.ai_autonomy.dataset_builder import build_training_jsonl
from shared.rise_ai.auto_grader import grade_batch

logger = logging.getLogger("risedual.rise_ai.learning_loop")

# 8 canonical seats from the 2026-02-17 refactor. Mirrors the SEATS
# tuple in scripts/rise_ai_bootstrap.py — kept in sync by hand because
# the bootstrap is itself a one-shot tool and shouldn't import from
# a long-running module.
SEATS = (
    "strategist",
    "auditor",
    "governor",
    "executor",
    "crypto_strategist",
    "crypto_auditor",
    "crypto_governor",
    "crypto",  # crypto_executor — kept "crypto" for legacy compat
)

DATASET_DIR = Path(os.environ.get("RISE_AI_DATASET_DIR", "/app/backend/datasets/rise_ai"))

# ── Kill switches and intervals (env-driven, fail-OFF on missing) ────
RISE_LEARNING_LOOP_ENABLED = os.environ.get(
    "RISE_LEARNING_LOOP_ENABLED", "true",
).lower() not in ("0", "false", "no", "off")

# Auto-grader: graded one row = one anthropic call. Cap rows per cycle
# to bound cost. 50 rows × 4 cycles/day = 200/day = manageable cost
# even with a heavy Claude price tier.
GRADER_INTERVAL_SECONDS = int(os.environ.get(
    "RISE_LEARNING_GRADER_INTERVAL_SECONDS", str(60 * 60),  # 1h
))
GRADER_BATCH_LIMIT = int(os.environ.get(
    "RISE_LEARNING_GRADER_BATCH_LIMIT", "50",
))

# Harvester: bulk JSONL rewrite. Each tick takes a few seconds for the
# full 8 seats. Doesn't need to be aggressive.
HARVESTER_INTERVAL_SECONDS = int(os.environ.get(
    "RISE_LEARNING_HARVESTER_INTERVAL_SECONDS", str(6 * 60 * 60),  # 6h
))

# 5-minute warmup so the rest of the boot sequence completes before
# the loop starts hitting Claude.
WARMUP_SECONDS = int(os.environ.get(
    "RISE_LEARNING_WARMUP_SECONDS", "300",
))


# ── Module-level task handles (lifespan owns these) ─────────────────
_GRADER_TASK: Optional[asyncio.Task] = None
_HARVESTER_TASK: Optional[asyncio.Task] = None

# Diagnostic state — exposed via status endpoint
_LAST_GRADER_RUN: dict[str, Any] = {
    "ran_at": None, "duration_ms": None, "summary": None, "error": None,
}
_LAST_HARVESTER_RUN: dict[str, Any] = {
    "ran_at": None, "duration_ms": None, "per_seat": None, "error": None,
}


# ─────────────────────── grader loop ────────────────────────────────


async def _grader_loop() -> None:
    """Periodically grade ungraded llm_calls via the existing
    auto_grader. Cost-bounded by GRADER_BATCH_LIMIT per cycle."""
    logger.info(
        "rise_ai grader started: interval=%ds limit=%d",
        GRADER_INTERVAL_SECONDS, GRADER_BATCH_LIMIT,
    )
    await asyncio.sleep(WARMUP_SECONDS)
    while True:
        started = time.monotonic()
        try:
            summary = await grade_batch(db, limit=GRADER_BATCH_LIMIT)
            _LAST_GRADER_RUN.update({
                "ran_at": datetime.now(timezone.utc).isoformat(),
                "duration_ms": int((time.monotonic() - started) * 1000),
                "summary": summary,
                "error": None,
            })
            counts = (summary or {}).get("counts") or summary or {}
            logger.info(
                "rise_ai grader tick: graded=%s g1=%s g0=%s errored=%s",
                counts.get("graded"), counts.get("g1"),
                counts.get("g0"), counts.get("errored"),
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            _LAST_GRADER_RUN.update({
                "ran_at": datetime.now(timezone.utc).isoformat(),
                "duration_ms": int((time.monotonic() - started) * 1000),
                "summary": None,
                "error": f"{type(e).__name__}: {str(e)[:300]}",
            })
            logger.exception("rise_ai grader tick failed: %s", e)
        await asyncio.sleep(GRADER_INTERVAL_SECONDS)


# ─────────────────────── harvester loop ─────────────────────────────


async def _harvest_one_seat(seat: str) -> dict[str, Any]:
    """Single-seat harvest + checkpoint-metrics update. Returns a
    per-seat receipt that the loop aggregates into _LAST_HARVESTER_RUN.

    `build_training_jsonl` overwrites the file each call — that's
    intentional. Every cycle re-dumps the entire positively-graded
    corpus, so the file is always a faithful snapshot of current
    truth. No append-with-dedup complexity needed.
    """
    out_path = str(DATASET_DIR / f"{seat}.jsonl")
    try:
        receipt = await build_training_jsonl(
            db, role=seat, output_path=out_path, min_grade=1,
        )
        # Update the matching ai_checkpoints row's metrics. Match by
        # role==seat (the post-2026-02-17 refactor pin) AND state in
        # {SHADOW, ADVISOR, PRIMARY} so we don't touch DEPRECATED
        # legacy rows.
        now_iso = datetime.now(timezone.utc).isoformat()
        await db.ai_checkpoints.update_one(
            {
                "role": seat,
                "state": {"$in": ["SHADOW", "ADVISOR", "PRIMARY"]},
            },
            {
                "$set": {
                    "metrics.rows_seeded": receipt["rows_written"],
                    "metrics.last_harvest_at": now_iso,
                    "metrics.last_harvest_source": "rise_learning_loop",
                    "updated_at": datetime.now(timezone.utc),
                },
            },
        )
        return {
            "seat": seat,
            "rows_written": receipt["rows_written"],
            "output_path": out_path,
            "ok": True,
        }
    except Exception as e:  # noqa: BLE001
        return {
            "seat": seat,
            "rows_written": None,
            "output_path": out_path,
            "ok": False,
            "error": f"{type(e).__name__}: {str(e)[:200]}",
        }


async def _harvester_loop() -> None:
    """Periodically rebuild JSONL corpora for all 8 seats from the
    graded llm_calls ledger. Mirrors the VRL scheduler pattern."""
    logger.info(
        "rise_ai harvester started: interval=%ds seats=%s",
        HARVESTER_INTERVAL_SECONDS, list(SEATS),
    )
    # Slightly longer warmup than the grader so the grader has at
    # least a chance to produce graded rows before we harvest.
    await asyncio.sleep(WARMUP_SECONDS + 60)
    while True:
        started = time.monotonic()
        DATASET_DIR.mkdir(parents=True, exist_ok=True)
        try:
            per_seat = []
            for seat in SEATS:
                per_seat.append(await _harvest_one_seat(seat))
            _LAST_HARVESTER_RUN.update({
                "ran_at": datetime.now(timezone.utc).isoformat(),
                "duration_ms": int((time.monotonic() - started) * 1000),
                "per_seat": per_seat,
                "error": None,
            })
            total_rows = sum(p.get("rows_written") or 0 for p in per_seat)
            logger.info(
                "rise_ai harvester tick: seats=%d total_rows=%d",
                len(per_seat), total_rows,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            _LAST_HARVESTER_RUN.update({
                "ran_at": datetime.now(timezone.utc).isoformat(),
                "duration_ms": int((time.monotonic() - started) * 1000),
                "per_seat": None,
                "error": f"{type(e).__name__}: {str(e)[:300]}",
            })
            logger.exception("rise_ai harvester tick failed: %s", e)
        await asyncio.sleep(HARVESTER_INTERVAL_SECONDS)


# ──────────────── lifespan-callable start / stop hooks ───────────────


def start_rise_learning_loop() -> None:
    """Idempotent. Called from server lifespan on boot."""
    global _GRADER_TASK, _HARVESTER_TASK
    if not RISE_LEARNING_LOOP_ENABLED:
        logger.info("rise_ai learning loop disabled (RISE_LEARNING_LOOP_ENABLED=false)")
        return
    try:
        if not _GRADER_TASK or _GRADER_TASK.done():
            _GRADER_TASK = asyncio.create_task(_grader_loop())
        if not _HARVESTER_TASK or _HARVESTER_TASK.done():
            _HARVESTER_TASK = asyncio.create_task(_harvester_loop())
    except RuntimeError:
        logger.warning("rise_ai learning loop could not start: no event loop")


async def stop_rise_learning_loop() -> None:
    """Lifespan shutdown hook — cancel both loops gracefully."""
    global _GRADER_TASK, _HARVESTER_TASK
    for task in (_GRADER_TASK, _HARVESTER_TASK):
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
    _GRADER_TASK = None
    _HARVESTER_TASK = None


def status() -> dict[str, Any]:
    """Diagnostic snapshot for the admin status endpoint."""
    return {
        "enabled": RISE_LEARNING_LOOP_ENABLED,
        "grader": {
            "running": _GRADER_TASK is not None and not _GRADER_TASK.done(),
            "interval_seconds": GRADER_INTERVAL_SECONDS,
            "batch_limit": GRADER_BATCH_LIMIT,
            "last_run": dict(_LAST_GRADER_RUN),
        },
        "harvester": {
            "running": _HARVESTER_TASK is not None and not _HARVESTER_TASK.done(),
            "interval_seconds": HARVESTER_INTERVAL_SECONDS,
            "seats": list(SEATS),
            "last_run": dict(_LAST_HARVESTER_RUN),
        },
        "dataset_dir": str(DATASET_DIR),
    }


__all__ = [
    "SEATS",
    "start_rise_learning_loop",
    "stop_rise_learning_loop",
    "status",
]

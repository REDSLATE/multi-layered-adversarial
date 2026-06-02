"""Bootstrap RISE AI per-SEAT checkpoints.

2026-02-17 refactor: keyed by SEAT (not brain). The 8-seat IP makes
seats the unit of authority and promotion, so training and checkpoint
identity live on the seat. A brain rotation no longer breaks training
continuity — the auditor model stays the auditor model regardless of
which brain currently holds that seat.

For each canonical seat:
  1. Build a JSONL training corpus from `llm_calls` filtered by role.
  2. Register a SHADOW-state checkpoint with the canonical model_id.

Legacy migration: any pre-refactor brain-keyed checkpoints
(`rise-ai-{alpha|camaro|chevelle|redeye}-...`) get marked
`state=DEPRECATED` on first run.

Usage:
    cd /app/backend
    python -m scripts.rise_ai_bootstrap

Authority: writes one JSONL per seat + one ai_checkpoints row per
seat at state=SHADOW. Never promotes — promotion is operator action
via `set_checkpoint_state` after evaluation.
"""
import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path

from db import db
from shared.ai_autonomy import register_checkpoint
from shared.ai_autonomy.dataset_builder import build_training_jsonl
from shared.rise_ai import RISE_AI_ROLE_PROFILES


# Canonical 8 seats. Iteration order matches the IP refresh.
SEATS = (
    "strategist",
    "auditor",
    "governor",
    "executor",
    "crypto_strategist",
    "crypto_auditor",
    "crypto_governor",
    "crypto",
)

# Legacy brain-keyed model_ids from before the seat refactor. Marked
# DEPRECATED on first run so they fall out of any priority walk.
LEGACY_BRAIN_MODEL_IDS = (
    "rise-ai-alpha-qwen3-8b-v1",
    "rise-ai-camaro-qwen3-8b-v1",
    "rise-ai-chevelle-qwen3-8b-v1",
    "rise-ai-redeye-qwen3-8b-v1",
)

DATASET_DIR = Path(
    os.environ.get("RISE_AI_DATASET_DIR", "/app/backend/datasets/rise_ai")
)


async def _ensure_checkpoint_unique(model_id: str) -> bool:
    """Return True if no existing ai_checkpoints row for this model_id."""
    existing = await db.ai_checkpoints.find_one(
        {"model_id": model_id}, {"_id": 0, "model_id": 1, "state": 1}
    )
    return existing is None


async def _deprecate_legacy_brain_checkpoints() -> int:
    """Mark any pre-refactor brain-keyed checkpoint rows as DEPRECATED
    so they don't compete with the new seat-keyed checkpoints. Returns
    the number of rows updated. Idempotent."""
    res = await db.ai_checkpoints.update_many(
        {
            "model_id": {"$in": list(LEGACY_BRAIN_MODEL_IDS)},
            "state": {"$ne": "DEPRECATED"},
        },
        {
            "$set": {
                "state": "DEPRECATED",
                "state_reason": "Superseded by seat-keyed checkpoints (2026-02-17 refactor)",
                "updated_at": datetime.now(timezone.utc),
            }
        },
    )
    return res.modified_count


async def main():
    DATASET_DIR.mkdir(parents=True, exist_ok=True)
    print(f"datasets dir: {DATASET_DIR}")

    # 0) Deprecate legacy brain-keyed checkpoints (if any exist).
    n_deprecated = await _deprecate_legacy_brain_checkpoints()
    if n_deprecated:
        print(f"deprecated {n_deprecated} legacy brain-keyed checkpoint(s)")

    summaries = []
    for seat in SEATS:
        profile = RISE_AI_ROLE_PROFILES[seat]
        model_id = profile["model_id"]
        out_path = str(DATASET_DIR / f"{seat}.jsonl")

        # 1) Build the dataset
        receipt = await build_training_jsonl(
            db, role=seat, output_path=out_path, min_grade=1
        )
        print(f"[{seat:18s}] dataset → {receipt['rows_written']:5d} rows @ {out_path}")

        # 2) Register checkpoint at SHADOW state (idempotent by model_id)
        is_fresh = await _ensure_checkpoint_unique(model_id)
        if is_fresh:
            await register_checkpoint(
                db=db,
                role=seat,
                model_id=model_id,
                base_model="qwen3-8b",
                dataset_path=out_path,
                metrics={"stage": "initial", "rows_seeded": receipt["rows_written"]},
            )
            print(f"[{seat:18s}] checkpoint registered: {model_id} state=SHADOW")
        else:
            print(f"[{seat:18s}] checkpoint already exists: {model_id} (skipped)")

        summaries.append({
            "seat": seat,
            "model_id": model_id,
            "dataset_rows": receipt["rows_written"],
            "checkpoint_created": is_fresh,
        })

    print()
    print("=" * 70)
    print("RISE AI bootstrap complete (seat-keyed)")
    print("=" * 70)
    for s in summaries:
        print(
            f"  {s['seat']:18s} {s['model_id']:42s} "
            f"rows={s['dataset_rows']:6d} new={s['checkpoint_created']}"
        )


if __name__ == "__main__":
    asyncio.run(main())

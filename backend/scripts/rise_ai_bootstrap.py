"""Bootstrap RISE AI per-brain checkpoints.

For each of the four brains:
  1. Build a JSONL training corpus from `llm_calls` filtered by role.
  2. Register a SHADOW-state checkpoint with the canonical model_id.

This is a one-shot operator action — run once per fresh deploy that
wants per-brain checkpoint rows in `ai_checkpoints`. Re-running is
safe: dataset is rewritten, checkpoint insert is dedup-keyed by
model_id (an existing row stays).

Usage:
    cd /app/backend
    python -m scripts.rise_ai_bootstrap

Authority: writes one JSONL per brain + one ai_checkpoints row per
brain at state=SHADOW. Never promotes — promotion is operator action
via `set_checkpoint_state` after evaluation.
"""
import asyncio
import os
from pathlib import Path

from db import db
from shared.ai_autonomy import register_checkpoint
from shared.ai_autonomy.dataset_builder import build_training_jsonl
from shared.rise_ai import RISE_AI_ROLE_PROFILES


BRAINS = ("alpha", "camaro", "chevelle", "redeye")

# Datasets live next to the backend by default. Operator can override
# via RISE_AI_DATASET_DIR.
DATASET_DIR = Path(
    os.environ.get("RISE_AI_DATASET_DIR", "/app/backend/datasets/rise_ai")
)


async def _ensure_checkpoint_unique(model_id: str) -> bool:
    """Return True if no existing ai_checkpoints row for this model_id."""
    existing = await db.ai_checkpoints.find_one(
        {"model_id": model_id}, {"_id": 0, "model_id": 1, "state": 1}
    )
    return existing is None


async def main():
    DATASET_DIR.mkdir(parents=True, exist_ok=True)
    print(f"datasets dir: {DATASET_DIR}")

    summaries = []
    for brain in BRAINS:
        profile = RISE_AI_ROLE_PROFILES[brain]
        model_id = profile["model_id"]
        out_path = str(DATASET_DIR / f"{brain}.jsonl")

        # 1) Build the dataset
        receipt = await build_training_jsonl(
            db, role=brain, output_path=out_path, min_grade=1
        )
        print(f"[{brain}] dataset → {receipt['rows_written']} rows @ {out_path}")

        # 2) Register checkpoint at SHADOW state (idempotent by model_id)
        is_fresh = await _ensure_checkpoint_unique(model_id)
        if is_fresh:
            doc = await register_checkpoint(
                db=db,
                role=brain,
                model_id=model_id,
                base_model="qwen3-8b",
                dataset_path=out_path,
                metrics={"stage": "initial", "rows_seeded": receipt["rows_written"]},
            )
            print(f"[{brain}] checkpoint registered: {doc['model_id']} state=SHADOW")
        else:
            print(f"[{brain}] checkpoint already exists: {model_id} (skipped)")

        summaries.append({
            "brain": brain,
            "model_id": model_id,
            "dataset_rows": receipt["rows_written"],
            "checkpoint_created": is_fresh,
        })

    print()
    print("=" * 60)
    print("RISE AI bootstrap complete")
    print("=" * 60)
    for s in summaries:
        print(
            f"  {s['brain']:9s} {s['model_id']:35s} "
            f"rows={s['dataset_rows']:6d} new={s['checkpoint_created']}"
        )


if __name__ == "__main__":
    asyncio.run(main())

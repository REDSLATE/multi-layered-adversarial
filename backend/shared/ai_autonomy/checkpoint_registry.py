"""Checkpoint registry — `ai_checkpoints` collection helpers.

One row per trained candidate model. Lifecycle:
    OFFLINE → SHADOW → ADVISOR → PRIMARY (or ROLLBACK from any state).

Authority:
    `register_checkpoint`     — inserts a SHADOW row. ADVISORY_ONLY.
    `set_checkpoint_state`    — operator promotes/rolls back. The
                                routing kernel reads this collection
                                via `llm_provider_state` to decide
                                whether a local model is allowed
                                anywhere near a real role.

Timestamps are UTC-aware (`datetime.now(timezone.utc)`) per env doctrine.
"""
from datetime import datetime, timezone


async def register_checkpoint(
    db,
    role: str,
    model_id: str,
    base_model: str,
    dataset_path: str,
    metrics: dict,
):
    """Insert a new candidate checkpoint at SHADOW state."""
    doc = {
        "role": role,
        "model_id": model_id,
        "base_model": base_model,
        "dataset_path": dataset_path,
        "metrics": metrics or {},
        "state": "SHADOW",
        "created_at": datetime.now(timezone.utc),
        "authority": "ADVISORY_ONLY",
    }
    await db.ai_checkpoints.insert_one(doc)
    # Don't return the input dict (motor mutates it with _id).
    return {
        "role": role,
        "model_id": model_id,
        "base_model": base_model,
        "dataset_path": dataset_path,
        "metrics": metrics or {},
        "state": "SHADOW",
        "authority": "ADVISORY_ONLY",
    }


async def set_checkpoint_state(db, model_id: str, state: str, reason: str):
    """Operator transitions a checkpoint between states."""
    await db.ai_checkpoints.update_one(
        {"model_id": model_id},
        {
            "$set": {
                "state": state,
                "state_reason": reason,
                "updated_at": datetime.now(timezone.utc),
            }
        },
    )
    return {
        "model_id": model_id,
        "state": state,
        "reason": reason,
        "authority": "STATE_CHANGE_ONLY",
    }

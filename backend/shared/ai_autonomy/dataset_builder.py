"""Dataset builder — graded `llm_calls` → JSONL training corpus.

Reads positively-graded rows out of the live ledger and writes one
JSON-per-line file for downstream supervised fine-tuning. Filters:

  * role match
  * grade >= min_grade (default 1; +1 = operator/auditor-positive)
  * non-empty prompt + response (skip failed calls)

Authority: read-only on llm_calls. Writes one local file. Never touches
execution, never mutates ledger rows. The corpus is consumed by an
external training run (not orchestrated here).
"""
import json
from datetime import datetime, timezone
from pathlib import Path


async def build_training_jsonl(db, role: str, output_path: str, min_grade: int = 1):
    """Convert graded llm_calls into a JSONL training corpus.

    Only positively graded calls become training examples. Mongo `_id`
    is never included in the output (BSON-not-JSON pin).

    Returns a manifest dict the caller can persist alongside the dataset:
        {role, output_path, rows_written, authority: "DATASET_ONLY"}
    """
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    cursor = db.llm_calls.find({
        "role": role,
        "grade": {"$gte": min_grade},
        "response": {"$exists": True, "$ne": ""},
        "prompt": {"$exists": True, "$ne": ""},
    }, {"_id": 0}).sort("created_at", -1)

    count = 0
    with open(output_path, "w", encoding="utf-8") as f:
        async for row in cursor:
            created_at = row.get("created_at")
            if hasattr(created_at, "isoformat"):
                created_at = created_at.isoformat()
            elif created_at is None:
                created_at = datetime.now(timezone.utc).isoformat()

            item = {
                "role": role,
                "task": row.get("task"),
                "prompt": row.get("prompt"),
                "response": row.get("response"),
                "grade": row.get("grade"),
                "provider": row.get("provider"),
                "model": row.get("model"),
                "call_id": row.get("call_id"),
                "created_at": created_at,
            }
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
            count += 1

    return {
        "role": role,
        "output_path": output_path,
        "rows_written": count,
        "authority": "DATASET_ONLY",
    }

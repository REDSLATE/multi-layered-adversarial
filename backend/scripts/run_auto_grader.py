"""Operator one-shot: grade the next batch of ungraded llm_calls rows.

Usage:
    cd /app/backend
    python -m scripts.run_auto_grader            # default limit=50
    AUTO_GRADER_LIMIT=200 python -m scripts.run_auto_grader

This is the cron-friendly entry point. Re-runnable, idempotent (rows
that already have a grade are skipped). Bounded by `limit` so each
invocation has a known cost ceiling.
"""
import asyncio
import json
import os

from db import db
from shared.rise_ai.auto_grader import grade_batch


async def main():
    limit = int(os.environ.get("AUTO_GRADER_LIMIT", "50"))
    summary = await grade_batch(db, limit=limit)
    print(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())

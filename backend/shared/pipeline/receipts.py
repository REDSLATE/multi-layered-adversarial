"""PipelineReceipt persistence + lookup.

Single collection: `pipeline_receipts`. One row per intent run through
the unified pipeline. Indexed on `intent_id` for the /why endpoint.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from db import db

from .models import PipelineReceipt


PIPELINE_RECEIPTS_COLL = "pipeline_receipts"


class ReceiptStore:
    """Async Mongo writer. Stateless; safe to construct per request."""

    async def write(self, receipt: PipelineReceipt) -> None:
        doc = asdict(receipt)
        if not doc.get("ts"):
            doc["ts"] = datetime.now(timezone.utc).isoformat()
        await db[PIPELINE_RECEIPTS_COLL].update_one(
            {"intent_id": receipt.intent_id},
            {"$set": doc},
            upsert=True,
        )

    async def find_by_intent(self, intent_id: str) -> Optional[Dict[str, Any]]:
        return await db[PIPELINE_RECEIPTS_COLL].find_one(
            {"intent_id": intent_id}, {"_id": 0},
        )


async def ensure_indexes() -> None:
    """Idempotent index creation. Called once at boot."""
    await db[PIPELINE_RECEIPTS_COLL].create_index("intent_id", unique=True)
    await db[PIPELINE_RECEIPTS_COLL].create_index("ts")
    await db[PIPELINE_RECEIPTS_COLL].create_index("restriction_source")

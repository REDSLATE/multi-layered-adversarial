"""Memory Labeling Firewall.
All runtimes write through this. Provides isolation tags so labels can be filtered per runtime,
but the firewall logic is shared (label-then-store)."""
import uuid
from datetime import datetime, timezone
from typing import Literal

from namespaces import SHARED_MEMORY


SAFE_LABELS = {"safe", "review", "quarantine"}


async def label_and_store(db, runtime: Literal["alpha", "camaro", "chevelle"], payload: dict, label: str, reason: str = "") -> dict:
    if label not in SAFE_LABELS:
        raise ValueError(f"label must be one of {SAFE_LABELS}")
    doc = {
        "id": str(uuid.uuid4()),
        "runtime": runtime,
        "label": label,
        "reason": reason,
        "payload_summary": payload.get("summary") or str(payload)[:240],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    await db[SHARED_MEMORY].insert_one(doc)
    return doc

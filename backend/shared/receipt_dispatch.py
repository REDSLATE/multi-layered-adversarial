"""ADL receipt dispatch. Append-only ledger of decision events.
Receipts are observation-only by default — they record intent, not execution."""
import uuid
from datetime import datetime, timezone

from namespaces import SHARED_RECEIPTS


async def dispatch_receipt(db, runtime: str, action: str, intent: dict, observed: bool = True) -> dict:
    doc = {
        "id": str(uuid.uuid4()),
        "runtime": runtime,
        "action": action,
        "intent": intent,
        "observed": observed,
        "executed": False,  # OBSERVATION ONLY — never True until enforce flags flip
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    await db[SHARED_RECEIPTS].insert_one(doc)
    return doc

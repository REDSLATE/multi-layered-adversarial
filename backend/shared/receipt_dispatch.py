"""ADL receipt dispatch. Append-only ledger of decision events.

Receipts record a brain's intent and (when supplied) the execution
outcome. The seat-policy execution gate is what decides whether an
intent flows downstream — this module just persists the record.
"""
import uuid
from datetime import datetime, timezone

from namespaces import SHARED_RECEIPTS


async def dispatch_receipt(
    db, runtime: str, action: str, intent: dict,
    observed: bool = True, executed: bool = False,
) -> dict:
    """Append one receipt row.

    `executed` defaults to False (most receipts are observation-only
    records of an intent that did not route to a broker), but callers
    that DID execute MUST pass `executed=True` so the ADL doesn't lie
    about routing outcomes.
    """
    doc = {
        "id": str(uuid.uuid4()),
        "runtime": runtime,
        "action": action,
        "intent": intent,
        "observed": observed,
        "executed": executed,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    await db[SHARED_RECEIPTS].insert_one(doc)
    return doc

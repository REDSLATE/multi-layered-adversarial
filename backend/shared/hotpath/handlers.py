"""Atlas-side appliers for outbox events. Every handler MUST be
idempotent — a crash between apply and ack replays the event."""
from __future__ import annotations

from db import db
from shared.hotpath.outbox import register_handler


async def _apply_exit_receipt(event_id: str, payload: dict) -> None:
    doc = dict(payload)
    doc["outbox_id"] = event_id
    await db["shared_exit_receipts"].update_one(
        {"outbox_id": event_id}, {"$setOnInsert": doc}, upsert=True,
    )


async def _apply_exit_outcome(event_id: str, payload: dict) -> None:
    from shared.exits.outcomes import record_outcome  # noqa: WPS433
    res = await record_outcome(payload)
    if res is None:
        raise RuntimeError("record_outcome failed (Atlas write error)")


def register_all() -> None:
    register_handler("exit_receipt", _apply_exit_receipt)
    register_handler("exit_outcome", _apply_exit_outcome)

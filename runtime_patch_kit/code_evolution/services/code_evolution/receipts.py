"""Receipt dispatcher for Code Evolution v0.

Each stack persists its own receipts to its own datastore. The dispatcher
is a Protocol so a stack can swap Mongo for SQL without touching the rest
of the package.

Two collections (or tables) are written:
    code_evolution_proposals   — one row per /audit submission
    code_evolution_signoffs    — append-only countersign / reject events

No row ever leaves this stack. Each stack has its own audit trail.
"""
from __future__ import annotations

from typing import Any, Protocol, Optional


class ReceiptDispatcher(Protocol):
    """Storage contract. Implementations must be idempotent on proposal_id."""

    async def upsert_proposal(self, doc: dict[str, Any]) -> None: ...

    async def get_proposal(self, proposal_id: str) -> Optional[dict[str, Any]]: ...

    async def list_proposals(
        self, status: Optional[str] = None, limit: int = 100
    ) -> list[dict[str, Any]]: ...

    async def append_signoff(self, proposal_id: str, event: dict[str, Any]) -> None: ...

    async def update_status(
        self,
        proposal_id: str,
        new_status: str,
        signers: Optional[list[dict[str, Any]]] = None,
    ) -> None: ...


# ─────────────────────────── In-memory adapter (tests / smoke) ───────────────────────────

class InMemoryDispatcher:
    def __init__(self) -> None:
        self._props: dict[str, dict[str, Any]] = {}

    async def upsert_proposal(self, doc: dict[str, Any]) -> None:
        pid = doc["proposal_id"]
        existing = self._props.get(pid, {})
        existing.update(doc)
        existing.setdefault("signers", [])
        existing.setdefault("signoffs", [])
        self._props[pid] = existing

    async def get_proposal(self, proposal_id: str) -> Optional[dict[str, Any]]:
        doc = self._props.get(proposal_id)
        return None if doc is None else dict(doc)

    async def list_proposals(
        self, status: Optional[str] = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        items = list(self._props.values())
        if status:
            items = [d for d in items if d.get("status") == status]
        items.sort(key=lambda d: d.get("created_at", ""), reverse=True)
        return [dict(d) for d in items[:limit]]

    async def append_signoff(self, proposal_id: str, event: dict[str, Any]) -> None:
        doc = self._props.setdefault(proposal_id, {"proposal_id": proposal_id, "signoffs": []})
        doc.setdefault("signoffs", []).append(event)

    async def update_status(
        self,
        proposal_id: str,
        new_status: str,
        signers: Optional[list[dict[str, Any]]] = None,
    ) -> None:
        doc = self._props.setdefault(proposal_id, {"proposal_id": proposal_id})
        doc["status"] = new_status
        if signers is not None:
            doc["signers"] = list(signers)


# ─────────────────────────── Motor adapter (Mongo) ───────────────────────────

class MotorDispatcher:
    """Async Mongo adapter using motor.motor_asyncio.AsyncIOMotorDatabase.

    Usage in the host stack:
        from motor.motor_asyncio import AsyncIOMotorClient
        client = AsyncIOMotorClient(MONGO_URL)
        db = client[DB_NAME]
        dispatcher = MotorDispatcher(db)
    """

    PROPOSALS = "code_evolution_proposals"

    def __init__(self, db: Any) -> None:
        self._db = db

    async def upsert_proposal(self, doc: dict[str, Any]) -> None:
        body = {k: v for k, v in doc.items() if k != "_id"}
        body.setdefault("signers", [])
        body.setdefault("signoffs", [])
        await self._db[self.PROPOSALS].update_one(
            {"proposal_id": body["proposal_id"]},
            {"$setOnInsert": body},
            upsert=True,
        )

    async def get_proposal(self, proposal_id: str) -> Optional[dict[str, Any]]:
        return await self._db[self.PROPOSALS].find_one(
            {"proposal_id": proposal_id}, {"_id": 0}
        )

    async def list_proposals(
        self, status: Optional[str] = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        q: dict[str, Any] = {}
        if status:
            q["status"] = status
        cur = self._db[self.PROPOSALS].find(q, {"_id": 0}).sort("created_at", -1)
        return await cur.to_list(limit)

    async def append_signoff(self, proposal_id: str, event: dict[str, Any]) -> None:
        await self._db[self.PROPOSALS].update_one(
            {"proposal_id": proposal_id},
            {"$push": {"signoffs": event}},
        )

    async def update_status(
        self,
        proposal_id: str,
        new_status: str,
        signers: Optional[list[dict[str, Any]]] = None,
    ) -> None:
        update: dict[str, Any] = {"$set": {"status": new_status}}
        if signers is not None:
            update["$set"]["signers"] = list(signers)
        await self._db[self.PROPOSALS].update_one({"proposal_id": proposal_id}, update)

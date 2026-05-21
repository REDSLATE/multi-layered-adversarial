"""RISE_AI Saved Threads — `/api/admin/rise-ai/threads`.

Doctrine pin (2026-02-XX):
    Threads are REASONING MEMORY only. They persist transcripts
    so the operator can resume long-running thinking. They are
    NOT execution memory, NOT trade authority, NOT doctrine
    authority. The endpoints in this file:

      * Cannot place trades
      * Cannot submit intents
      * Cannot promote anything
      * Cannot hand off seats
      * Cannot edit doctrine
      * Cannot trigger automated execution

    A resumed thread reuses its `session_id` so the LLM kernel
    maintains continuity. The transcript is the operator's
    artifact; grading on individual messages still flows through
    the existing ledger grade endpoint.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, Field

from auth import get_current_user
from db import db
from namespaces import RISE_AI_THREAD_MESSAGES, RISE_AI_THREADS

log = logging.getLogger("risedual.rise_ai_threads")

router = APIRouter(prefix="/admin/rise-ai/threads", tags=["rise-ai-threads"])


# ─── helpers ──────────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _strip(doc: Dict[str, Any]) -> Dict[str, Any]:
    doc.pop("_id", None)
    for k in ("created_at", "updated_at"):
        v = doc.get(k)
        if isinstance(v, datetime):
            doc[k] = v.isoformat()
    return doc


def _strip_message(doc: Dict[str, Any]) -> Dict[str, Any]:
    doc.pop("_id", None)
    v = doc.get("created_at")
    if isinstance(v, datetime):
        doc["created_at"] = v.isoformat()
    return doc


# ─── schemas ──────────────────────────────────────────────────────────


class IncomingMessage(BaseModel):
    """One message as the UI knows it. The backend re-stamps `seq`
    and `created_at` on insert; the rest is recorded verbatim."""
    kind: str = Field(..., pattern="^(user|rise)$")
    text: str = Field("", max_length=20000)
    mode: Optional[str] = None
    role: Optional[str] = None
    call_id: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    latency_ms: Optional[int] = None
    llm_authority: Optional[str] = "ADVISORY_ONLY"
    extra: Optional[Dict[str, Any]] = None


class CreateThreadRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    session_id: Optional[str] = Field(default=None, max_length=120)
    mode: str = Field(default="chat", max_length=20)
    role: Optional[str] = Field(default=None, max_length=40)
    pinned: bool = False
    tags: List[str] = Field(default_factory=list)
    messages: List[IncomingMessage] = Field(default_factory=list)


class PatchThreadRequest(BaseModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=200)
    pinned: Optional[bool] = None
    tags: Optional[List[str]] = None
    archived: Optional[bool] = None
    append_messages: Optional[List[IncomingMessage]] = None


# ─── GET /threads ─────────────────────────────────────────────────────


@router.get("")
async def list_threads(
    pinned_only: bool = Query(default=False),
    archived: bool = Query(default=False),
    search: Optional[str] = Query(default=None, max_length=120),
    limit: int = Query(default=100, ge=1, le=500),
    _user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    q: Dict[str, Any] = {"archived": archived}
    if pinned_only:
        q["pinned"] = True
    if search:
        # Title OR tag contains the search term (case-insensitive).
        s = search.strip()
        q["$or"] = [
            {"title": {"$regex": s, "$options": "i"}},
            {"tags": {"$regex": s, "$options": "i"}},
        ]
    items: List[Dict[str, Any]] = []
    cursor = (
        db[RISE_AI_THREADS].find(q).sort([("pinned", -1), ("updated_at", -1)]).limit(limit)
    )
    async for d in cursor:
        items.append(_strip(d))
    return {"ok": True, "count": len(items), "items": items}


# ─── POST /threads ────────────────────────────────────────────────────


@router.post("")
async def create_thread(
    body: CreateThreadRequest,
    user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    thread_id = str(uuid.uuid4())
    session_id = body.session_id or f"thread-{thread_id}"
    now = _now()
    last_call_id = None
    # Persist messages first to compute message_count + last_call_id.
    for seq, m in enumerate(body.messages):
        await db[RISE_AI_THREAD_MESSAGES].insert_one({
            "thread_id": thread_id,
            "seq": seq,
            "kind": m.kind,
            "text": m.text,
            "mode": m.mode,
            "role": m.role,
            "call_id": m.call_id,
            "provider": m.provider,
            "model": m.model,
            "latency_ms": m.latency_ms,
            "llm_authority": m.llm_authority or "ADVISORY_ONLY",
            "extra": m.extra or None,
            "created_at": now,
        })
        if m.call_id:
            last_call_id = m.call_id
    doc = {
        "thread_id": thread_id,
        "title": body.title.strip(),
        "session_id": session_id,
        "mode": body.mode,
        "role": body.role,
        "pinned": bool(body.pinned),
        "tags": [t.strip() for t in (body.tags or []) if t.strip()][:20],
        "message_count": len(body.messages),
        "last_call_id": last_call_id,
        "created_at": now,
        "updated_at": now,
        "created_by": user.get("email", "operator"),
        "archived": False,
    }
    await db[RISE_AI_THREADS].insert_one(dict(doc))
    return {"ok": True, "thread": _strip(doc)}


# ─── GET /threads/{thread_id} ─────────────────────────────────────────


@router.get("/{thread_id}")
async def get_thread(
    thread_id: str = Path(...),
    _user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    thread = await db[RISE_AI_THREADS].find_one({"thread_id": thread_id})
    if not thread:
        raise HTTPException(status_code=404, detail=f"thread {thread_id!r} not found")
    messages: List[Dict[str, Any]] = []
    async for d in db[RISE_AI_THREAD_MESSAGES].find(
        {"thread_id": thread_id},
    ).sort("seq", 1):
        messages.append(_strip_message(d))
    return {"ok": True, "thread": _strip(thread), "messages": messages}


# ─── PATCH /threads/{thread_id} ───────────────────────────────────────


@router.patch("/{thread_id}")
async def patch_thread(
    body: PatchThreadRequest,
    thread_id: str = Path(...),
    _user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    thread = await db[RISE_AI_THREADS].find_one({"thread_id": thread_id})
    if not thread:
        raise HTTPException(status_code=404, detail=f"thread {thread_id!r} not found")

    update: Dict[str, Any] = {"updated_at": _now()}
    if body.title is not None:
        update["title"] = body.title.strip()
    if body.pinned is not None:
        update["pinned"] = bool(body.pinned)
    if body.tags is not None:
        update["tags"] = [t.strip() for t in body.tags if t.strip()][:20]
    if body.archived is not None:
        update["archived"] = bool(body.archived)

    # Append messages if supplied — atomic per-message seq increment.
    appended = 0
    last_call_id = thread.get("last_call_id")
    if body.append_messages:
        start_seq = int(thread.get("message_count") or 0)
        now = _now()
        for i, m in enumerate(body.append_messages):
            await db[RISE_AI_THREAD_MESSAGES].insert_one({
                "thread_id": thread_id,
                "seq": start_seq + i,
                "kind": m.kind,
                "text": m.text,
                "mode": m.mode,
                "role": m.role,
                "call_id": m.call_id,
                "provider": m.provider,
                "model": m.model,
                "latency_ms": m.latency_ms,
                "llm_authority": m.llm_authority or "ADVISORY_ONLY",
                "extra": m.extra or None,
                "created_at": now,
            })
            if m.call_id:
                last_call_id = m.call_id
            appended += 1
        update["message_count"] = start_seq + appended
        update["last_call_id"] = last_call_id

    await db[RISE_AI_THREADS].update_one(
        {"thread_id": thread_id}, {"$set": update},
    )
    refreshed = await db[RISE_AI_THREADS].find_one({"thread_id": thread_id})
    return {"ok": True, "appended": appended, "thread": _strip(refreshed)}


# ─── POST /threads/{thread_id}/resume ─────────────────────────────────


@router.post("/{thread_id}/resume")
async def resume_thread(
    thread_id: str = Path(...),
    _user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Return the `session_id` + mode + role + transcript needed
    for the operator to continue the conversation. The kernel
    uses session_id for continuity already, so the next
    `/api/ai/run` call with this session_id picks up where the
    thread left off (within provider-level context limits).
    """
    thread = await db[RISE_AI_THREADS].find_one({"thread_id": thread_id})
    if not thread:
        raise HTTPException(status_code=404, detail=f"thread {thread_id!r} not found")
    # Bump updated_at so resumed threads sort to the top.
    await db[RISE_AI_THREADS].update_one(
        {"thread_id": thread_id}, {"$set": {"updated_at": _now()}},
    )
    messages: List[Dict[str, Any]] = []
    async for d in db[RISE_AI_THREAD_MESSAGES].find(
        {"thread_id": thread_id},
    ).sort("seq", 1):
        messages.append(_strip_message(d))
    return {
        "ok": True,
        "thread_id": thread_id,
        "session_id": thread["session_id"],
        "mode": thread.get("mode", "chat"),
        "role": thread.get("role"),
        "title": thread.get("title"),
        "messages": messages,
    }

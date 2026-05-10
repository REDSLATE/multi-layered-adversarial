"""Cross-brain discussion layer.

Doctrine:
    Brains share OPINIONS (heuristic outputs, observations, disagreements).
    Brains do NOT share INTERNAL STATE (feature vectors, model logits,
    raw memory weights). Internal state lives in each brain's own namespace
    and is never readable peer-to-peer.

Properties enforced here:
    - All comms mediated through Mission Control (no direct A→B channel).
    - Pull-only consumption — no peer push.
    - Schema rejects anything claiming execution.
    - Reply threading walks `in_reply_to` for cycle detection.
    - `evidence` size capped to prevent leaking internal state under guise.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from auth import get_current_user
from db import db
from namespaces import (
    ADVISORS,
    DISCUSSION_PARTICIPANTS,
    RUNTIMES,
    ROLES,
    SHARED_AUTHORITY_STATE,
    SHARED_HEARTBEATS,
    SHARED_OPINIONS,
)
from runtime_auth import verify_runtime_token


# ──────────────────────── config (small, deliberate) ────────────────────────

# Stance vocabulary — expanded so brains can express more than just direction.
# Doctrine: communication is not a learning bottleneck; expand freely.
STANCE_VALUES = (
    # Directional calls
    "long", "short", "veto", "endorse",
    # Discourse moves
    "question", "observation",
    # Peer engagement
    "agree", "disagree", "refine", "retract",
    # Theorising
    "hypothesis",
)

# Topic kinds — anchors a discussion thread to a thing. Permissive: any
# `<kind>:<value>` is accepted as long as kind is a valid identifier and
# value is non-empty. The closed whitelist was a learning bottleneck; gone.
import re as _re
_TOPIC_KIND_RE = _re.compile(r"^[a-z_][a-z0-9_]*$")

# Hard caps — kept generous enough not to throttle learning; still bounded
# so a single payload can't denial-of-service the layer.
MAX_BODY_CHARS = 8_000
MAX_EVIDENCE_BYTES = 65_536       # 64 KB JSON serialised
MAX_THREAD_DEPTH = 64             # cycle / runaway-thread guard


# ──────────────────────── ingest (write) ────────────────────────

class OpinionIn(BaseModel):
    """Schema for posting an opinion. `runtime` is the speaker."""
    runtime: Literal["alpha", "camaro", "chevelle", "redeye"]
    topic: str = Field(..., min_length=1, max_length=128)
    stance: Literal[
        "long", "short", "veto", "endorse",
        "question", "observation",
        "agree", "disagree", "refine", "retract",
        "hypothesis",
    ]
    confidence: float = Field(0.5, ge=0.0, le=1.0)
    body: str = Field(..., min_length=1, max_length=MAX_BODY_CHARS)
    evidence: dict = Field(default_factory=dict)
    in_reply_to: Optional[str] = None
    # ALWAYS False. Schema-rejected if anything else is sent.
    may_execute: bool = False

    @field_validator("topic")
    @classmethod
    def _topic_format(cls, v: str) -> str:
        if v == "free":
            return v
        if ":" not in v:
            raise ValueError(
                "topic must be 'free' or '<kind>:<value>' (e.g. 'symbol:TSLA', "
                "'regime:trend', 'theory:momentum_decay')"
            )
        kind, _, value = v.partition(":")
        if not _TOPIC_KIND_RE.match(kind):
            raise ValueError(
                f"topic kind must match [a-z_][a-z0-9_]*; got {kind!r}"
            )
        if not value:
            raise ValueError("topic value cannot be empty")
        return v

    @field_validator("may_execute")
    @classmethod
    def _no_execution_claim(cls, v: bool) -> bool:
        # The discussion layer never carries execution claims. Any opinion
        # asserting may_execute=True is structurally illegitimate — reject
        # before it ever lands in Mongo.
        if v is not False:
            raise ValueError(
                "opinions MUST set may_execute=false; the discussion layer "
                "does not carry execution authority"
            )
        return v

    @field_validator("evidence")
    @classmethod
    def _evidence_size(cls, v: dict) -> dict:
        import json
        try:
            blob = json.dumps(v, default=str)
        except (TypeError, ValueError) as e:
            raise ValueError(f"evidence must be JSON-serialisable: {e}") from e
        if len(blob.encode("utf-8")) > MAX_EVIDENCE_BYTES:
            raise ValueError(
                f"evidence exceeds {MAX_EVIDENCE_BYTES} bytes; trim before posting "
                f"(only references — never raw internal state)"
            )
        return v


router = APIRouter(tags=["discussion"])


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@router.post("/ingest/opinion")
async def post_opinion(
    body: OpinionIn,
    x_runtime_token: str | None = Header(default=None, alias="X-Runtime-Token"),
):
    """A brain posts an opinion into the shared discussion layer.

    The same X-Runtime-Token mechanism used by other ingest endpoints applies.
    Reply threading is validated server-side: the `in_reply_to` chain is
    walked to MAX_THREAD_DEPTH to detect cycles or runaway threads.
    """
    verify_runtime_token(body.runtime, x_runtime_token or "")

    # Validate reply target if present.
    if body.in_reply_to:
        parent = await db[SHARED_OPINIONS].find_one(
            {"opinion_id": body.in_reply_to}, {"_id": 0, "thread_root": 1, "depth": 1}
        )
        if not parent:
            raise HTTPException(status_code=404, detail="in_reply_to opinion not found")
        depth = (parent.get("depth") or 0) + 1
        if depth > MAX_THREAD_DEPTH:
            raise HTTPException(
                status_code=400,
                detail=f"thread depth would exceed MAX_THREAD_DEPTH={MAX_THREAD_DEPTH}",
            )
        thread_root = parent.get("thread_root") or body.in_reply_to
    else:
        depth = 0
        thread_root = None  # filled in after insert with own opinion_id

    opinion_id = str(uuid.uuid4())
    doc = {
        "opinion_id": opinion_id,
        "runtime": body.runtime,
        "topic": body.topic,
        "stance": body.stance,
        "confidence": float(body.confidence),
        "body": body.body,
        "evidence": body.evidence,
        "in_reply_to": body.in_reply_to,
        "thread_root": thread_root or opinion_id,
        "depth": depth,
        "may_execute": False,           # belt and braces — stored explicitly false
        "posted_at": _now_iso(),
    }
    await db[SHARED_OPINIONS].insert_one(doc)

    # Conflict auto-detection — never blocks the post.
    from shared.conflicts import detect_conflicts_for_opinion  # noqa: WPS433
    new_conflicts = await detect_conflicts_for_opinion(doc)

    return {
        "ok": True,
        "opinion_id": opinion_id,
        "thread_root": doc["thread_root"],
        "depth": depth,
        "conflicts_detected": [c["conflict_id"] for c in new_conflicts],
    }


# ──────────────────────── shared (read) ────────────────────────

@router.get("/shared/opinions")
async def list_opinions(
    runtime: Optional[str] = Query(None, description="filter by speaker"),
    topic: Optional[str] = Query(None, description="exact topic match"),
    symbol: Optional[str] = Query(None, description="convenience: shorthand for topic=symbol:<X>"),
    thread: Optional[str] = Query(None, description="filter to a thread by thread_root"),
    since: Optional[str] = Query(None, description="ISO timestamp; opinions strictly after"),
    limit: int = Query(100, ge=1, le=500),
    _user: dict = Depends(get_current_user),
):
    if runtime and runtime not in DISCUSSION_PARTICIPANTS:
        raise HTTPException(
            status_code=400,
            detail=f"runtime must be one of {DISCUSSION_PARTICIPANTS}",
        )
    q: dict = {}
    if runtime:
        q["runtime"] = runtime
    if topic:
        q["topic"] = topic
    if symbol:
        q["topic"] = f"symbol:{symbol.upper()}"
    if thread:
        q["thread_root"] = thread
    if since:
        q["posted_at"] = {"$gt": since}
    docs = (
        await db[SHARED_OPINIONS]
        .find(q, {"_id": 0})
        .sort("posted_at", -1)
        .to_list(limit)
    )
    return {"items": docs, "count": len(docs)}


@router.get("/shared/opinions/{opinion_id}")
async def get_opinion_thread(
    opinion_id: str,
    _user: dict = Depends(get_current_user),
):
    """Return the opinion + the entire thread it belongs to (oldest first)."""
    target = await db[SHARED_OPINIONS].find_one({"opinion_id": opinion_id}, {"_id": 0})
    if not target:
        raise HTTPException(status_code=404, detail="opinion not found")
    root = target.get("thread_root") or opinion_id
    thread = (
        await db[SHARED_OPINIONS]
        .find({"thread_root": root}, {"_id": 0})
        .sort("posted_at", 1)
        .to_list(MAX_THREAD_DEPTH * 4)
    )
    return {"thread_root": root, "items": thread, "count": len(thread)}


# ──────────────────────── roles manifest ────────────────────────

@router.get("/shared/roles-manifest")
async def roles_manifest(_user: dict = Depends(get_current_user)):
    """Read-only view of every participant's identity. Brains call this on
    boot + on a refresh interval so they know their peers.

    Includes RUNTIMES + ADVISORS. Authority state is included for runtimes
    only — advisors are off-ladder by definition.
    """
    items: list[dict] = []
    # Runtimes — include current authority state and last_seen.
    for rt in RUNTIMES:
        role = ROLES.get(rt, {})
        auth = await db[SHARED_AUTHORITY_STATE].find_one({"runtime": rt}, {"_id": 0})
        hb = await db[SHARED_HEARTBEATS].find_one({"runtime": rt}, {"_id": 0})
        items.append({
            "runtime": rt,
            "kind": "runtime",
            "role": role.get("role"),
            "title": role.get("title"),
            "tagline": role.get("tagline"),
            "description": role.get("description"),
            "allowed_actions": role.get("allowed_actions", []),
            "authority_state": (auth or {}).get("authority_state"),
            "last_seen": (hb or {}).get("last_seen"),
            "may_execute": False,  # observation-only across all brains
        })
    # Advisors — no authority state (off-ladder by design).
    for ad in ADVISORS:
        role = ROLES.get(ad, {})
        hb = await db[SHARED_HEARTBEATS].find_one({"runtime": ad}, {"_id": 0})
        items.append({
            "runtime": ad,
            "kind": "advisor",
            "role": role.get("role"),
            "title": role.get("title"),
            "tagline": role.get("tagline"),
            "description": role.get("description"),
            "allowed_actions": role.get("allowed_actions", []),
            "authority_state": None,
            "last_seen": (hb or {}).get("last_seen"),
            "may_execute": False,
        })
    return {
        "items": items,
        "count": len(items),
        "doctrine": (
            "Brains share opinions, not internal model state. None can "
            "execute trades, paper or live."
        ),
    }


# ─────────────────── runtime-authenticated reads ───────────────────
# Brains read peer opinions and the roles manifest as part of "learning".
# These mirror the operator reads above but accept X-Runtime-Token instead
# of an operator JWT. Same data, runtime-friendly auth path.

@router.get("/runtime-discussion/opinions")
async def runtime_list_opinions(
    runtime_caller: str = Query(..., alias="caller"),
    runtime: Optional[str] = Query(None, description="filter by speaker"),
    topic: Optional[str] = Query(None),
    symbol: Optional[str] = Query(None),
    thread: Optional[str] = Query(None),
    since: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    x_runtime_token: str | None = Header(default=None, alias="X-Runtime-Token"),
):
    verify_runtime_token(runtime_caller, x_runtime_token or "")
    if runtime and runtime not in DISCUSSION_PARTICIPANTS:
        raise HTTPException(
            status_code=400,
            detail=f"runtime must be one of {DISCUSSION_PARTICIPANTS}",
        )
    q: dict = {}
    if runtime:
        q["runtime"] = runtime
    if topic:
        q["topic"] = topic
    if symbol:
        q["topic"] = f"symbol:{symbol.upper()}"
    if thread:
        q["thread_root"] = thread
    if since:
        q["posted_at"] = {"$gt": since}
    docs = (
        await db[SHARED_OPINIONS]
        .find(q, {"_id": 0})
        .sort("posted_at", -1)
        .to_list(limit)
    )
    return {"items": docs, "count": len(docs)}


@router.get("/runtime-discussion/roles-manifest")
async def runtime_roles_manifest(
    runtime_caller: str = Query(..., alias="caller"),
    x_runtime_token: str | None = Header(default=None, alias="X-Runtime-Token"),
):
    verify_runtime_token(runtime_caller, x_runtime_token or "")
    # Reuse the same builder by hitting the underlying collections directly.
    items: list[dict] = []
    for rt in RUNTIMES:
        role = ROLES.get(rt, {})
        auth = await db[SHARED_AUTHORITY_STATE].find_one({"runtime": rt}, {"_id": 0})
        hb = await db[SHARED_HEARTBEATS].find_one({"runtime": rt}, {"_id": 0})
        items.append({
            "runtime": rt, "kind": "runtime",
            "role": role.get("role"), "title": role.get("title"),
            "tagline": role.get("tagline"), "description": role.get("description"),
            "allowed_actions": role.get("allowed_actions", []),
            "authority_state": (auth or {}).get("authority_state"),
            "last_seen": (hb or {}).get("last_seen"),
            "may_execute": False,
        })
    for ad in ADVISORS:
        role = ROLES.get(ad, {})
        hb = await db[SHARED_HEARTBEATS].find_one({"runtime": ad}, {"_id": 0})
        items.append({
            "runtime": ad, "kind": "advisor",
            "role": role.get("role"), "title": role.get("title"),
            "tagline": role.get("tagline"), "description": role.get("description"),
            "allowed_actions": role.get("allowed_actions", []),
            "authority_state": None,
            "last_seen": (hb or {}).get("last_seen"),
            "may_execute": False,
        })
    return {
        "items": items,
        "count": len(items),
        "doctrine": (
            "Brains share opinions, not internal model state. None can "
            "execute trades, paper or live."
        ),
    }

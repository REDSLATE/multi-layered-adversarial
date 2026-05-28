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

import asyncio
import os
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
    SHARED_RECEIPTS,
)
from runtime_auth import verify_runtime_token


# ──────────────────────── governor authority-call mirror ──────────────────────


async def _mirror_authority_call_to_receipts(opinion_doc: dict) -> None:
    """Bridge from `/api/ingest/opinion` payloads to `shared_adl_receipts`
    so the council's gate chain (which reads receipts, NOT opinions)
    sees governor authority calls.

    Doctrine (2026-05-19): brains post their governor decisions through
    the opinion discussion layer (where Chevelle's role adapter lands),
    but the council reads from receipts. Without this mirror, every
    Chevelle authority call would be invisible to the gate chain and
    every intent would block on `NO_STANCE_LOW_EFFECTIVE_CONF`.

    Only mirrors when:
      * `evidence.authority_call` is present and is a dict
      * The authority_call's `brain` matches the opinion's runtime
        (defensive — opinions cannot impersonate authority calls)
    """
    evidence = opinion_doc.get("evidence") or {}
    if not isinstance(evidence, dict):
        return
    auth_call = evidence.get("authority_call")
    if not isinstance(auth_call, dict):
        return
    runtime = opinion_doc.get("runtime")
    if not runtime:
        return
    # Defensive: brain field on the inner authority_call must match the
    # opinion's runtime — opinions can't impersonate other brains.
    if str(auth_call.get("brain", "")).lower() != str(runtime).lower():
        return

    # Translate the survival-kit authority_call shape into the
    # signal fields the council's `_normalize_governor_call` expects.
    # status BLOCK/ALLOW/WARN → executable + stance; reason flows through
    # untouched so the FATAL/SILENCE taxonomy classifies it correctly.
    status = str(auth_call.get("status", "")).upper()
    reason_code = str(auth_call.get("reason", "")).upper()
    if status == "BLOCK":
        stance = "VETO" if reason_code in ("GOVERNOR_HARD_VETO",) or reason_code.startswith("GOVERNOR_HARD_VETO_") else "DISSENT"
        executable = False
        veto = (reason_code in ("GOVERNOR_HARD_VETO",) or reason_code.startswith("GOVERNOR_HARD_VETO_"))
    elif status == "WARN":
        stance = "DISSENT"
        executable = False
        veto = False
    else:  # ALLOW or anything else benign
        stance = "ENDORSE"
        executable = True
        veto = False

    payload_for_council = {
        # Council's `_normalize_governor_call` looks for these inside
        # the container at `payload` / `intent` / `call` / `data` / root.
        "executable": executable,
        "veto": veto,
        "confidence": float(auth_call.get("confidence") or 0.0),
        "stance": stance,
        "reason": reason_code or "NO_GOVERNOR_DISSENT",
    }

    receipt = {
        "receipt_id": str(uuid.uuid4()),
        "runtime": runtime,
        # Council filters with _ACTION_FIELDS=("action","kind","type","event")
        # against _AUTHORITY_CALL_VALUES=("authority_call",...). Use the
        # exact value the filter expects so `_latest_governor_call()` finds us.
        "action": "authority_call",
        "kind": "authority_call_mirror",   # debug breadcrumb only
        "source_opinion_id": opinion_doc.get("opinion_id"),
        "thread_root": opinion_doc.get("thread_root"),
        "topic": opinion_doc.get("topic"),
        # Council `_normalize_governor_call` walks containers in priority:
        # intent → payload → call → data → root. Put the translated signals
        # under `payload` (a recognised container) so the normalizer hits.
        "payload": payload_for_council,
        # Raw authority_call kept alongside for audit / forensic replay.
        "authority_call": auth_call,
        # Top-level mirrors so council's `_symbol_clause()` finds the symbol.
        "symbol": auth_call.get("symbol"),
        "lane": auth_call.get("lane"),
        "timestamp": _now_iso(),
    }
    await db[SHARED_RECEIPTS].insert_one(receipt)


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
# Market regime tag — free-form snake_case identifier, capped length.
# Examples: "trend", "chop", "high_vol", "risk_on", "earnings_week".
# Doctrine: the operator-controlled vocabulary; no closed whitelist so
# Camaro can learn whatever regime decomposition turns out to be useful.
_REGIME_RE = _re.compile(r"^[a-z_][a-z0-9_]*$")
MAX_REGIME_LEN = 48

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
    # Optional market regime tag (Step 5 — Camaro command training).
    # Snake_case identifier; brains include this so we can later compute
    # "endorse hit rate by regime" without joining external context.
    regime: Optional[str] = Field(default=None, max_length=MAX_REGIME_LEN)
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

    @field_validator("regime")
    @classmethod
    def _regime_format(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v == "":
            return None
        if not _REGIME_RE.match(v):
            raise ValueError(
                "regime must be a snake_case identifier ([a-z_][a-z0-9_]*); "
                f"got {v!r}"
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
    # Look up the brain's current role at posting time. Best-effort —
    # roster failures must never block an opinion.
    posted_as: Optional[str] = None
    try:
        from shared.roster import get_role_of  # noqa: WPS433
        posted_as = await get_role_of(body.runtime)
    except Exception:  # noqa: BLE001
        posted_as = None

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
        "regime": body.regime,           # optional snake_case tag; None if unset
        "posted_as": posted_as,          # role from live roster at post time
        "may_execute": False,           # belt and braces — stored explicitly false
        "posted_at": _now_iso(),
    }
    # Anchor-price capture for directional opinions (2026-05-24).
    # The opinion resolver needs a reference price to grade against.
    # Best-effort — if the price fetch fails OR exceeds the bounded
    # deadline the opinion still posts; the resolver will skip it
    # until an operator backfills the anchor or a later fetch lands.
    #
    # 2026-05-28 — Chevelle-author reported 504s from MC's
    # `/api/ingest/opinion` under load: "hard wall-clock deadline 10.0s
    # exceeded" from the platform ingress. Root cause: this anchor-price
    # fetch synchronously calls Alpaca's get_latest_trade (equity) or
    # Kraken's public ticker (crypto), and when those brokers slow
    # down the POST hangs past the proxy's 10s deadline. Fix: bound
    # the fetch with a short asyncio timeout (default 1.5s, env-
    # tuneable). On timeout we drop the anchor capture, log a debug
    # breadcrumb, and let the opinion land. The resolver re-grades
    # later — anchor_price is not needed for the opinion itself to be
    # ingested, only for outcome scoring downstream.
    if body.stance in {"long", "short"}:
        anchor_timeout_s = float(os.environ.get(
            "OPINION_ANCHOR_FETCH_TIMEOUT_SEC", "1.5",
        ))
        try:
            from shared.opinion_resolver import (  # noqa: WPS433
                _fetch_current_price, _lane_for_topic, _symbol_from_topic,
            )
            sym = _symbol_from_topic(body.topic)
            lane = _lane_for_topic(body.topic)
            if sym:
                anchor = await asyncio.wait_for(
                    _fetch_current_price(sym, lane),
                    timeout=anchor_timeout_s,
                )
                if anchor and anchor > 0:
                    doc["anchor_price"] = float(anchor)
                    doc["anchor_lane"] = lane
        except asyncio.TimeoutError:
            # Don't block the post on a slow broker. Resolver backfills.
            pass
        except Exception:  # noqa: BLE001
            pass
    await db[SHARED_OPINIONS].insert_one(doc)

    # ── Governor authority-call mirror (2026-05-19) ──────────────────
    # When the opinion carries `evidence.authority_call`, also persist
    # it to SHARED_RECEIPTS so the council's `_latest_governor_call()`
    # (which reads from receipts, NOT opinions) sees it.
    #
    # This is the doctrine bridge: Chevelle posts via the opinion
    # discussion layer, but the council's gate chain reads from the
    # receipts collection. Without this mirror, Chevelle's authority
    # calls are silent to the gate chain — which is exactly the
    # "Chevelle silent" bug we're fixing.
    #
    # Best-effort: mirror failures must never block the opinion post.
    try:
        await _mirror_authority_call_to_receipts(doc)
    except Exception:  # noqa: BLE001
        pass

    # Conflict auto-detection — never blocks the post.
    # 2026-05-28 — also bounded; conflict detection runs one indexed
    # mongo query + per-candidate idempotency lookups. Under high
    # opinion-volume load it can occasionally exceed the ingress
    # deadline. Bound at 2.0s; on timeout we return an empty
    # conflicts_detected list and let the operator detect conflicts
    # on-demand via the conflicts router.
    from shared.conflicts import detect_conflicts_for_opinion  # noqa: WPS433
    conflicts_timeout_s = float(os.environ.get(
        "OPINION_CONFLICT_DETECT_TIMEOUT_SEC", "2.0",
    ))
    try:
        new_conflicts = await asyncio.wait_for(
            detect_conflicts_for_opinion(doc),
            timeout=conflicts_timeout_s,
        )
    except asyncio.TimeoutError:
        new_conflicts = []
    except Exception:  # noqa: BLE001
        new_conflicts = []

    return {
        "ok": True,
        "opinion_id": opinion_id,
        "thread_root": doc["thread_root"],
        "depth": depth,
        "conflicts_detected": [c["conflict_id"] for c in new_conflicts],
    }


# ──────────────────────── admin proxy (operator speaks-as) ────────────────────────

@router.post("/admin/runtime-discussion/opinion")
async def admin_post_opinion(
    body: OpinionIn,
    user: dict = Depends(get_current_user),  # noqa: B008
):
    """Admin proxy — an authenticated operator can post an opinion as any brain.

    Identical write path and conflict detection as `/ingest/opinion`, but the
    operator's admin JWT substitutes for the brain's X-Runtime-Token. Every
    admin-posted opinion is stamped with `posted_by_admin_email` so the audit
    trail records who spoke on behalf of whom.

    Use cases:
      * Operator forcing a missing brain to register a stance during dry runs.
      * Manual moderation / corrections when a brain's sidecar is offline.
      * UI "Speak as <brain>" debug controls.
    """
    # Same reply-target validation as the runtime path.
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
        thread_root = None

    opinion_id = str(uuid.uuid4())

    posted_as: Optional[str] = None
    try:
        from shared.roster import get_role_of  # noqa: WPS433
        posted_as = await get_role_of(body.runtime)
    except Exception:  # noqa: BLE001
        posted_as = None

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
        "regime": body.regime,
        "posted_as": posted_as,
        "may_execute": False,
        "posted_at": _now_iso(),
        # Audit trail — admin proxy stamps who spoke as this brain.
        "posted_by_admin_email": user.get("email"),
        "posted_via": "admin_proxy",
    }
    await db[SHARED_OPINIONS].insert_one(doc)

    from shared.conflicts import detect_conflicts_for_opinion  # noqa: WPS433
    new_conflicts = await detect_conflicts_for_opinion(doc)

    return {
        "ok": True,
        "opinion_id": opinion_id,
        "thread_root": doc["thread_root"],
        "depth": depth,
        "conflicts_detected": [c["conflict_id"] for c in new_conflicts],
        "posted_via": "admin_proxy",
        "speaker_runtime": body.runtime,
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

    Includes RUNTIMES + ADVISORS. The `may_execute` bit on each row is
    derived from whichever **seat** the brain currently holds (via
    seat_policy.snapshot). If the brain doesn't currently hold a seat,
    or holds a non-executing seat, the bit is False. If it holds the
    Executor seat for equity OR Crypto, the bit is True. This is the
    only authority surface — brain identity has zero standing of its
    own. (Defanged 2026-05-17: previously hardcoded `False` for all
    brains, which was a phantom restriction overlaying seat policy.)
    """
    from shared.seat_policy import seat_may_execute_lane  # noqa: WPS433
    from shared.roster import get_roster  # noqa: WPS433

    try:
        roster = await get_roster()
        assignments = roster.get("assignments") or {}
    except Exception:  # noqa: BLE001
        assignments = {}

    def _may_execute_for(brain: str) -> bool:
        # Find the seat (if any) this brain currently holds; ask the
        # seat policy whether THAT SEAT may execute on EITHER lane.
        for seat, holder in assignments.items():
            if holder == brain:
                if seat_may_execute_lane(seat, "equity") or seat_may_execute_lane(seat, "crypto"):
                    return True
        return False

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
            # Seat-derived. True iff this brain holds an executor seat
            # for at least one lane.
            "may_execute": _may_execute_for(rt),
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
            # Advisors never hold an executor seat by design.
            "may_execute": False,
        })
    return {
        "items": items,
        "count": len(items),
        "doctrine": (
            "Brains share opinions. Authority is seat-bound: a brain may "
            "execute only when it holds an executor seat. MC is the "
            "regulator at the execution gate, not at the opinion layer."
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

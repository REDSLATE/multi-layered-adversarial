"""Conflict memory (Step 4 of the cross-brain training plan).

When two brains post opposing stances on the same topic within a window,
the disagreement is auto-flagged as a conflict. The operator (or Chevelle)
resolves the conflict by attaching outcomes to the participating opinions;
the conflict's winner is then computed from those outcomes.

Doctrine carried over:
    Communication is unrestricted. None of the four brains can execute
    (paper or live). Conflicts are evidence; they do not gate authority.

What lives where:
    detect_conflicts_for_opinion()  — called from opinions.post_opinion
                                      after insert. Synchronous, low-cost.
    /api/shared/conflicts            — operator JWT. List + filters.
    /api/shared/conflicts/{id}       — single conflict + status.
    /api/shared/conflicts/pair-scorecard — X-vs-Y win rates over resolved
                                           conflicts.
    /api/admin/conflicts/{id}/resolve — operator picks the winner manually
                                       (used when outcomes are unavailable
                                       or ambiguous).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from auth import get_current_user
from db import db
from namespaces import (
    DISCUSSION_PARTICIPANTS,
    SHARED_CONFLICTS,
    SHARED_OPINIONS,
    SHARED_OUTCOMES,
)


# ──────────────────────── conflict matrix ────────────────────────

# Two opinions on the same topic conflict when one stance is positive and
# the other is negative. Neutral stances do not trigger conflict by
# themselves. This is intentionally permissive — discussion stances
# (question, observation, refine) shouldn't generate noise.
POSITIVE_STANCES: frozenset[str] = frozenset(
    {"long", "endorse", "agree", "hypothesis"}
)
NEGATIVE_STANCES: frozenset[str] = frozenset(
    {"short", "veto", "disagree", "retract"}
)
NEUTRAL_STANCES: frozenset[str] = frozenset(
    {"question", "observation", "refine"}
)

# Window for auto-detection. Two brains posting opposing stances on the
# same topic within this window get auto-flagged. Longer than a single
# trading session intentionally — discussion can span hours.
DEFAULT_CONFLICT_WINDOW_MINUTES = 240   # 4h


def _stance_polarity(stance: str) -> str:
    if stance in POSITIVE_STANCES:
        return "positive"
    if stance in NEGATIVE_STANCES:
        return "negative"
    return "neutral"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ──────────────────────── auto-detection hook ────────────────────────

async def detect_conflicts_for_opinion(
    new_opinion: dict,
    *,
    window_minutes: int = DEFAULT_CONFLICT_WINDOW_MINUTES,
) -> list[dict]:
    """Called from opinions.post_opinion after a new opinion is inserted.
    Looks back `window_minutes` for opinions on the same topic from
    different runtimes with the opposite polarity, and creates one
    conflict record per detected pair.

    Returns the list of conflict docs created (may be empty). Never raises;
    failures here must not block the post.
    """
    try:
        polarity = _stance_polarity(new_opinion["stance"])
        if polarity == "neutral":
            return []

        opposing = NEGATIVE_STANCES if polarity == "positive" else POSITIVE_STANCES
        window_start = (
            datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
        ).isoformat()

        candidates = await (
            db[SHARED_OPINIONS]
            .find(
                {
                    "topic": new_opinion["topic"],
                    "stance": {"$in": list(opposing)},
                    "runtime": {"$ne": new_opinion["runtime"]},
                    "posted_at": {"$gte": window_start},
                    "opinion_id": {"$ne": new_opinion["opinion_id"]},
                },
                {"_id": 0, "opinion_id": 1, "runtime": 1, "stance": 1,
                 "confidence": 1, "posted_at": 1},
            )
            .sort("posted_at", -1)
            .to_list(50)
        )

        created: list[dict] = []
        for c in candidates:
            # Idempotency — if a conflict already exists for this exact pair
            # of opinions, skip. (Pair = unordered set of two opinion_ids.)
            pair_ids = sorted([new_opinion["opinion_id"], c["opinion_id"]])
            existing = await db[SHARED_CONFLICTS].find_one(
                {"pair_ids": pair_ids}, {"_id": 0, "conflict_id": 1}
            )
            if existing:
                continue

            doc = {
                "conflict_id": str(uuid.uuid4()),
                "topic": new_opinion["topic"],
                "detected_at": _now_iso(),
                "pair_ids": pair_ids,            # sorted for idempotency lookup
                "participants": [
                    {
                        "opinion_id": new_opinion["opinion_id"],
                        "runtime": new_opinion["runtime"],
                        "stance": new_opinion["stance"],
                        "confidence": float(new_opinion.get("confidence", 0.5)),
                        "posted_at": new_opinion["posted_at"],
                    },
                    {
                        "opinion_id": c["opinion_id"],
                        "runtime": c["runtime"],
                        "stance": c["stance"],
                        "confidence": float(c.get("confidence", 0.5)),
                        "posted_at": c["posted_at"],
                    },
                ],
                "status": "open",                # open | resolved | stale
                "winner": None,
                "winning_opinion_id": None,
                "resolved_at": None,
                "resolved_by": None,
                "resolution_source": None,       # outcomes | manual
                "notes": "",
            }
            await db[SHARED_CONFLICTS].insert_one(doc)
            created.append(doc)
        return created
    except Exception as e:  # noqa: BLE001
        # Detection must never block a post.
        from logging import getLogger
        getLogger("risedual.conflicts").warning("conflict detection failed: %s", e)
        return []


# ──────────────────────── auto-resolve from outcomes ────────────────────────

async def _try_auto_resolve(conflict: dict) -> Optional[dict]:
    """If both participants have outcomes attached, compute the winner
    automatically (the one whose `actual` is "win"). Returns the updated
    conflict doc if resolved, None otherwise."""
    if conflict["status"] != "open":
        return conflict

    pids = [p["opinion_id"] for p in conflict["participants"]]
    outcomes = await db[SHARED_OUTCOMES].find(
        {"opinion_id": {"$in": pids}}, {"_id": 0}
    ).to_list(2)
    if len(outcomes) < 2:
        return None  # at least one not yet resolved

    # Build a lookup by opinion_id → outcome
    by_id = {o["opinion_id"]: o for o in outcomes}
    wins = [pid for pid, o in by_id.items() if o["actual"] == "win"]

    if len(wins) == 1:
        winning_oid = wins[0]
        winning_runtime = next(
            p["runtime"] for p in conflict["participants"]
            if p["opinion_id"] == winning_oid
        )
        await db[SHARED_CONFLICTS].update_one(
            {"conflict_id": conflict["conflict_id"]},
            {"$set": {
                "status": "resolved",
                "winner": winning_runtime,
                "winning_opinion_id": winning_oid,
                "resolved_at": _now_iso(),
                "resolved_by": "outcomes:auto",
                "resolution_source": "outcomes",
            }},
        )
    elif len(wins) == 0:
        # Both lost or both no-event/ambiguous: mark stale, no winner.
        await db[SHARED_CONFLICTS].update_one(
            {"conflict_id": conflict["conflict_id"]},
            {"$set": {
                "status": "stale",
                "resolved_at": _now_iso(),
                "resolved_by": "outcomes:auto",
                "resolution_source": "outcomes",
                "notes": "no decisive winner — both participants resolved as non-win",
            }},
        )
    else:
        # Both wins on opposing stances should be impossible by the conflict
        # matrix; treat as ambiguous.
        await db[SHARED_CONFLICTS].update_one(
            {"conflict_id": conflict["conflict_id"]},
            {"$set": {
                "status": "stale",
                "resolved_at": _now_iso(),
                "resolved_by": "outcomes:auto",
                "resolution_source": "outcomes",
                "notes": "ambiguous — both participants resolved as wins (logic error?)",
            }},
        )

    return await db[SHARED_CONFLICTS].find_one(
        {"conflict_id": conflict["conflict_id"]}, {"_id": 0}
    )


async def attempt_resolve_conflicts_for_opinion(opinion_id: str) -> list[dict]:
    """Called from outcomes.post_outcome / post_outcome_admin after an
    outcome is attached. Looks for any open conflicts that include this
    opinion and tries to auto-resolve them."""
    try:
        conflicts = await db[SHARED_CONFLICTS].find(
            {"pair_ids": opinion_id, "status": "open"}, {"_id": 0}
        ).to_list(50)
        resolved: list[dict] = []
        for c in conflicts:
            r = await _try_auto_resolve(c)
            if r and r["status"] != "open":
                resolved.append(r)
        return resolved
    except Exception as e:  # noqa: BLE001
        from logging import getLogger
        getLogger("risedual.conflicts").warning("auto-resolve failed: %s", e)
        return []


# ──────────────────────── HTTP API ────────────────────────

router = APIRouter(tags=["conflicts"])


@router.get("/shared/conflicts")
async def list_conflicts(
    status: Optional[str] = Query(None, description="open|resolved|stale"),
    runtime: Optional[str] = Query(None, description="filter to conflicts where this brain participated"),
    topic: Optional[str] = Query(None),
    since: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    _user: dict = Depends(get_current_user),
):
    if status and status not in ("open", "resolved", "stale"):
        raise HTTPException(status_code=400, detail="status must be open|resolved|stale")
    if runtime and runtime not in DISCUSSION_PARTICIPANTS:
        raise HTTPException(status_code=400, detail=f"runtime must be one of {DISCUSSION_PARTICIPANTS}")

    q: dict = {}
    if status:
        q["status"] = status
    if topic:
        q["topic"] = topic
    if since:
        q["detected_at"] = {"$gt": since}
    if runtime:
        q["participants.runtime"] = runtime

    items = await (
        db[SHARED_CONFLICTS]
        .find(q, {"_id": 0})
        .sort("detected_at", -1)
        .to_list(limit)
    )
    return {"items": items, "count": len(items)}


@router.get("/shared/conflicts/pair-scorecard")
async def pair_scorecard(
    a: str = Query(..., description="first runtime"),
    b: str = Query(..., description="second runtime"),
    _user: dict = Depends(get_current_user),
):
    """X-vs-Y scorecard: of resolved conflicts where these two brains
    disagreed, who was right and how often?

    Also returns a `temperature` block — how often this pair has been in
    opposing stances over rolling windows. Friction, not skill. A pair
    that fights a lot AND has a clear winner is where to focus learning;
    a pair that fights a lot with no clear winner is where the doctrine
    itself may need rethinking.
    """
    if a not in DISCUSSION_PARTICIPANTS or b not in DISCUSSION_PARTICIPANTS:
        raise HTTPException(
            status_code=400,
            detail=f"a and b must be in {DISCUSSION_PARTICIPANTS}",
        )
    if a == b:
        raise HTTPException(status_code=400, detail="a and b must differ")

    items = await db[SHARED_CONFLICTS].find(
        {
            "status": "resolved",
            "$and": [
                {"participants.runtime": a},
                {"participants.runtime": b},
            ],
        },
        {"_id": 0},
    ).to_list(2000)

    a_wins = sum(1 for c in items if c.get("winner") == a)
    b_wins = sum(1 for c in items if c.get("winner") == b)
    decisive = a_wins + b_wins

    # ── Temperature: count ALL conflicts (any status) for this pair over
    # rolling windows. Excludes "stale" from decisive counts but includes
    # them in raw conflict counts because friction == "they fought",
    # regardless of who won.
    now = datetime.now(timezone.utc)
    windows = {
        "24h": (now - timedelta(hours=24)).isoformat(),
        "7d":  (now - timedelta(days=7)).isoformat(),
        "30d": (now - timedelta(days=30)).isoformat(),
    }
    temperature: dict = {}
    for label, since_iso in windows.items():
        window_q = {
            "$and": [
                {"participants.runtime": a},
                {"participants.runtime": b},
                {"detected_at": {"$gte": since_iso}},
            ],
        }
        n = await db[SHARED_CONFLICTS].count_documents(window_q)
        decisive_n = await db[SHARED_CONFLICTS].count_documents(
            {**window_q, "status": "resolved"}
        )
        temperature[label] = {
            "conflicts": n,
            "decisive": decisive_n,
            "stale_or_open": n - decisive_n,
        }

    # Heat band based on 7d frequency. Calibrated to be useful at low N
    # without screaming "hot" the moment two brains disagree once.
    seven_d = temperature["7d"]["conflicts"]
    if seven_d == 0:
        heat = "cold"
    elif seven_d < 3:
        heat = "cool"
    elif seven_d < 8:
        heat = "warm"
    elif seven_d < 20:
        heat = "hot"
    else:
        heat = "blazing"

    return {
        "pair": [a, b],
        "decisive": decisive,
        "a_wins": a_wins,
        "b_wins": b_wins,
        "a_win_rate": round(a_wins / decisive, 4) if decisive else None,
        "b_win_rate": round(b_wins / decisive, 4) if decisive else None,
        "recent": items[:25],
        "temperature": temperature,
        "heat": heat,
        "doctrine": (
            "Pair scorecards are descriptive. They do not modify any "
            "brain's authority. Communication is unrestricted; trading is "
            "off the table until operator consent."
        ),
    }


@router.get("/shared/conflicts/{conflict_id}")
async def get_conflict(
    conflict_id: str,
    _user: dict = Depends(get_current_user),
):
    doc = await db[SHARED_CONFLICTS].find_one({"conflict_id": conflict_id}, {"_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail="conflict not found")
    return doc


# ──────────────────────── manual resolution ────────────────────────

class ResolveBody(BaseModel):
    winner: str = Field(..., description="runtime that was right; must be a participant")
    notes: str = Field("", max_length=2048)


@router.post("/admin/conflicts/{conflict_id}/resolve")
async def resolve_conflict(
    conflict_id: str,
    body: ResolveBody,
    user: dict = Depends(get_current_user),
):
    doc = await db[SHARED_CONFLICTS].find_one({"conflict_id": conflict_id}, {"_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail="conflict not found")
    if doc["status"] != "open":
        raise HTTPException(status_code=409, detail=f"conflict already {doc['status']}")

    participant_runtimes = [p["runtime"] for p in doc["participants"]]
    if body.winner not in participant_runtimes:
        raise HTTPException(
            status_code=400,
            detail=f"winner must be one of the participants {participant_runtimes}",
        )

    winning_oid = next(
        p["opinion_id"] for p in doc["participants"] if p["runtime"] == body.winner
    )
    await db[SHARED_CONFLICTS].update_one(
        {"conflict_id": conflict_id},
        {"$set": {
            "status": "resolved",
            "winner": body.winner,
            "winning_opinion_id": winning_oid,
            "resolved_at": _now_iso(),
            "resolved_by": user.get("email") or "operator",
            "resolution_source": "manual",
            "notes": body.notes,
        }},
    )
    updated = await db[SHARED_CONFLICTS].find_one({"conflict_id": conflict_id}, {"_id": 0})
    return updated

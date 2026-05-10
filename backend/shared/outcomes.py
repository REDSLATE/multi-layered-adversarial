"""Role scoring (Step 2 of the cross-brain training plan).

Doctrine:
    Share evidence and outcomes; each stack learns role-specific lessons;
    no stack rewrites another's authority.

This module:
  - lets the operator (or Chevelle, the auditor) attach an outcome to an
    opinion (append-only; no re-resolution in v0).
  - emits a role-specific scorecard for each brain — schema-encoded so a
    brain literally cannot read another brain's metrics via the runtime
    endpoint (operator JWT path remains unfiltered for visibility).
  - never gates promotions. Scorecards are descriptive, not prescriptive.

A brain may not resolve its own opinions. Chevelle is the auditor and may
resolve any opinion EXCEPT its own (no brain self-grades, even the auditor).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field

from auth import get_current_user
from db import db
from namespaces import (
    DISCUSSION_PARTICIPANTS,
    SHARED_OPINIONS,
    SHARED_OUTCOMES,
)
from runtime_auth import verify_runtime_token


# ──────────────────────── config ────────────────────────

ACTUAL_VALUES = ("win", "loss", "no-event", "ambiguous")

# Stances that count as a directional call worth scoring per role.
ROLE_STANCES: dict[str, tuple[str, ...]] = {
    "alpha":    ("long",),                  # Alpha is judged on longs only
    "redeye":   ("short",),                 # REDEYE is judged on shorts only
    "camaro":   ("endorse", "veto", "observation"),  # judgement calls
    "chevelle": ("observation", "veto"),    # source-reliability rulings
}


# ──────────────────────── outcome ingest ────────────────────────

class OutcomeIn(BaseModel):
    opinion_id: str
    actual: Literal["win", "loss", "no-event", "ambiguous"]
    notes: str = Field("", max_length=2048)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


router = APIRouter(tags=["scoring"])


@router.post("/ingest/outcome")
async def post_outcome(
    body: OutcomeIn,
    x_runtime_token: str | None = Header(default=None, alias="X-Runtime-Token"),
):
    """Attach an outcome to an opinion via X-Runtime-Token (Chevelle only).

    Auth model:
      - This endpoint accepts only Chevelle (the auditor) via X-Runtime-Token.
      - Operators use POST /api/admin/outcome instead (JWT-authenticated).

    Append-only: one outcome per opinion. 409 on second attempt.
    Chevelle may not resolve its own opinions (no brain self-grades).
    """
    if not x_runtime_token:
        raise HTTPException(
            status_code=401,
            detail="X-Runtime-Token required (chevelle); operators use /api/admin/outcome",
        )
    verify_runtime_token("chevelle", x_runtime_token)
    resolved_by = "chevelle"

    opinion = await db[SHARED_OPINIONS].find_one(
        {"opinion_id": body.opinion_id}, {"_id": 0}
    )
    if not opinion:
        raise HTTPException(status_code=404, detail="opinion not found")

    if opinion.get("runtime") == "chevelle":
        raise HTTPException(
            status_code=403,
            detail="chevelle may not resolve its own opinions (no brain self-grades)",
        )

    existing = await db[SHARED_OUTCOMES].find_one(
        {"opinion_id": body.opinion_id}, {"_id": 0}
    )
    if existing:
        raise HTTPException(
            status_code=409,
            detail="opinion already resolved; outcomes are append-only in v0",
        )

    doc = {
        "outcome_id": str(uuid.uuid4()),
        "opinion_id": body.opinion_id,
        "runtime": opinion["runtime"],
        "topic": opinion["topic"],
        "stance": opinion["stance"],
        "confidence": float(opinion.get("confidence", 0.5)),
        "posted_at": opinion["posted_at"],
        "resolved_at": _now_iso(),
        "resolved_by": resolved_by,
        "actual": body.actual,
        "notes": body.notes,
    }
    await db[SHARED_OUTCOMES].insert_one(doc)

    # Outcome attached → try to auto-resolve any conflicts that include
    # this opinion. Never blocks the resolve.
    from shared.conflicts import attempt_resolve_conflicts_for_opinion  # noqa: WPS433
    auto_resolved = await attempt_resolve_conflicts_for_opinion(body.opinion_id)

    return {
        "ok": True,
        "outcome_id": doc["outcome_id"],
        "opinion_id": body.opinion_id,
        "runtime": doc["runtime"],
        "actual": body.actual,
        "auto_resolved_conflicts": [c["conflict_id"] for c in auto_resolved],
    }


# Operator-JWT variant of /ingest/outcome — same behaviour, alternate path so
# we can keep the runtime-token branch above unauthenticated for chevelle.
@router.post("/admin/outcome")
async def post_outcome_admin(
    body: OutcomeIn,
    user: dict = Depends(get_current_user),
):
    opinion = await db[SHARED_OPINIONS].find_one(
        {"opinion_id": body.opinion_id}, {"_id": 0}
    )
    if not opinion:
        raise HTTPException(status_code=404, detail="opinion not found")
    existing = await db[SHARED_OUTCOMES].find_one(
        {"opinion_id": body.opinion_id}, {"_id": 0}
    )
    if existing:
        raise HTTPException(
            status_code=409,
            detail="opinion already resolved; outcomes are append-only in v0",
        )
    doc = {
        "outcome_id": str(uuid.uuid4()),
        "opinion_id": body.opinion_id,
        "runtime": opinion["runtime"],
        "topic": opinion["topic"],
        "stance": opinion["stance"],
        "confidence": float(opinion.get("confidence", 0.5)),
        "posted_at": opinion["posted_at"],
        "resolved_at": _now_iso(),
        "resolved_by": user.get("email") or "operator",
        "actual": body.actual,
        "notes": body.notes,
    }
    await db[SHARED_OUTCOMES].insert_one(doc)

    from shared.conflicts import attempt_resolve_conflicts_for_opinion  # noqa: WPS433
    auto_resolved = await attempt_resolve_conflicts_for_opinion(body.opinion_id)

    return {
        "ok": True,
        "outcome_id": doc["outcome_id"],
        "opinion_id": body.opinion_id,
        "runtime": doc["runtime"],
        "actual": body.actual,
        "auto_resolved_conflicts": [c["conflict_id"] for c in auto_resolved],
    }


# ──────────────────────── scorecard math ────────────────────────

def _hit_rate(rows: list[dict]) -> dict:
    """For SHORT/LONG-style stances, "win" counts as a hit. Excludes
    no-event/ambiguous from the denominator so the rate isn't diluted by
    rows the operator couldn't grade."""
    decisive = [r for r in rows if r["actual"] in ("win", "loss")]
    wins = [r for r in decisive if r["actual"] == "win"]
    losses = [r for r in decisive if r["actual"] == "loss"]
    no_event = [r for r in rows if r["actual"] == "no-event"]
    ambiguous = [r for r in rows if r["actual"] == "ambiguous"]
    return {
        "total_resolved": len(rows),
        "decisive": len(decisive),
        "wins": len(wins),
        "losses": len(losses),
        "no_event": len(no_event),
        "ambiguous": len(ambiguous),
        "hit_rate": (len(wins) / len(decisive)) if decisive else None,
        "avg_confidence_on_wins": (
            sum(r["confidence"] for r in wins) / len(wins) if wins else None
        ),
        "avg_confidence_on_losses": (
            sum(r["confidence"] for r in losses) / len(losses) if losses else None
        ),
    }


def _brier(rows: list[dict]) -> Optional[float]:
    """Brier score — lower is better. Map win→1, loss→0; skip the rest."""
    decisive = [r for r in rows if r["actual"] in ("win", "loss")]
    if not decisive:
        return None
    total = 0.0
    for r in decisive:
        outcome_bin = 1.0 if r["actual"] == "win" else 0.0
        total += (float(r["confidence"]) - outcome_bin) ** 2
    return round(total / len(decisive), 4)


def _confidence_calibration_buckets(rows: list[dict]) -> list[dict]:
    """Bucket by confidence in 0.1 bands; show actual win-rate per band."""
    decisive = [r for r in rows if r["actual"] in ("win", "loss")]
    if not decisive:
        return []
    buckets: dict[int, list[dict]] = {}
    for r in decisive:
        b = min(int(float(r["confidence"]) * 10), 9)
        buckets.setdefault(b, []).append(r)
    out: list[dict] = []
    for b in sorted(buckets):
        items = buckets[b]
        wins = sum(1 for x in items if x["actual"] == "win")
        out.append({
            "confidence_band": f"{b/10:.1f}-{(b+1)/10:.1f}",
            "n": len(items),
            "win_rate": round(wins / len(items), 4),
        })
    return out


def _alpha_alignment_breakdown(rows: list[dict]) -> dict:
    """REDEYE-only — group outcomes by the alpha_alignment hint that REDEYE
    emitted at posting time. Pulled from the source opinion's evidence."""
    # We need each opinion's evidence to read alpha_alignment. The outcome
    # row carries opinion_id so the caller will pre-join evidence.
    out: dict[str, dict] = {}
    for r in rows:
        align = r.get("_alpha_alignment", "null") or "null"
        bucket = out.setdefault(align, {"wins": 0, "losses": 0, "no_event": 0, "ambiguous": 0})
        if r["actual"] == "win":
            bucket["wins"] += 1
        elif r["actual"] == "loss":
            bucket["losses"] += 1
        elif r["actual"] == "no-event":
            bucket["no_event"] += 1
        else:
            bucket["ambiguous"] += 1
    # Add hit_rate per bucket
    for align, b in out.items():
        decisive = b["wins"] + b["losses"]
        b["hit_rate"] = round(b["wins"] / decisive, 4) if decisive else None
    return out


def _topic_breakdown(rows: list[dict]) -> list[dict]:
    """Chevelle-style source/topic reliability — group by topic prefix."""
    by_topic: dict[str, list[dict]] = {}
    for r in rows:
        by_topic.setdefault(r["topic"], []).append(r)
    out = []
    for topic, items in by_topic.items():
        decisive = [r for r in items if r["actual"] in ("win", "loss")]
        wins = sum(1 for x in decisive if x["actual"] == "win")
        out.append({
            "topic": topic,
            "n": len(items),
            "decisive": len(decisive),
            "wins": wins,
            "hit_rate": round(wins / len(decisive), 4) if decisive else None,
        })
    out.sort(key=lambda x: x["n"], reverse=True)
    return out[:50]


# ──────────────────────── scorecard builder ────────────────────────

async def _gather_rows(runtime: str, since: Optional[str]) -> list[dict]:
    """Pull outcomes for this runtime, joined with each opinion's evidence
    so role-specific aggregators (e.g. REDEYE alpha_alignment) work."""
    q: dict = {"runtime": runtime}
    if since:
        q["resolved_at"] = {"$gt": since}
    # Limit to stances scored for this role.
    stances = ROLE_STANCES.get(runtime, ())
    if stances:
        q["stance"] = {"$in": list(stances)}
    rows = await db[SHARED_OUTCOMES].find(q, {"_id": 0}).to_list(2000)
    if runtime == "redeye":
        # Hydrate alpha_alignment from each source opinion's evidence
        ids = [r["opinion_id"] for r in rows]
        if ids:
            opinions = await db[SHARED_OPINIONS].find(
                {"opinion_id": {"$in": ids}}, {"_id": 0, "opinion_id": 1, "evidence": 1}
            ).to_list(len(ids))
            ev_by_id = {o["opinion_id"]: (o.get("evidence") or {}) for o in opinions}
            for r in rows:
                r["_alpha_alignment"] = (ev_by_id.get(r["opinion_id"]) or {}).get(
                    "alpha_alignment"
                )
    return rows


def _build_scorecard(runtime: str, rows: list[dict]) -> dict:
    base = {
        "runtime": runtime,
        "role_stances": list(ROLE_STANCES.get(runtime, ())),
        "summary": _hit_rate(rows),
        "brier": _brier(rows),
        "calibration_bands": _confidence_calibration_buckets(rows),
        "doctrine": (
            "Descriptive only. Scorecards do not gate promotions. No brain "
            "may rewrite another brain's authority based on a scorecard."
        ),
    }
    if runtime == "alpha":
        base["lens"] = "longs"
        base["question_answered"] = "When am I good at longs?"
    elif runtime == "redeye":
        base["lens"] = "shorts"
        base["question_answered"] = "When am I good at shorts?"
        base["alpha_alignment_breakdown"] = _alpha_alignment_breakdown(rows)
    elif runtime == "camaro":
        base["lens"] = "judgement_calls"
        base["question_answered"] = (
            "When should I trust, reduce, veto, or execute? "
            "(Execution remains gated; this only grades the calls.)"
        )
        # Camaro-specific: break down by stance so the operator sees
        # endorse-vs-veto-vs-observation hit rates separately.
        by_stance: dict[str, list[dict]] = {}
        for r in rows:
            by_stance.setdefault(r["stance"], []).append(r)
        base["per_stance"] = {s: _hit_rate(items) for s, items in by_stance.items()}
    elif runtime == "chevelle":
        base["lens"] = "source_reliability"
        base["question_answered"] = "Which outside signals are reliable?"
        base["topic_breakdown"] = _topic_breakdown(rows)
    else:
        base["lens"] = "generic"
    return base


# ──────────────────────── scorecard endpoints ────────────────────────

@router.get("/shared/scorecard")
async def operator_scorecard(
    runtime: str = Query(..., description="alpha|camaro|chevelle|redeye"),
    since: Optional[str] = Query(None),
    _user: dict = Depends(get_current_user),
):
    if runtime not in DISCUSSION_PARTICIPANTS:
        raise HTTPException(
            status_code=400,
            detail=f"runtime must be one of {DISCUSSION_PARTICIPANTS}",
        )
    rows = await _gather_rows(runtime, since)
    return _build_scorecard(runtime, rows)


@router.get("/runtime-discussion/scorecard")
async def runtime_scorecard(
    runtime_caller: str = Query(..., alias="caller"),
    since: Optional[str] = Query(None),
    x_runtime_token: str | None = Header(default=None, alias="X-Runtime-Token"),
):
    """A brain reads its OWN scorecard. Token-vs-caller mismatch returns
    401 (Alpha cannot pull Camaro's scorecard via Alpha's token).

    There is no `runtime=` query param here — schema-level enforcement that
    a brain only ever sees its own role-specific lens.
    """
    verify_runtime_token(runtime_caller, x_runtime_token or "")
    if runtime_caller not in DISCUSSION_PARTICIPANTS:
        raise HTTPException(
            status_code=400,
            detail=f"runtime must be one of {DISCUSSION_PARTICIPANTS}",
        )
    rows = await _gather_rows(runtime_caller, since)
    return _build_scorecard(runtime_caller, rows)

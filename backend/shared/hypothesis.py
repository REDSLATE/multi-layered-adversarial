"""Hypothesis engine — pure brain-content recall (no external LLMs).

DOCTRINE (per operator, 2026-02-14):
  * No outside AIs. Strategist + Auditor narratives come ONLY from the
    four brains' own pushes into MC (intents, opinions, outcomes) and
    each brain's own Shelly memory store.
  * Strategist = brain currently holding the EXECUTOR seat.
  * Auditor   = brain currently holding the AUDITOR seat.
  * Both brains "explain based on the memories of similar situations":
    we surface that brain's Shelly memories that mention the symbol
    alongside their latest intent + discussion stance.
  * NO LLM calls. Operator search is a fast aggregate query (<200ms).

What each role's narrative is composed of:
  - latest_intent     — most recent shared_intents row by that brain on
                        this symbol (action, confidence, rationale,
                        evidence, gate_state)
  - latest_opinion    — most recent shared_brain_opinions row by that
                        brain on topic="symbol:<S>" (stance + body)
  - shelly_memories   — that brain's labeled-memory entries
                        (shared_labeled_memories) that reference the
                        symbol; labels: safe / review / quarantine
  - track_record      — that brain's resolved outcomes
                        (shared_brain_outcomes) attached to opinions
                        they posted on this symbol (W/L count + last 5)
  - similar_setups    — historical intents by that brain on OTHER
                        symbols that match this symbol's CURRENT
                        indicator regime (RSI band, MACD sign, BB
                        position) — naive bucketing, not embeddings

If a brain has zero data on the symbol, the card surfaces "no recent
stance" and points at /admin/discussion.

PERFORMANCE (2026-02-15 sprint):
  * Role builds run concurrently via asyncio.gather (saves ~half the
    role-build wall time vs sequential)
  * Mongo indexes added in db.ensure_indexes() — see that file
  * Audit-log insert is fire-and-forget (asyncio.create_task) — caller
    never waits on bookkeeping
  * Every analysis records `latency_ms` for p50/p95/p99 monitoring at
    GET /api/hypothesis/_perf
"""
from __future__ import annotations

import asyncio
import re
import time
import uuid
from datetime import datetime, timezone
from statistics import mean, stdev
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from auth import get_current_user
from db import db
from namespaces import (
    HYPOTHESIS_ANALYSES,
    SHARED_INDICATOR_SNAPSHOTS,
    SHARED_INTENTS,
    SHARED_MEMORY,
    SHARED_OPINIONS,
    SHARED_OUTCOMES,
    SHARED_POSITIONS,
)
from shared.auditor_seat import get_auditor_holder
from shared.regime_keys import (  # canonical regime/crypto primitives (cycle broken 2026-05-18)
    REGIME_FP_KEYS as _REGIME_FP_KEYS_CANONICAL,
    _looks_like_crypto as _is_crypto_symbol,
    _regime_fingerprint as _regime_fingerprint_canonical,
)
# from shared.executor_seat import get_executor_holder  # superseded by lane-aware lookup 2026-02-16


router = APIRouter(prefix="/hypothesis", tags=["hypothesis"])


_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
MAX_INTENTS_PER_BRAIN = 6           # last N intents by brain on this symbol
MAX_OPINIONS_PER_BRAIN = 4          # last N opinions
MAX_SHELLY_PER_BRAIN = 8            # labeled memories that mention symbol
MAX_SIMILAR_SETUPS = 5              # past intents on OTHER symbols in same regime


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ───────────────────────────── regime fingerprinting ─────────────────────────────

# Canonical impls now live in `shared/regime_keys.py` (2026-05-18) to
# break the intents.py ↔ hypothesis.py import cycle. We re-export the
# old names so out-of-tree callers that did
# `from shared.hypothesis import REGIME_FP_KEYS` keep working.
REGIME_FP_KEYS = _REGIME_FP_KEYS_CANONICAL
_regime_fingerprint = _regime_fingerprint_canonical


# ───────────────────────────── per-role aggregation ─────────────────────────────

async def _shelly_memories(brain: str, symbol: str) -> list[dict]:
    """Pull labeled-memory entries for `brain` mentioning `symbol`.

    Prefer the text index over regex (created in db.ensure_indexes).
    If the text index isn't present yet for any reason, fall back to
    case-insensitive regex on payload_summary + reason. Both paths
    return the same shape."""
    try:
        return await db[SHARED_MEMORY].find(
            {
                "runtime": brain,
                "$text": {"$search": symbol},
            },
            {"_id": 0, "id": 1, "label": 1, "reason": 1,
             "payload_summary": 1, "timestamp": 1},
        ).sort("timestamp", -1).to_list(MAX_SHELLY_PER_BRAIN)
    except Exception:  # noqa: BLE001 - text index missing → regex fallback
        return await db[SHARED_MEMORY].find(
            {
                "runtime": brain,
                "$or": [
                    {"payload_summary": {"$regex": rf"\b{re.escape(symbol)}\b", "$options": "i"}},
                    {"reason": {"$regex": rf"\b{re.escape(symbol)}\b", "$options": "i"}},
                ],
            },
            {"_id": 0, "id": 1, "label": 1, "reason": 1,
             "payload_summary": 1, "timestamp": 1},
        ).sort("timestamp", -1).to_list(MAX_SHELLY_PER_BRAIN)


async def _build_role(brain: Optional[str], symbol: str, regime_fp: dict) -> dict:
    """Aggregate everything we have for one brain on one symbol.

    Returns a dict ready for the UI to render. If `brain` is None, the
    seat is empty and we return a stub card."""
    if not brain:
        return {
            "brain": None,
            "seat_empty": True,
            "latest_intent": None,
            "latest_opinion": None,
            "shelly_memories": [],
            "track_record": {"wins": 0, "losses": 0, "open": 0, "items": []},
            "similar_setups": [],
            "summary": "Seat is empty. Rotate a brain into this seat to surface its stance.",
        }

    # 2026-02-23 dual-field migration — canonical-aware intent
    # queries so legacy + canonical historical docs come back as
    # one brain's track record.
    from shared.brain_legend import canonicalize_stack as _canon  # noqa: WPS433
    brain_c = _canon(brain) or brain

    # The first three queries are independent — run them concurrently.
    # outcomes + similar_setups depend on the opinions/regime results,
    # so they fan out in a second wave.
    intents_task = db[SHARED_INTENTS].find(
        {"stack_canonical": brain_c, "symbol": symbol},
        {"_id": 0, "intent_id": 1, "action": 1, "confidence": 1, "rationale": 1,
         "evidence": 1, "regime": 1, "gate_state": 1, "executed": 1,
         "executed_at": 1, "ingest_ts": 1, "risk_multiplier": 1},
    ).sort("ingest_ts", -1).to_list(MAX_INTENTS_PER_BRAIN)

    opinions_task = db[SHARED_OPINIONS].find(
        {"runtime": brain, "topic": f"symbol:{symbol}"},
        {"_id": 0, "opinion_id": 1, "stance": 1, "confidence": 1, "body": 1,
         "evidence": 1, "posted_at": 1, "thread_root": 1},
    ).sort("posted_at", -1).to_list(MAX_OPINIONS_PER_BRAIN)

    # Shelly memories — use $text if a text index exists (much faster
    # than regex), fall back to regex if the index isn't ready yet.
    shelly_task = _shelly_memories(brain, symbol)

    intents, opinions, shelly = await asyncio.gather(
        intents_task, opinions_task, shelly_task,
    )
    latest_intent = intents[0] if intents else None
    latest_opinion = opinions[0] if opinions else None

    # ─── track record (resolved outcomes attached to this brain's opinions on this symbol) ─
    # Second wave — outcomes (depend on opinion ids) + similar setups
    # (depend on regime_fp). They're independent of each other and can
    # be gathered.
    opinion_ids = [o["opinion_id"] for o in opinions]

    async def _outcomes_query():
        if not opinion_ids:
            return []
        return await db[SHARED_OUTCOMES].find(
            {"opinion_id": {"$in": opinion_ids}},
            {"_id": 0, "opinion_id": 1, "outcome": 1, "resolved_at": 1,
             "resolved_by": 1, "rationale": 1},
        ).sort("resolved_at", -1).to_list(20)

    async def _similar_query():
        if not regime_fp:
            return []
        and_clauses = [
            {"$or": [{f"evidence.regime_fp.{k}": v}, {f"evidence.{k}": v}]}
            for k, v in regime_fp.items()
        ]
        q: dict = {"stack_canonical": brain_c, "symbol": {"$ne": symbol}, "executed": True}
        if and_clauses:
            q["$and"] = and_clauses
        return await db[SHARED_INTENTS].find(
            q,
            {"_id": 0, "intent_id": 1, "symbol": 1, "action": 1,
             "confidence": 1, "rationale": 1, "executed_at": 1},
        ).sort("executed_at", -1).to_list(MAX_SIMILAR_SETUPS)

    outcomes, similar = await asyncio.gather(_outcomes_query(), _similar_query())

    wins = losses = open_ = 0
    for o in outcomes:
        v = (o.get("outcome") or "").lower()
        if v in ("win", "correct", "good"):
            wins += 1
        elif v in ("loss", "wrong", "bad"):
            losses += 1
        else:
            open_ += 1
    track_items = outcomes[:5]
    track_record = {"wins": wins, "losses": losses, "open": open_,
                    "items": track_items}

    return {
        "brain": brain,
        "seat_empty": False,
        "latest_intent": latest_intent,
        "intents_history": intents,
        "latest_opinion": latest_opinion,
        "opinions_history": opinions,
        "shelly_memories": shelly,
        "track_record": track_record,
        "similar_setups": similar,
        "summary": _build_role_summary(brain, latest_intent, latest_opinion,
                                       len(shelly), track_record),
    }


def _build_role_summary(brain: str, intent: dict | None, opinion: dict | None,
                        shelly_count: int, track: dict) -> str:
    """A one-liner header rendered above the card body. Pure string
    composition — no LLM. Lets the operator scan 4-6 brains/symbols
    quickly without expanding sections."""
    bits: list[str] = []
    if intent:
        action = intent.get("action", "—")
        conf = intent.get("confidence")
        confs = f"{round(conf * 100)}%" if isinstance(conf, (int, float)) else "—"
        bits.append(f"latest intent: {action} @ {confs}")
    elif opinion:
        bits.append(f"latest opinion: {opinion.get('stance', '—')}")
    else:
        return f"{brain.upper()} has no recent stance on this symbol."
    if shelly_count:
        bits.append(f"{shelly_count} memory hit{'s' if shelly_count != 1 else ''}")
    wl = track["wins"] + track["losses"]
    if wl:
        bits.append(f"track: {track['wins']}W / {track['losses']}L")
    return " · ".join(bits)


# ───────────────────────────── routes ─────────────────────────────

class AnalyzeBody(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=10)

    @field_validator("symbol")
    @classmethod
    def _norm(cls, v: str) -> str:
        v = (v or "").strip().upper()
        if not _SYMBOL_RE.match(v):
            raise ValueError(
                "symbol must be 1-10 chars, uppercase letters/digits/. /- only"
            )
        return v


@router.post("/analyze")
async def analyze(
    body: AnalyzeBody,
    user: dict = Depends(get_current_user),  # noqa: B008
):
    """Operator-triggered. Pure aggregation — no external LLM calls.

    Strategist card = brain holding the Executor seat.
    Auditor card   = brain holding the Auditor seat.
    Both surface that brain's own intent/opinion/Shelly-memory data on
    the symbol, plus similar past setups by regime match.

    Performance: role builds parallelised; audit-log insert is
    fire-and-forget; `latency_ms` is stamped on every row for the
    /_perf p50/p95/p99 report.
    """
    t0 = time.perf_counter()
    symbol = body.symbol

    # 1. Symbol-level context (snapshot + open positions) — these don't
    #    depend on which brain holds the seats, so run in parallel.
    snap_task = db[SHARED_INDICATOR_SNAPSHOTS].find_one(
        {"symbol": symbol}, {"_id": 0},
        sort=[("captured_at", -1)],
    )
    open_positions_task = db[SHARED_POSITIONS].find(
        {"symbol": symbol, "state": {"$in": ["open", "managing"]}},
        {"_id": 0, "position_id": 1, "direction": 1, "state": 1, "updated_at": 1},
    ).sort("updated_at", -1).to_list(5)

    # Strategist = the lane-appropriate execute-seat holder. Equity
    # symbols ask "who holds equity executor?", crypto symbols ask
    # "who holds the crypto seat?". 2026-02-16: previously equity-only,
    # which meant crypto hypotheses were built around Alpha's lens
    # rather than REDEYE's.
    from shared.executor_seat import get_seat_holder, seats_with_execute  # noqa: WPS433
    inferred_lane = "crypto" if _is_crypto_symbol(symbol) else "equity"

    async def _lane_strategist() -> str | None:
        for seat in seats_with_execute(inferred_lane):
            h = await get_seat_holder(seat)
            if h:
                return h
        return None

    strategist_brain_task = _lane_strategist()
    auditor_brain_task = get_auditor_holder()

    snap, open_positions, strategist_brain, auditor_brain = await asyncio.gather(
        snap_task, open_positions_task, strategist_brain_task, auditor_brain_task,
    )

    indicators = (snap or {}).get("indicators") or {}
    regime_fp = _regime_fingerprint(indicators)

    # 2. The two role builds are independent — run them concurrently.
    #    This is the biggest single perf win (≈40-60ms shaved).
    strategist, auditor = await asyncio.gather(
        _build_role(strategist_brain, symbol, regime_fp),
        _build_role(auditor_brain, symbol, regime_fp),
    )

    latency_ms = round((time.perf_counter() - t0) * 1000.0, 1)
    analysis_id = str(uuid.uuid4())
    now = _now_iso()
    row = {
        "analysis_id": analysis_id,
        "symbol": symbol,
        "generated_at": now,
        "requested_by": user.get("email"),
        "seats": {
            "strategist_brain": strategist_brain,
            "auditor_brain": auditor_brain,
        },
        "regime_fp": regime_fp,
        "latency_ms": latency_ms,
        "had_market_context": snap is not None,
    }
    # 3. Fire-and-forget audit insert. The caller does NOT wait on it —
    #    if Mongo hiccups the decision still returns. Worst case: one
    #    audit row lost; the analysis itself was visible to the
    #    operator on screen.
    async def _persist():
        try:
            await db[HYPOTHESIS_ANALYSES].insert_one(row)
        except Exception:  # noqa: BLE001 - audit-row loss is acceptable
            pass
    asyncio.create_task(_persist())

    return {
        "analysis_id": analysis_id,
        "symbol": symbol,
        "generated_at": now,
        "latency_ms": latency_ms,
        "seats": {
            "strategist_brain": strategist_brain,
            "auditor_brain": auditor_brain,
        },
        "strategist": strategist,
        "auditor": auditor,
        "context": {
            "indicator_snapshot": snap,
            "regime_fp": regime_fp,
            "open_positions": open_positions,
            "has_market_context": snap is not None,
        },
    }


@router.get("/recent")
async def list_recent(
    limit: int = 25,
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    rows = (
        await db[HYPOTHESIS_ANALYSES]
        .find({}, {"_id": 0})
        .sort("generated_at", -1)
        .to_list(min(max(1, limit), 200))
    )
    return {"items": rows, "count": len(rows)}


@router.get("/_perf")
async def perf_audit(
    hours: int = Query(default=4, ge=1, le=168),
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    """Latency audit over the last N hours of /hypothesis/analyze calls.

    Returns p50/p95/p99 + std + healthy/unhealthy flag. Recommended
    targets: p95 < 250ms, p99 < 400ms (see ops doc). The threshold
    flips to TAIL_LATENCY_HIGH when any sample crosses 400ms.
    """
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    rows = await db[HYPOTHESIS_ANALYSES].find(
        {"generated_at": {"$gte": cutoff}, "latency_ms": {"$exists": True}},
        {"_id": 0, "latency_ms": 1, "symbol": 1, "generated_at": 1, "had_market_context": 1},
    ).to_list(5000)

    samples = [r["latency_ms"] for r in rows if isinstance(r.get("latency_ms"), (int, float))]
    if not samples:
        return {
            "window_hours": hours,
            "samples": 0,
            "msg": "No analyses recorded with latency_ms in this window yet. Run a few hypothesis searches.",
        }

    samples.sort()
    n = len(samples)
    def _p(pct: float) -> float:
        idx = min(n - 1, max(0, int(round(pct * n)) - 1))
        return samples[idx]

    p50 = _p(0.50)
    p95 = _p(0.95)
    p99 = _p(0.99)
    avg = mean(samples)
    sd = stdev(samples) if n > 1 else 0.0
    worst = samples[-1]

    health = "GOOD"
    notes: list[str] = []
    if p95 > 250:
        health = "WARNING"
        notes.append(f"p95={p95}ms exceeds 250ms target")
    if p99 > 400 or worst > 400:
        health = "TAIL_LATENCY_HIGH"
        notes.append(f"p99={p99}ms / max={worst}ms exceeds 400ms ceiling")

    by_context = {"with_context": [], "without_context": []}
    for r in rows:
        bucket = "with_context" if r.get("had_market_context") else "without_context"
        if isinstance(r.get("latency_ms"), (int, float)):
            by_context[bucket].append(r["latency_ms"])

    def _summary(arr: list[float]) -> dict:
        if not arr:
            return {"count": 0}
        return {
            "count": len(arr),
            "mean": round(mean(arr), 1),
            "max": round(max(arr), 1),
        }

    return {
        "window_hours": hours,
        "samples": n,
        "mean_ms": round(avg, 1),
        "std_ms": round(sd, 1),
        "p50_ms": p50,
        "p95_ms": p95,
        "p99_ms": p99,
        "max_ms": worst,
        "min_ms": samples[0],
        "health": health,
        "notes": notes,
        "by_context": {
            "with_context": _summary(by_context["with_context"]),
            "without_context": _summary(by_context["without_context"]),
        },
    }

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
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
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
from shared.executor_seat import get_executor_holder


router = APIRouter(prefix="/hypothesis", tags=["hypothesis"])


_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
MAX_INTENTS_PER_BRAIN = 6           # last N intents by brain on this symbol
MAX_OPINIONS_PER_BRAIN = 4          # last N opinions
MAX_SHELLY_PER_BRAIN = 8            # labeled memories that mention symbol
MAX_SIMILAR_SETUPS = 5              # past intents on OTHER symbols in same regime


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ───────────────────────────── regime fingerprinting ─────────────────────────────

def _regime_fingerprint(indicators: dict | None) -> dict:
    """Coarse buckets used to find 'similar past setups' across the
    brain's history. Naive on purpose — we want a 5-row recall, not a
    research-grade similarity search."""
    if not indicators:
        return {}
    fp: dict = {}
    rsi = indicators.get("rsi14")
    if isinstance(rsi, (int, float)):
        if rsi < 30:
            fp["rsi_band"] = "oversold"
        elif rsi < 45:
            fp["rsi_band"] = "weak"
        elif rsi <= 55:
            fp["rsi_band"] = "neutral"
        elif rsi <= 70:
            fp["rsi_band"] = "strong"
        else:
            fp["rsi_band"] = "overbought"
    macd = indicators.get("macd") or {}
    hist = macd.get("hist")
    if isinstance(hist, (int, float)):
        fp["macd_hist_sign"] = "positive" if hist > 0 else ("negative" if hist < 0 else "flat")
    bb = indicators.get("bb") or {}
    pos = bb.get("position")
    if isinstance(pos, (int, float)):
        if pos < 0.25:
            fp["bb_band"] = "lower"
        elif pos < 0.55:
            fp["bb_band"] = "mid_low"
        elif pos < 0.75:
            fp["bb_band"] = "mid_high"
        else:
            fp["bb_band"] = "upper"
    return fp


# ───────────────────────────── per-role aggregation ─────────────────────────────

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

    # ─── latest intent on this symbol ────────────────────────────────
    intents = await db[SHARED_INTENTS].find(
        {"stack": brain, "symbol": symbol},
        {"_id": 0, "intent_id": 1, "action": 1, "confidence": 1, "rationale": 1,
         "evidence": 1, "regime": 1, "gate_state": 1, "executed": 1,
         "executed_at": 1, "ingest_ts": 1, "risk_multiplier": 1},
    ).sort("ingest_ts", -1).to_list(MAX_INTENTS_PER_BRAIN)
    latest_intent = intents[0] if intents else None

    # ─── latest opinion on this symbol topic ─────────────────────────
    opinions = await db[SHARED_OPINIONS].find(
        {"runtime": brain, "topic": f"symbol:{symbol}"},
        {"_id": 0, "opinion_id": 1, "stance": 1, "confidence": 1, "body": 1,
         "evidence": 1, "posted_at": 1, "thread_root": 1},
    ).sort("posted_at", -1).to_list(MAX_OPINIONS_PER_BRAIN)
    latest_opinion = opinions[0] if opinions else None

    # ─── Shelly memories referencing this symbol ─────────────────────
    # Each brain's gated/labeled memory store. We surface a brain's own
    # memories that mention the symbol — these "back the play."
    shelly = await db[SHARED_MEMORY].find(
        {
            "runtime": brain,
            "$or": [
                {"payload_summary": {"$regex": rf"\b{re.escape(symbol)}\b", "$options": "i"}},
                {"reason": {"$regex": rf"\b{re.escape(symbol)}\b", "$options": "i"}},
            ],
        },
        {"_id": 0, "id": 1, "label": 1, "reason": 1, "payload_summary": 1, "timestamp": 1},
    ).sort("timestamp", -1).to_list(MAX_SHELLY_PER_BRAIN)

    # ─── track record (resolved outcomes attached to this brain's opinions on this symbol) ─
    opinion_ids = [o["opinion_id"] for o in opinions]
    track_items: list[dict] = []
    wins = losses = open_ = 0
    if opinion_ids:
        outcomes = await db[SHARED_OUTCOMES].find(
            {"opinion_id": {"$in": opinion_ids}},
            {"_id": 0, "opinion_id": 1, "outcome": 1, "resolved_at": 1,
             "resolved_by": 1, "rationale": 1},
        ).sort("resolved_at", -1).to_list(20)
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

    # ─── similar setups (this brain's past intents on OTHER symbols matching current regime) ─
    similar: list[dict] = []
    if regime_fp:
        # naive: bucket-match against intent.evidence.regime_fp OR
        # intent.regime if the brain reports it. We use evidence first.
        and_clauses: list[dict] = []
        for k, v in regime_fp.items():
            and_clauses.append({
                "$or": [
                    {f"evidence.regime_fp.{k}": v},
                    {f"evidence.{k}": v},
                ]
            })
        q = {
            "stack": brain,
            "symbol": {"$ne": symbol},
            "executed": True,    # only completed plays — they're memorable
        }
        if and_clauses:
            q["$and"] = and_clauses
        rows = await db[SHARED_INTENTS].find(
            q,
            {"_id": 0, "intent_id": 1, "symbol": 1, "action": 1,
             "confidence": 1, "rationale": 1, "executed_at": 1},
        ).sort("executed_at", -1).to_list(MAX_SIMILAR_SETUPS)
        similar = rows

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
    """
    symbol = body.symbol

    # context: latest indicator snapshot for the symbol (used for regime fp)
    snap = await db[SHARED_INDICATOR_SNAPSHOTS].find_one(
        {"symbol": symbol}, {"_id": 0},
        sort=[("captured_at", -1)],
    )
    indicators = (snap or {}).get("indicators") or {}
    regime_fp = _regime_fingerprint(indicators)

    open_positions = await db[SHARED_POSITIONS].find(
        {"symbol": symbol, "state": {"$in": ["open", "managing"]}},
        {"_id": 0, "position_id": 1, "direction": 1, "state": 1, "updated_at": 1},
    ).sort("updated_at", -1).to_list(5)

    strategist_brain = await get_executor_holder()
    auditor_brain = await get_auditor_holder()

    strategist = await _build_role(strategist_brain, symbol, regime_fp)
    auditor = await _build_role(auditor_brain, symbol, regime_fp)

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
    }
    await db[HYPOTHESIS_ANALYSES].insert_one(row)

    return {
        "analysis_id": analysis_id,
        "symbol": symbol,
        "generated_at": now,
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

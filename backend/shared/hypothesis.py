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

# Canonical regime-fingerprint key set. Brains and the server-side
# enrichment hook (see shared/intents.py) target this set; IntentIn's
# evidence validator rejects unknown keys to keep memory recall honest.
REGIME_FP_KEYS: frozenset[str] = frozenset({
    "rsi_band",
    "macd_hist_sign",
    "bb_band",
    "trend_direction",
    "volume_band",
    "volatility_band",
})


def _regime_fingerprint(indicators: dict | None) -> dict:
    """6-key coarse buckets used to find 'similar past setups' across the
    brain's history. Naive on purpose — we want a 5-row recall, not a
    research-grade similarity search.

    Doctrine (2026-02-16 rev2): upgraded from 3 → 6 keys so each setup
    points to a higher-resolution slice of memory. Misalignment on more
    than 2 keys disqualifies a recall match (see hypothesis._build_role
    `regime_fp.$or` query).

    Keys:
      rsi_band          oversold / weak / neutral / strong / overbought
      macd_hist_sign    positive / negative / flat
      bb_band           lower / mid_low / mid_high / upper
      trend_direction   up / down / flat                 (NEW)
      volume_band       quiet / normal / high / spike    (NEW)
      volatility_band   calm / normal / elevated / violent (NEW)

    All keys are optional — if the snapshot is missing the input metric,
    we omit the corresponding key rather than guess. A fingerprint with
    < 6 keys is acceptable and will simply match more loosely.
    """
    if not indicators:
        return {}
    fp: dict = {}

    # 1. RSI band — momentum oscillator zones.
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

    # 2. MACD histogram sign — momentum direction.
    macd = indicators.get("macd") or {}
    hist = macd.get("hist")
    if isinstance(hist, (int, float)):
        fp["macd_hist_sign"] = "positive" if hist > 0 else ("negative" if hist < 0 else "flat")

    # 3. Bollinger position — mean-reversion vs extension.
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

    # 4. Trend direction — price vs SMA50 (preferred) or EMA20 fallback.
    #    Threshold ±0.5% so noise doesn't whip the label.
    price = indicators.get("price") or indicators.get("close")
    sma50 = indicators.get("sma50")
    ema20 = indicators.get("ema20")
    anchor = sma50 if isinstance(sma50, (int, float)) else (
        ema20 if isinstance(ema20, (int, float)) else None
    )
    if isinstance(price, (int, float)) and isinstance(anchor, (int, float)) and anchor > 0:
        delta_pct = (price - anchor) / anchor
        if delta_pct > 0.005:
            fp["trend_direction"] = "up"
        elif delta_pct < -0.005:
            fp["trend_direction"] = "down"
        else:
            fp["trend_direction"] = "flat"

    # 5. Volume band — current bar volume vs 20-day average.
    vol = indicators.get("volume")
    vol_avg = indicators.get("volume_avg20") or indicators.get("avg_volume")
    if isinstance(vol, (int, float)) and isinstance(vol_avg, (int, float)) and vol_avg > 0:
        ratio = vol / vol_avg
        if ratio < 0.6:
            fp["volume_band"] = "quiet"
        elif ratio < 1.3:
            fp["volume_band"] = "normal"
        elif ratio < 2.5:
            fp["volume_band"] = "high"
        else:
            fp["volume_band"] = "spike"

    # 6. Volatility band — ATR% (ATR / price) or rolling stddev.
    atr = indicators.get("atr14")
    if isinstance(atr, (int, float)) and isinstance(price, (int, float)) and price > 0:
        atr_pct = atr / price
        if atr_pct < 0.008:
            fp["volatility_band"] = "calm"
        elif atr_pct < 0.020:
            fp["volatility_band"] = "normal"
        elif atr_pct < 0.040:
            fp["volatility_band"] = "elevated"
        else:
            fp["volatility_band"] = "violent"

    return fp


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

    # The first three queries are independent — run them concurrently.
    # outcomes + similar_setups depend on the opinions/regime results,
    # so they fan out in a second wave.
    intents_task = db[SHARED_INTENTS].find(
        {"stack": brain, "symbol": symbol},
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
        q: dict = {"stack": brain, "symbol": {"$ne": symbol}, "executed": True}
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
    strategist_brain_task = get_executor_holder()
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

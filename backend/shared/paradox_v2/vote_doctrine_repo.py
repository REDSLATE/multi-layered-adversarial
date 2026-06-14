"""Mongo persistence for the Paradox v2 vote-doctrine layer.

Strictly ADDITIVE — none of these reads/writes touch the existing
`/api/v2/evaluate` pipeline. Trading is unaffected.

Persistence map:
    paradox_v2_brain_votes          ← BrainVote dataclass (immutable)
    paradox_v2_calibration_history  ← BrainCalibration._history
    paradox_v2_negative_patterns    ← NegativeKnowledge._patterns
    paradox_v2_failure_attributions ← VerifierReplay.analyze() outputs

Each entity is stored as a plain dict; conversion to/from the
dataclass happens at the boundary. Datetimes are ISO strings on
disk and `datetime` objects in memory.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from db import db
from namespaces import (
    PARADOX_V2_BRAIN_VOTES,
    PARADOX_V2_CALIBRATION,
    PARADOX_V2_NEGATIVE_PATTERNS,
    PARADOX_V2_FAILURE_ATTRIBUTIONS,
)
from shared.brain_vote import BrainVote, CalibrationKey, MarketMemoryResult
from brains.calibration import BrainCalibration, CalibrationRecord
from brains.negative_knowledge import NegativeKnowledge, NegativePattern
from verifier.replay import FailureReason


# ─── BrainVote ────────────────────────────────────────────────────────


def _vote_to_doc(v: BrainVote, *, vote_id: Optional[str] = None,
                 symbol: Optional[str] = None,
                 regime: Optional[str] = None) -> dict[str, Any]:
    """Serialise a BrainVote to a Mongo doc. `symbol` and `regime` are
    operator-supplied context (the dataclass has no notion of either —
    the brain doesn't read the symbol; the caller knows it)."""
    return {
        "vote_id": vote_id or str(uuid.uuid4()),
        "brain": v.brain,
        "stance": v.stance,
        "calibrated_confidence": v.calibrated_confidence,
        "raw_confidence": v.raw_confidence,
        "calibration_key": {
            "regime": v.calibration_key.regime,
            "conf_bucket": v.calibration_key.conf_bucket,
        },
        "memory_evidence": (
            {
                "similar_count": v.memory_evidence.similar_count,
                "win_rate": v.memory_evidence.win_rate,
                "avg_return_bps": v.memory_evidence.avg_return_bps,
                "worst_drawdown_bps": v.memory_evidence.worst_drawdown_bps,
                "failure_pattern": v.memory_evidence.failure_pattern,
            }
            if v.memory_evidence is not None
            else None
        ),
        "negative_knowledge_triggered": v.negative_knowledge_triggered,
        "reasoning": list(v.reasoning),
        "timestamp": v.timestamp.isoformat() if hasattr(v.timestamp, "isoformat") else v.timestamp,
        # Operator context — never read by the brain layer.
        "symbol": symbol,
        "regime": regime,
    }


def _doc_to_vote(d: dict[str, Any]) -> BrainVote:
    ck = d["calibration_key"]
    mem_d = d.get("memory_evidence")
    mem = None
    if mem_d:
        mem = MarketMemoryResult(
            similar_count=int(mem_d["similar_count"]),
            win_rate=float(mem_d["win_rate"]),
            avg_return_bps=float(mem_d["avg_return_bps"]),
            worst_drawdown_bps=float(mem_d["worst_drawdown_bps"]),
            failure_pattern=mem_d.get("failure_pattern"),
        )
    ts = d["timestamp"]
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return BrainVote(
        brain=d["brain"],
        stance=d["stance"],
        calibrated_confidence=float(d["calibrated_confidence"]),
        raw_confidence=float(d["raw_confidence"]),
        calibration_key=CalibrationKey(regime=ck["regime"], conf_bucket=float(ck["conf_bucket"])),
        memory_evidence=mem,
        negative_knowledge_triggered=bool(d["negative_knowledge_triggered"]),
        reasoning=tuple(d["reasoning"]),
        timestamp=ts,
    )


async def save_brain_vote(
    vote: BrainVote,
    *,
    symbol: Optional[str] = None,
    regime: Optional[str] = None,
) -> str:
    doc = _vote_to_doc(vote, symbol=symbol, regime=regime)
    await db[PARADOX_V2_BRAIN_VOTES].insert_one(dict(doc))
    return doc["vote_id"]


async def list_recent_votes(
    *,
    limit: int = 50,
    brain: Optional[str] = None,
    symbol: Optional[str] = None,
) -> list[dict[str, Any]]:
    q: dict[str, Any] = {}
    if brain:
        q["brain"] = brain
    if symbol:
        q["symbol"] = symbol.upper().strip()
    rows = await db[PARADOX_V2_BRAIN_VOTES].find(q, {"_id": 0}).sort(
        "timestamp", -1,
    ).to_list(limit)
    return rows


async def load_votes_by_ids(vote_ids: list[str]) -> list[BrainVote]:
    rows = await db[PARADOX_V2_BRAIN_VOTES].find(
        {"vote_id": {"$in": vote_ids}}, {"_id": 0},
    ).to_list(len(vote_ids))
    return [_doc_to_vote(r) for r in rows]


# ─── BrainCalibration ─────────────────────────────────────────────────
# History rows are keyed by (brain_id, regime, conf_bucket) so multiple
# brains and buckets coexist in one collection.


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def hydrate_calibration(cal: BrainCalibration) -> int:
    """Pull saved buckets for this brain into the in-memory store."""
    rows = await db[PARADOX_V2_CALIBRATION].find(
        {"brain_id": cal.brain_id}, {"_id": 0},
    ).to_list(2000)
    n = 0
    for r in rows:
        key = CalibrationKey(
            regime=r["regime"], conf_bucket=float(r["conf_bucket"]),
        )
        cal._history[key] = CalibrationRecord(
            total_signals=int(r["total_signals"]),
            wins=int(r["wins"]),
            avg_return_bps=float(r["avg_return_bps"]),
            max_drawdown_bps=float(r["max_drawdown_bps"]),
            last_updated=datetime.fromisoformat(
                r["last_updated"].replace("Z", "+00:00"),
            ),
        )
        n += 1
    return n


async def persist_calibration_outcome(
    cal: BrainCalibration,
    key: CalibrationKey,
) -> None:
    """After cal.record_outcome(...), call this to mirror the new state."""
    rec = cal.history(key)
    if rec is None:
        return
    await db[PARADOX_V2_CALIBRATION].update_one(
        {"brain_id": cal.brain_id, "regime": key.regime,
         "conf_bucket": key.conf_bucket},
        {"$set": {
            "brain_id": cal.brain_id,
            "regime": key.regime,
            "conf_bucket": key.conf_bucket,
            "total_signals": rec.total_signals,
            "wins": rec.wins,
            "avg_return_bps": rec.avg_return_bps,
            "max_drawdown_bps": rec.max_drawdown_bps,
            "last_updated": rec.last_updated.isoformat(),
        }},
        upsert=True,
    )


# ─── NegativeKnowledge ────────────────────────────────────────────────


async def hydrate_negative_knowledge(nk: NegativeKnowledge) -> int:
    rows = await db[PARADOX_V2_NEGATIVE_PATTERNS].find(
        {"brain_id": nk.brain_id}, {"_id": 0},
    ).to_list(5000)
    nk._patterns = [
        NegativePattern(
            pattern_hash=r["pattern_hash"],
            regime=r["regime"],
            false_positive_count=int(r["false_positive_count"]),
            regret_score=float(r["regret_score"]),
            last_triggered=datetime.fromisoformat(
                r["last_triggered"].replace("Z", "+00:00"),
            ),
        )
        for r in rows
    ]
    return len(rows)


async def persist_negative_pattern(
    nk: NegativeKnowledge,
    pattern: NegativePattern,
) -> None:
    await db[PARADOX_V2_NEGATIVE_PATTERNS].update_one(
        {"brain_id": nk.brain_id,
         "pattern_hash": pattern.pattern_hash,
         "regime": pattern.regime},
        {"$set": {
            "brain_id": nk.brain_id,
            "pattern_hash": pattern.pattern_hash,
            "regime": pattern.regime,
            "false_positive_count": pattern.false_positive_count,
            "regret_score": pattern.regret_score,
            "last_triggered": pattern.last_triggered.isoformat(),
        }},
        upsert=True,
    )


# ─── FailureReason ────────────────────────────────────────────────────


async def save_failure_attribution(
    reason: FailureReason,
    *,
    case_context: Optional[dict[str, Any]] = None,
) -> str:
    """Append-only — every replay analysis writes one row."""
    aid = str(uuid.uuid4())
    await db[PARADOX_V2_FAILURE_ATTRIBUTIONS].insert_one({
        "attribution_id": aid,
        "type": reason.type.value,
        "responsible_brain": reason.responsible_brain,
        "calibration_error": reason.calibration_error,
        "memory_error": reason.memory_error,
        "negative_knowledge_miss": reason.negative_knowledge_miss,
        "explanation": reason.explanation,
        "case_context": case_context or {},
        "ts": _now(),
    })
    return aid


async def list_recent_attributions(
    *, limit: int = 50, responsible_brain: Optional[str] = None,
) -> list[dict[str, Any]]:
    q: dict[str, Any] = {}
    if responsible_brain:
        q["responsible_brain"] = responsible_brain
    rows = await db[PARADOX_V2_FAILURE_ATTRIBUTIONS].find(
        q, {"_id": 0},
    ).sort("ts", -1).to_list(limit)
    return rows

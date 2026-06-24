"""Consensus boost — advisory pool that lets non-executor brains
contribute to the executor's confidence floor without granting them
fire authority.

Doctrine (operator pick, 2026-06-24):
  * Non-executor brains STILL get blocked at the seat by
    `brain_not_current_seat_holder`. Their fire authority is
    unchanged.
  * However, their opinion is captured into `intent_consensus_pool`
    (TTL 15min). When the executor for the same (lane, symbol)
    later emits, the pool is read and the executor's `confidence`
    is shifted by ±0.05 per agreeing/disagreeing advisor, capped
    at ±0.15.
  * This lets all four brains contribute analytically while keeping
    the operator-pinned executor as the only brain that can pull
    the trigger.
  * The boost is applied to the `confidence_min` floor check in
    SeatPolicy.evaluate(), NOT to the post-fill grading. Doctrine
    grades the executor on its OWN call, not the consensus.

Tunable via runtime_flags (Mongo overrides; operator UI can set them
later without redeploy). Defaults below.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from db import db
from namespaces import INTENT_CONSENSUS_POOL, INTENT_CONSENSUS_TELEMETRY
from shared.pipeline.models import BrainOpinion


# ── Defaults (operator can override per-flag in `runtime_flags`) ────
DEFAULT_BOOST_PER_BRAIN = 0.05
DEFAULT_BOOST_CAP = 0.15
DEFAULT_WINDOW_SECONDS = 900   # 15 min — pool TTL AND lookup window


# Cache for one request — the seat is constructed per-request so this
# in-memory cache is request-scoped and never stale.
_RUNTIME_FLAGS_CACHE: Dict[str, Any] = {}


async def _load_runtime_flag(name: str, default: float) -> float:
    """Read a tunable from `runtime_flags`. Returns default if missing
    or malformed. Best-effort — never raises."""
    if name in _RUNTIME_FLAGS_CACHE:
        return _RUNTIME_FLAGS_CACHE[name]
    try:
        doc = await db["runtime_flags"].find_one({"_id": name}, {"_id": 0, "value": 1})
        val = float(doc["value"]) if doc and "value" in doc else default
    except Exception:  # noqa: BLE001
        val = default
    _RUNTIME_FLAGS_CACHE[name] = val
    return val


@dataclass
class ConsensusResult:
    """The seat reads this and uses `effective_confidence` for the
    floor check. The post-mortem reads it for telemetry."""
    base_confidence: float
    delta: float
    effective_confidence: float
    agree_count: int
    disagree_count: int
    agree_brains: List[str] = field(default_factory=list)
    disagree_brains: List[str] = field(default_factory=list)
    advisor_count: int = 0   # total advisors in window (agree + disagree + other)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "base_confidence": round(self.base_confidence, 4),
            "delta": round(self.delta, 4),
            "effective_confidence": round(self.effective_confidence, 4),
            "agree_count": self.agree_count,
            "disagree_count": self.disagree_count,
            "agree_brains": self.agree_brains,
            "disagree_brains": self.disagree_brains,
            "advisor_count": self.advisor_count,
        }


# ── Public API ──────────────────────────────────────────────────────
async def record_advisory_opinion(
    opinion: BrainOpinion,
    block_reason: str,
) -> None:
    """Capture a non-executor brain's opinion into the consensus pool.

    Called from SeatPolicy.evaluate() at the
    `brain_not_current_seat_holder` reject path. The opinion is
    auto-pruned by the TTL index on `ts`.

    Never raises — pool capture is best-effort housekeeping; we never
    want it to break the seat path.
    """
    try:
        await db[INTENT_CONSENSUS_POOL].insert_one({
            "intent_id": opinion.intent_id,
            "brain_id": opinion.brain_id,
            "lane": opinion.lane,
            "symbol": opinion.symbol,
            "action": opinion.action,
            "confidence": float(opinion.confidence),
            "ts": datetime.now(timezone.utc),
            "block_reason": block_reason,
        })
    except Exception:  # noqa: BLE001
        return


async def compute_consensus_boost(
    opinion: BrainOpinion,
) -> ConsensusResult:
    """Compute the consensus delta for the executor's opinion.

    Reads the pool for matching (lane, symbol) within the configured
    window. Counts brains whose `action` matches the executor's
    (agree) or opposes it (disagree). Returns a ConsensusResult with
    the boosted effective confidence.

    HOLD/ABSTAIN opinions in the pool are ignored — they're
    non-directional and don't tell us anything about consensus on a
    directional executor call. They DO still get captured (the pool
    is the full audit trail) but they don't move the boost.

    Symmetric pairs are handled:
      - executor BUY → agrees with BUY, disagrees with SELL
      - executor SELL → agrees with SELL, disagrees with BUY
      - executor HOLD → no boost (no directional reference)
    """
    base = float(opinion.confidence)
    no_boost = ConsensusResult(
        base_confidence=base,
        delta=0.0,
        effective_confidence=base,
        agree_count=0,
        disagree_count=0,
        advisor_count=0,
    )
    if opinion.action not in ("BUY", "SELL"):
        # HOLD/ABSTAIN executor → no consensus reference. Pass through.
        return no_boost

    per_brain = await _load_runtime_flag(
        "consensus_boost_per_brain", DEFAULT_BOOST_PER_BRAIN
    )
    cap = await _load_runtime_flag("consensus_boost_cap", DEFAULT_BOOST_CAP)
    window_s = await _load_runtime_flag(
        "consensus_window_seconds", DEFAULT_WINDOW_SECONDS
    )

    cutoff = datetime.now(timezone.utc) - timedelta(seconds=window_s)
    rows = await db[INTENT_CONSENSUS_POOL].find(
        {
            "lane": opinion.lane,
            "symbol": opinion.symbol,
            "ts": {"$gte": cutoff},
            # Exclude the executor's own historical advisory entries
            # (e.g. if the executor seat changed within the window).
            "brain_id": {"$ne": opinion.brain_id},
        },
        {"_id": 0, "brain_id": 1, "action": 1},
    ).to_list(length=100)

    opposite = {"BUY": "SELL", "SELL": "BUY"}[opinion.action]
    seen_brain_actions: Dict[str, str] = {}
    # If a brain emitted multiple advisories in the window, take the
    # MOST RECENT one as their latest read. The query above doesn't
    # sort; we iterate and overwrite. Simpler than a $group aggregation
    # for what is typically <10 rows.
    for r in rows:
        b = r.get("brain_id")
        a = r.get("action")
        if b and a:
            seen_brain_actions[b] = a

    agree_brains = [b for b, a in seen_brain_actions.items() if a == opinion.action]
    disagree_brains = [b for b, a in seen_brain_actions.items() if a == opposite]

    # Sort for deterministic telemetry output.
    agree_brains.sort()
    disagree_brains.sort()

    raw_delta = per_brain * (len(agree_brains) - len(disagree_brains))
    delta = max(-cap, min(cap, raw_delta))
    effective = max(0.0, min(1.0, base + delta))

    return ConsensusResult(
        base_confidence=base,
        delta=delta,
        effective_confidence=effective,
        agree_count=len(agree_brains),
        disagree_count=len(disagree_brains),
        agree_brains=agree_brains,
        disagree_brains=disagree_brains,
        advisor_count=len(seen_brain_actions),
    )


async def record_telemetry(
    intent_id: str,
    result: ConsensusResult,
    applied: bool,
) -> None:
    """Write the consensus result for an executor's intent to a
    sidecar collection. The post-mortem and receipts panels read
    this to render the boost story for the operator.

    `applied` = whether the result was non-trivial (delta != 0).
    Same TTL as the pool — keeps the sidecar bounded.

    Never raises.
    """
    try:
        await db[INTENT_CONSENSUS_TELEMETRY].insert_one({
            "intent_id": intent_id,
            "ts": datetime.now(timezone.utc),
            "applied": applied,
            **result.to_dict(),
        })
    except Exception:  # noqa: BLE001
        return


def clear_runtime_flags_cache() -> None:
    """Test hook — flush the in-memory tunable cache."""
    _RUNTIME_FLAGS_CACHE.clear()

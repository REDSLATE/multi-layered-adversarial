"""Shelly memory/reasoning contracts.

Doctrine (operator-locked, pass #13):
    Shelly is MEMORY + REASONING. Shelly is NOT authority.

    Brain Shelly  = local learning per brain
    MC Shelly     = shared memory head, cross-brain reasoning
    MC core       = verifier / notary  (existing 12-gate chain)
    RoadGuard     = safety              (existing market-structure guards)
    Brains        = decision authority  (existing seat doctrine)

    Shelly CAN say:  "support" · "warn" · "neutral" · "seen before"
                     · "loss pattern" · "conflict between brains"
    Shelly CANNOT say: "execute" · "block" · "override" · "promote"

    Every artifact emitted carries `authority="memory_reasoning_only"`
    as a schema-pinned tag. The gate chain MUST NOT branch on this
    field. RoadGuard MUST NOT consult it. The brain seat holder
    decides; Shelly is advisory context only.

This module is pure data shape — no DB, no IO, no async. Pickled into
both LocalShelly and MCShelly call sites so the contract stays the
same everywhere.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


# Pinned authority tag — locked by tripwire. Any artifact that doesn't
# carry this exact string is rejected at write time.
AUTHORITY_MEMORY_REASONING_ONLY = "memory_reasoning_only"

# Pinned recommendation vocabulary. Shelly's verdicts MUST come from
# this set; anything else means a code-path drifted into authority
# territory and will be rejected.
RECOMMENDATIONS_ALLOWED = frozenset({
    "support",      # historical pattern looks favorable
    "warn",         # historical pattern looks unfavorable
    "neutral",      # insufficient or mixed signal
    "seen_before",  # informational — operator has been here before
})

# Pinned BANNED vocabulary. Anything Shelly might be tempted to say
# that would imply authority must be explicitly forbidden so a future
# refactor can't sneak it in.
RECOMMENDATIONS_BANNED = frozenset({
    "execute", "block", "override", "promote",
    "approve", "reject", "kill", "force",
})


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_hash(payload: dict[str, Any]) -> str:
    """Deterministic SHA256 of a JSON-encoded dict. Used to dedupe
    memory events at upsert time."""
    raw = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ShellyMemoryEvent:
    """One row of brain history. Stored in the per-brain local
    collection and (after rollup) in the MC shared collection."""
    brain: str
    symbol: str
    direction: str           # BUY | SHORT | HOLD | SELL | COVER
    confidence: float
    decision: str            # what the brain ultimately decided
    features: dict[str, Any] = field(default_factory=dict)
    mc_status: str = "UNKNOWN"        # what MC's gate chain said
    roadguard_status: str = "UNKNOWN"  # what RoadGuard said
    outcome: Optional[dict[str, Any]] = None  # pnl_pct, exit_ts, etc.
    created_at: str = ""

    def to_doc(self) -> dict[str, Any]:
        doc = asdict(self)
        if not doc["created_at"]:
            doc["created_at"] = utc_now()
        # Pinned doctrine tag — locked at construction time, not
        # mutable downstream.
        doc["authority"] = AUTHORITY_MEMORY_REASONING_ONLY
        # event_hash hashes the SEMANTIC content only — NOT created_at,
        # which would defeat idempotent upsert (same event remembered
        # twice would produce two different hashes). Stable fields are
        # what defines event identity for dedupe purposes.
        hash_payload = {
            k: v for k, v in doc.items()
            if k not in ("created_at", "event_hash")
        }
        doc["event_hash"] = stable_hash(hash_payload)
        return doc


@dataclass(frozen=True)
class ShellyReasoningReceipt:
    """One reasoning verdict. Lives in the per-brain receipts
    collection (LocalShelly) or the MC shared receipts collection
    (MCShelly). Brain teams read these as advisory context."""
    brain: str
    symbol: str
    recommendation: str           # must be in RECOMMENDATIONS_ALLOWED
    confidence_delta: float       # bounded [-0.25, +0.10]
    reasons: list[str] = field(default_factory=list)
    evidence_hashes: list[str] = field(default_factory=list)
    authority: str = AUTHORITY_MEMORY_REASONING_ONLY

    def to_doc(self) -> dict[str, Any]:
        # Doctrine check — Shelly NEVER emits a banned verdict.
        if self.recommendation in RECOMMENDATIONS_BANNED:
            raise ValueError(
                f"Shelly recommendation {self.recommendation!r} is BANNED — "
                f"Shelly is memory/reasoning only, not authority"
            )
        if self.recommendation not in RECOMMENDATIONS_ALLOWED:
            raise ValueError(
                f"Shelly recommendation {self.recommendation!r} not in "
                f"allowed vocabulary {sorted(RECOMMENDATIONS_ALLOWED)}"
            )
        # Confidence delta bounded — Shelly cannot single-handedly
        # tank or pump a brain's confidence. Same bounds as the
        # existing memory_modulator (intents.py).
        if not (-0.25 <= self.confidence_delta <= 0.10):
            raise ValueError(
                f"Shelly confidence_delta {self.confidence_delta} outside "
                f"bounds [-0.25, +0.10]"
            )
        if self.authority != AUTHORITY_MEMORY_REASONING_ONLY:
            raise ValueError(
                f"Shelly authority field tampered: got {self.authority!r}, "
                f"must be {AUTHORITY_MEMORY_REASONING_ONLY!r}"
            )

        doc = asdict(self)
        doc["created_at"] = utc_now()
        doc["receipt_hash"] = stable_hash(doc)
        return doc

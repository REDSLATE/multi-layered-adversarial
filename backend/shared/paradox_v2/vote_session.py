"""Phase 2 vote escalation — auditor veto → 3-min vote pool.

Doctrine (2026-02-19):
    When an intent earns decision=PENDING_VOTE (governor flagged
    vote_required, e.g. earnings_window) or the auditor vetoes,
    a vote session opens. The four CANONICAL BRAINS vote — NOT
    the seats. The auditor that vetoed does not re-vote (their veto
    is their vote). Rules:

      * Eligible voters: alpha, camaro, chevelle, redeye
      * Quorum: ≥2 brains must cast a vote (ABSTAIN counts toward
        quorum but NOT toward the majority tally)
      * Majority: strict > 50% of non-abstain votes
      * Tie or no quorum at the 3-min timeout: REJECT
      * Outcome is one of: BUY_UP, SELL_DOWN, REJECT

The session is a single Mongo document; votes are appended atomically.
This module is PURE coordination logic — it never executes a trade. The
caller decides what to do with the resolved outcome.

Trading-impact: zero. No existing flow calls into here yet. Operator
opens sessions manually via /api/v2/vote-sessions/open and the
auto-sweeper resolves timed-out sessions in the background.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone, timedelta
from typing import Any, Optional, Literal
from collections import Counter

from db import db
from namespaces import PARADOX_V2_VOTE_SESSIONS


CANONICAL_BRAINS: tuple[str, ...] = ("alpha", "camaro", "chevelle", "redeye")
VOTE_WINDOW_SECONDS_DEFAULT: int = 180  # 3 min per the operator spec
QUORUM_DEFAULT: int = 2

Vote = Literal["BUY_UP", "SELL_DOWN", "HOLD", "ABSTAIN"]
Outcome = Literal["BUY_UP", "SELL_DOWN", "REJECT", "PENDING"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


async def open_session(
    *,
    intent_id: Optional[str],
    symbol: str,
    lane: str,
    triggered_by: str,
    reason: str,
    excluded_brain: Optional[str] = None,
    window_seconds: int = VOTE_WINDOW_SECONDS_DEFAULT,
    quorum: int = QUORUM_DEFAULT,
) -> dict[str, Any]:
    """Open a new vote session. The auditor that vetoed is excluded
    (their veto IS their vote). Returns the session doc."""
    eligible = [b for b in CANONICAL_BRAINS if b != excluded_brain]
    if len(eligible) < quorum:
        raise ValueError(
            f"cannot open session: only {len(eligible)} eligible brains, "
            f"quorum requires {quorum}"
        )
    now = _now()
    sid = str(uuid.uuid4())
    doc = {
        "session_id": sid,
        "intent_id": intent_id,
        "symbol": symbol.upper().strip(),
        "lane": lane,
        "triggered_by": triggered_by,
        "reason": reason,
        "excluded_brain": excluded_brain,
        "eligible_brains": list(eligible),
        "votes": [],            # list of {brain, vote, reason, ts}
        "status": "OPEN",
        "outcome": "PENDING",
        "quorum": quorum,
        "window_seconds": window_seconds,
        "opened_at": _iso(now),
        "expires_at": _iso(now + timedelta(seconds=window_seconds)),
        "resolved_at": None,
    }
    await db[PARADOX_V2_VOTE_SESSIONS].insert_one(dict(doc))
    doc.pop("_id", None)
    return doc


async def cast_vote(
    session_id: str,
    *,
    brain: str,
    vote: Vote,
    reason: str,
) -> dict[str, Any]:
    """Append a brain's vote to an open session. Idempotent per brain:
    a brain cannot vote twice (the second call raises). If the session
    is already CLOSED, raises."""
    doc = await db[PARADOX_V2_VOTE_SESSIONS].find_one({"session_id": session_id}, {"_id": 0})
    if not doc:
        raise LookupError(f"session not found: {session_id}")
    if doc["status"] != "OPEN":
        raise ValueError(f"session already {doc['status']}; outcome={doc['outcome']}")
    if brain not in doc["eligible_brains"]:
        raise ValueError(
            f"brain '{brain}' not eligible (excluded or non-canonical). "
            f"eligible={doc['eligible_brains']}"
        )
    if any(v["brain"] == brain for v in doc["votes"]):
        raise ValueError(f"brain '{brain}' already voted in session {session_id}")
    expires = datetime.fromisoformat(doc["expires_at"])
    if _now() > expires:
        # Window closed — caller should call resolve() to settle.
        raise ValueError("vote window expired; call resolve()")

    entry = {
        "brain": brain,
        "vote": vote,
        "reason": reason,
        "ts": _iso(_now()),
    }
    await db[PARADOX_V2_VOTE_SESSIONS].update_one(
        {"session_id": session_id, "status": "OPEN"},
        {"$push": {"votes": entry}},
    )
    # Re-read and (auto-resolve) if quorum reached + window closed OR
    # all eligible brains have voted.
    refreshed = await db[PARADOX_V2_VOTE_SESSIONS].find_one(
        {"session_id": session_id}, {"_id": 0},
    )
    if len(refreshed["votes"]) >= len(refreshed["eligible_brains"]):
        # All voted — resolve immediately, don't wait for timeout.
        return await resolve(session_id, force=True)
    return refreshed


def _tally(votes: list[dict[str, Any]]) -> tuple[Outcome, dict[str, Any]]:
    """Apply Phase 2 doctrine to a list of votes and return outcome.

    Rules:
      * ABSTAIN counts toward quorum but NOT majority.
      * Majority = strict > 50% of non-abstain votes.
      * Tie or no quorum or no majority → REJECT.
    """
    counts = Counter(v["vote"] for v in votes)
    total = sum(counts.values())
    abstain_n = counts.get("ABSTAIN", 0)
    non_abstain = total - abstain_n
    by_dir = {
        "BUY_UP": counts.get("BUY_UP", 0),
        "SELL_DOWN": counts.get("SELL_DOWN", 0),
        "HOLD": counts.get("HOLD", 0),
    }
    summary = {
        "total_votes": total,
        "abstain_votes": abstain_n,
        "non_abstain_votes": non_abstain,
        "by_direction": by_dir,
    }
    if non_abstain == 0:
        return "REJECT", summary

    # Strict majority: more than half of non-abstain votes.
    winner, winning_count = max(by_dir.items(), key=lambda kv: kv[1])
    if winning_count * 2 > non_abstain:
        if winner in ("BUY_UP", "SELL_DOWN"):
            return winner, summary  # type: ignore[return-value]
        # HOLD majority → REJECT (no override of the auditor veto).
        return "REJECT", summary
    # Tie or no majority.
    return "REJECT", summary


async def resolve(
    session_id: str,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Resolve a session. If `force=False`, only resolves if the window
    has expired. Always applies Phase 2 doctrine: quorum check, then
    majority over non-abstain votes, ties REJECT."""
    doc = await db[PARADOX_V2_VOTE_SESSIONS].find_one({"session_id": session_id}, {"_id": 0})
    if not doc:
        raise LookupError(f"session not found: {session_id}")
    if doc["status"] != "OPEN":
        return doc  # already resolved — idempotent

    now = _now()
    expires = datetime.fromisoformat(doc["expires_at"])
    if not force and now < expires:
        return doc  # not yet — caller may retry after timeout

    votes = doc["votes"]
    if len(votes) < doc["quorum"]:
        outcome: Outcome = "REJECT"
        summary = {"reason": f"no_quorum: {len(votes)}/{doc['quorum']}"}
    else:
        outcome, summary = _tally(votes)
        if outcome == "REJECT":
            summary["reason"] = "tie_or_no_majority"

    await db[PARADOX_V2_VOTE_SESSIONS].update_one(
        {"session_id": session_id, "status": "OPEN"},
        {"$set": {
            "status": "CLOSED",
            "outcome": outcome,
            "tally": summary,
            "resolved_at": _iso(now),
        }},
    )
    return await db[PARADOX_V2_VOTE_SESSIONS].find_one(
        {"session_id": session_id}, {"_id": 0},
    )


async def sweep_expired(limit: int = 50) -> dict[str, Any]:
    """Background sweeper — auto-resolves sessions past their expiry.
    Safe to call repeatedly; only OPEN sessions are touched."""
    now = _now()
    rows = await db[PARADOX_V2_VOTE_SESSIONS].find(
        {"status": "OPEN", "expires_at": {"$lt": _iso(now)}},
        {"session_id": 1, "_id": 0},
    ).to_list(limit)
    resolved: list[dict[str, Any]] = []
    for r in rows:
        try:
            d = await resolve(r["session_id"], force=True)
            resolved.append({
                "session_id": d["session_id"],
                "outcome": d.get("outcome"),
            })
        except Exception as e:  # noqa: BLE001
            resolved.append({"session_id": r["session_id"], "error": str(e)})
    return {"swept": len(resolved), "items": resolved, "ts": _iso(now)}

"""Layer 3 (Verified Fact Memory) + Layer 6 (Auto-curated RISEDUAL Wiki).

L3 — Verified Fact Memory
─────────────────────────
A rollup ingested by MCShelly is RAW: it represents what one brain
chose to remember. L3 promotes a memory event to a "verified fact"
when independent evidence converges:

    Auto-certify path: ≥ MIN_BRAIN_CONVERGENCE distinct brains have
        independently rolled up the same `event_hash` AND at least
        MIN_RESOLVED_OUTCOMES of those rollups carry an outcome
        (so we know the direction wasn't only an opinion).

    Operator-certify path: an admin promotes a single rollup to
        verified fact manually. Stored with `via="operator"`.

A verified fact is the doctrine-truth unit MC-Shelly hands to the
RISEDUAL wiki and the AI training corpora.

L6 — Auto-curated RISEDUAL Wiki
────────────────────────────────
A nightly synthesis job groups verified facts by (symbol, direction,
regime-bucket) and writes a wiki entry summarizing what RISEDUAL has
LEARNED about that pattern. Each entry is purely advisory data —
operator-readable + AI-training-corpus-readable, no execution
authority.

Authority pin: writes only to two new collections
(`shelly_verified_facts`, `risedual_wiki`). Never executes, never
promotes a brain, never overrides RoadGuard. The 'verify' verdict is
PROVENANCE, not PERMISSION — a brain still has to go through the
full gate chain to trade.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

from shelly.contracts import AUTHORITY_MEMORY_REASONING_ONLY, utc_now
from shelly.sync_db import get_db


# ─── Tunables (operator-friendly, no env required) ───
MIN_BRAIN_CONVERGENCE = 3
MIN_RESOLVED_OUTCOMES = 1
MAX_FACTS_PER_CERTIFY_RUN = 200
MAX_WIKI_ENTRIES_PER_CURATE_RUN = 100


VERIFIED_FACTS_COLL = "shelly_verified_facts"
WIKI_COLL = "risedual_wiki"


def _coll_facts():
    return get_db()[VERIFIED_FACTS_COLL]


def _coll_wiki():
    return get_db()[WIKI_COLL]


def _coll_shared():
    from shelly.mc_shelly import MCShelly  # noqa: WPS433
    return get_db()[MCShelly.SHARED_MEMORY_COLL]


# ──────────────────────────── L3 — Verified Facts ────────────────────────────

def certify_one(event_hash: str, *, via: str = "operator",
                operator: str | None = None,
                note: str | None = None) -> dict[str, Any]:
    """Promote ONE rollup event to verified fact. Idempotent on event_hash.

    Used by:
        * the operator-countersign admin endpoint (via="operator")
        * the auto-certify scan (via="auto_convergence")
    """
    shared_doc = _coll_shared().find_one(
        {"event_hash": event_hash}, {"_id": 0}
    )
    if not shared_doc:
        return {"ok": False, "reason": "no_shared_memory_for_hash",
                "event_hash": event_hash}

    existing = _coll_facts().find_one(
        {"event_hash": event_hash}, {"_id": 0, "event_hash": 1, "via": 1}
    )
    if existing:
        return {"ok": True, "already_verified": True, **existing}

    # The fact stores the canonical event PLUS provenance.
    fact = {
        "event_hash": event_hash,
        "symbol": shared_doc.get("symbol"),
        "direction": shared_doc.get("direction"),
        "confidence": shared_doc.get("confidence"),
        "decision": shared_doc.get("decision"),
        "features": shared_doc.get("features") or {},
        "outcome": shared_doc.get("outcome"),
        "via": via,
        "operator": operator,
        "note": note,
        "source_brain": shared_doc.get("source_brain"),
        "certified_at": utc_now(),
        "authority": AUTHORITY_MEMORY_REASONING_ONLY,
    }
    _coll_facts().insert_one(dict(fact))
    return {"ok": True, "newly_verified": True, "event_hash": event_hash}


def auto_certify_scan(*, limit: int = MAX_FACTS_PER_CERTIFY_RUN) -> dict[str, Any]:
    """Scan `shelly_mc_shared_memory` for event_hashes that have been
    rolled up by ≥ MIN_BRAIN_CONVERGENCE distinct brains. Promote each
    to verified fact via the auto path. Bounded by `limit`.

    Returns a summary the operator can read or render in a UI tile.
    """
    pipeline = [
        {"$group": {
            "_id": "$event_hash",
            "brains": {"$addToSet": "$source_brain"},
            "resolved": {"$sum": {"$cond": [{"$ifNull": ["$outcome", False]}, 1, 0]}},
        }},
        {"$match": {
            f"brains.{MIN_BRAIN_CONVERGENCE - 1}": {"$exists": True},
            "resolved": {"$gte": MIN_RESOLVED_OUTCOMES},
        }},
        {"$limit": limit},
    ]
    candidates = list(_coll_shared().aggregate(pipeline))

    newly = 0
    skipped = 0
    for c in candidates:
        h = c["_id"]
        r = certify_one(
            h, via="auto_convergence",
            note=f"converged across {len(c['brains'])} brains, "
                 f"{c['resolved']} resolved outcomes",
        )
        if r.get("newly_verified"):
            newly += 1
        else:
            skipped += 1

    return {
        "ok": True,
        "scanned": len(candidates),
        "newly_verified": newly,
        "already_verified": skipped,
        "min_convergence": MIN_BRAIN_CONVERGENCE,
        "min_resolved_outcomes": MIN_RESOLVED_OUTCOMES,
        "authority": AUTHORITY_MEMORY_REASONING_ONLY,
    }


def verified_facts_summary() -> dict[str, Any]:
    """Dashboard tile data. Read-only."""
    total = _coll_facts().count_documents({})
    via_counts: dict[str, int] = {}
    for v in ("operator", "auto_convergence"):
        via_counts[v] = _coll_facts().count_documents({"via": v})
    return {
        "ok": True,
        "total": total,
        "by_via": via_counts,
        "min_convergence": MIN_BRAIN_CONVERGENCE,
        "min_resolved_outcomes": MIN_RESOLVED_OUTCOMES,
    }


# ──────────────────────────── L6 — RISEDUAL Wiki ────────────────────────────

def _wiki_topic_key(fact: dict[str, Any]) -> str:
    """Group verified facts by (symbol, direction). Coarser grouping
    keeps wiki entries dense; finer grouping splinters too quickly at
    low sample counts."""
    sym = (fact.get("symbol") or "UNKNOWN").upper()
    direction = (fact.get("direction") or "?").upper()
    return f"{sym}::{direction}"


def _aggregate_topic(facts: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize one topic's facts into a wiki entry payload."""
    n = len(facts)
    if n == 0:
        return {}

    # Outcome stats (only over facts that carry an outcome).
    pnls: list[float] = []
    wins = losses = flat = 0
    for f in facts:
        o = f.get("outcome") or {}
        pct = o.get("pnl_pct")
        if pct is None:
            continue
        try:
            pct = float(pct)
        except (TypeError, ValueError):
            continue
        pnls.append(pct)
        if pct > 0.001:
            wins += 1
        elif pct < -0.001:
            losses += 1
        else:
            flat += 1

    feature_counter: Counter[str] = Counter()
    for f in facts:
        for k, v in (f.get("features") or {}).items():
            feature_counter[f"{k}={v}"] += 1
    top_features = [{"feature": k, "n": v}
                    for k, v in feature_counter.most_common(8)]

    brains = sorted({f.get("source_brain") for f in facts if f.get("source_brain")})
    confidences = [f.get("confidence") for f in facts
                   if isinstance(f.get("confidence"), (int, float))]
    avg_conf = (sum(confidences) / len(confidences)) if confidences else None

    return {
        "n_facts": n,
        "n_resolved": len(pnls),
        "wins": wins,
        "losses": losses,
        "flat": flat,
        "avg_pnl_pct": (sum(pnls) / len(pnls)) if pnls else None,
        "min_pnl_pct": min(pnls) if pnls else None,
        "max_pnl_pct": max(pnls) if pnls else None,
        "avg_confidence": avg_conf,
        "brains_seen": brains,
        "top_features": top_features,
    }


def curate_wiki_run(*, limit: int = MAX_WIKI_ENTRIES_PER_CURATE_RUN) -> dict[str, Any]:
    """Read every verified fact, group by (symbol, direction), write
    one wiki row per topic. Idempotent — re-runs overwrite the row.

    Bounded by `limit` topics. Re-run to cover the remainder.
    """
    cursor = _coll_facts().find({}, {"_id": 0})
    by_topic: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for f in cursor:
        by_topic[_wiki_topic_key(f)].append(f)

    topics = list(by_topic.items())[:limit]
    written = 0
    for topic_key, facts in topics:
        agg = _aggregate_topic(facts)
        symbol, direction = topic_key.split("::", 1)
        doc = {
            "topic_key": topic_key,
            "symbol": symbol,
            "direction": direction,
            "summary": agg,
            "curated_at": utc_now(),
            "authority": AUTHORITY_MEMORY_REASONING_ONLY,
        }
        _coll_wiki().update_one(
            {"topic_key": topic_key},
            {"$set": doc},
            upsert=True,
        )
        written += 1

    return {
        "ok": True,
        "topics_curated": written,
        "total_topics": len(by_topic),
        "authority": AUTHORITY_MEMORY_REASONING_ONLY,
    }


def wiki_summary() -> dict[str, Any]:
    return {
        "ok": True,
        "total_entries": _coll_wiki().count_documents({}),
    }


def wiki_lookup(symbol: str, direction: str | None = None) -> list[dict[str, Any]]:
    """Operator/brain-facing read: 'what has RISEDUAL learned about
    SYMBOL [+ direction]?'. Returns wiki entries; never mutates."""
    q: dict[str, Any] = {"symbol": symbol.upper()}
    if direction:
        q["direction"] = direction.upper()
    return list(_coll_wiki().find(q, {"_id": 0}))

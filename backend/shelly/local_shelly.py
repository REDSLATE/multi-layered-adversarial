"""Local Shelly — one per brain. Local learning only. SYNC pymongo.

Doctrine:
    LocalShelly stores the per-brain history and reasons over THAT
    BRAIN'S past decisions only — no cross-brain comparison happens
    here (that's MCShelly's job).

    LocalShelly cannot trade, block, approve, or override. Every
    artifact it writes carries `authority="memory_reasoning_only"`.

Implementation: sync pymongo (see shelly/sync_db.py for rationale).
"""
from __future__ import annotations

from typing import Any

from shelly.contracts import (
    AUTHORITY_MEMORY_REASONING_ONLY,
    RECOMMENDATIONS_ALLOWED,
    ShellyMemoryEvent,
    ShellyReasoningReceipt,
)
from shelly.embeddings import (
    CANDIDATE_POOL_DEFAULT,
    compute_event_embedding,
    cosine_rank,
)
from shelly.sync_db import get_db


LOCAL_SIMILAR_LOOKBACK = 25
LOCAL_MIN_SAMPLES_FOR_WARN = 5
LOCAL_LOSS_RATE_WARN_THRESHOLD = 0.60


class LocalShelly:
    """One per brain. Reads/writes the brain's own collections only.

    Brain names are normalized through `shared.brain_identity` so a
    caller passing a display name ("Barracuda") or a casing variant
    ("CAMARO") still lands in the same canonical Mongo collections
    (`shelly_camaro_memories`, etc.) as a caller passing the
    canonical ID. Prevents the silent-collection-fragmentation bug
    where a display-name leak forks brain memory into an orphan
    collection."""

    def __init__(self, brain_name: str):
        from shared.brain_identity import normalize_brain_id  # noqa: WPS433
        normalized = normalize_brain_id(brain_name)
        # Edge case: test fixtures (e.g. `LocalShelly("twembed")` for
        # embedding tests) use non-canonical names on purpose. We
        # preserve the original lowercase for those — the production
        # invariant is "every CANONICAL brain lands on the canonical
        # collection", not "every name must be canonical".
        if normalized != "unknown":
            self.brain_name = normalized
        else:
            self.brain_name = (brain_name or "").strip().lower()
        self.memories_coll_name = f"shelly_{self.brain_name}_memories"
        self.receipts_coll_name = f"shelly_{self.brain_name}_reasoning_receipts"

    @property
    def memories(self):
        return get_db()[self.memories_coll_name]

    @property
    def receipts(self):
        return get_db()[self.receipts_coll_name]

    # ──────────────────────── write path ────────────────────────

    def remember(self, event: ShellyMemoryEvent) -> dict[str, Any]:
        """Idempotent upsert keyed on event_hash.

        Phase 2 (2026-05-27): on FIRST insert, compute and store a
        384-dim embedding so `find_similar` can do semantic retrieval.
        Embedding failures are silent — Phase 1 exact-match path
        continues to work without them.
        """
        doc = event.to_doc()
        doc["shelly_scope"] = "local"
        doc["owner_brain"] = self.brain_name

        # Compute embedding once per event (idempotent — same hash,
        # same content, same vector). Failures are non-fatal.
        vec, _meta = compute_event_embedding(doc)
        if vec is not None:
            doc["embedding"] = vec
            doc["embedding_provider"] = _meta.get("provider", "local")

        self.memories.update_one(
            {"event_hash": doc["event_hash"]},
            {"$setOnInsert": doc},
            upsert=True,
        )
        return doc

    # ──────────────────────── Phase 2 similarity ────────────────────────

    def find_similar(
        self,
        current_case: dict[str, Any],
        *,
        top_k: int = 10,
        candidate_pool: int = CANDIDATE_POOL_DEFAULT,
        min_score: float = 0.0,
    ) -> list[dict[str, Any]]:
        """Semantic retrieval over THIS brain's memories.

        Doctrine: ADVISORY_ONLY. Returns ranked candidates with a
        `similarity` score in [0, 1]. The brain may consult these
        as additional context; they never modify execution
        authority. Phase 1's `reason()` (exact-match) is the formal
        reasoning path; this is a richer second view.
        """
        query_vec, _meta = compute_event_embedding(current_case)
        if query_vec is None:
            return []
        # Pull a recent candidate pool. Cap so cosine ranking stays
        # millisecond-scale on the sync path.
        candidates = list(
            self.memories.find({"embedding": {"$exists": True}}, {"_id": 0})
            .sort("created_at", -1)
            .limit(candidate_pool)
        )
        return cosine_rank(
            query_vec, candidates, top_k=top_k, min_score=min_score,
        )

    # ──────────────────────── reasoning ────────────────────────

    def reason(self, current_case: dict[str, Any]) -> dict[str, Any]:
        """Look up similar resolved cases for THIS brain; emit a
        reasoning receipt. Advisory only."""
        symbol = current_case.get("symbol")
        direction = current_case.get("direction")

        similar = list(
            self.memories.find(
                {
                    "symbol": symbol,
                    "direction": direction,
                    "outcome.pnl_pct": {"$exists": True},
                },
                {"_id": 0},
            )
            .sort("created_at", -1)
            .limit(LOCAL_SIMILAR_LOOKBACK)
        )

        losses = [
            m for m in similar
            if float(m.get("outcome", {}).get("pnl_pct", 0)) < 0
        ]

        reasons: list[str] = []
        confidence_delta = 0.0
        recommendation = "neutral"

        if (
            len(similar) >= LOCAL_MIN_SAMPLES_FOR_WARN
            and len(losses) / len(similar) >= LOCAL_LOSS_RATE_WARN_THRESHOLD
        ):
            recommendation = "warn"
            confidence_delta = -0.15
            reasons.append(
                f"{self.brain_name} has seen this setup {len(similar)} times; "
                f"loss rate {len(losses)/len(similar):.0%}."
            )
        elif not similar:
            reasons.append("No strong local memory match.")
        else:
            reasons.append(
                f"{self.brain_name}: {len(similar)} similar cases, "
                f"{len(losses)} losses — mixed signal."
            )

        receipt_doc = ShellyReasoningReceipt(
            brain=self.brain_name,
            symbol=symbol or "UNKNOWN",
            recommendation=recommendation,
            confidence_delta=confidence_delta,
            reasons=reasons,
            evidence_hashes=[m["event_hash"] for m in similar[:10]],
        ).to_doc()

        self.receipts.insert_one(receipt_doc)
        receipt_doc.pop("_id", None)
        return receipt_doc

    # ──────────────────────── MC rollup ────────────────────────

    def rollup_for_mc(self, limit: int = 100) -> list[dict[str, Any]]:
        """Return memories not yet rolled up to MC Shelly."""
        return list(
            self.memories.find(
                {"rolled_to_mc": {"$ne": True}},
                {"_id": 0},
            )
            .sort("created_at", -1)
            .limit(limit)
        )

    def mark_rolled_to_mc(self, event_hashes: list[str]) -> None:
        """Stamp memories as rolled up so subsequent calls don't
        re-ship them."""
        if not event_hashes:
            return
        self.memories.update_many(
            {"event_hash": {"$in": event_hashes}},
            {"$set": {"rolled_to_mc": True}},
        )

    # ──────────────────────── self-check ────────────────────────

    @staticmethod
    def doctrine_authority_tag() -> str:
        return AUTHORITY_MEMORY_REASONING_ONLY

    @staticmethod
    def allowed_recommendations() -> frozenset[str]:
        return RECOMMENDATIONS_ALLOWED

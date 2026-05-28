"""Shelly Phase 2 — embedding-based similarity retrieval.

Phase 1 reasoning (already shipped) does exact-match retrieval on
`(symbol, direction)`. That's good for "we've seen AAPL BUY 5
times" but misses "we've seen base-breakouts on AMC, GME, HOTH —
this AAPL one rhymes with those."

Phase 2 introduces embedding-based retrieval as an ADDITIVE layer:
  * On `remember()`, compute an embedding of the memory's salient
    text and store it alongside the event doc.
  * `find_similar(case, top_k)` returns the K nearest events by
    cosine, scoped to the brain's own memories.
  * Phase 1's exact-match `reason()` is untouched.

Doctrine pin:
    ADVISORY_ONLY. Same `authority="memory_reasoning_only"` stamp.
    Embeddings inform retrieval; they never modify execution
    authority or RoadGuard.

    The embedding adapter (`shared.llm.embed`) lives in the existing
    LLM kernel subtree so the same SHADOW→PRIMARY doctrine governs
    embeddings as text-gen.

Pure-Python cosine over Mongo:
    For now we fetch the brain's recent N candidate memories from
    Mongo, then rank by cosine in Python. With small per-brain
    corpora (<10k memories) this is fine. When corpora grow
    larger, this is the seam where we'd plug in a vector index
    (Chroma sidecar / Mongo Atlas Vector Search).
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from shared.llm.embed import EMBED_DIM, cosine_similarity, embed_text


logger = logging.getLogger("risedual.shelly.embeddings")


# How many candidate memories we'll cosine-rank per find_similar call.
# Bounded so Phase 2 retrieval stays O(few-ms) on the sync path.
CANDIDATE_POOL_DEFAULT = 500


def memory_event_to_text(event: dict[str, Any]) -> str:
    """Serialize a memory event into a short, stable text string for
    embedding. Captures the signal-bearing fields; ignores noise.

    Order matters here — same content → same string → same embedding
    on re-embedding (deterministic).
    """
    parts: list[str] = []
    symbol = (event.get("symbol") or "").strip().upper()
    direction = (event.get("direction") or "").strip().upper()
    decision = (event.get("decision") or "").strip()
    if symbol:
        parts.append(f"symbol={symbol}")
    if direction:
        parts.append(f"direction={direction}")
    if decision:
        parts.append(f"decision={decision}")

    # Features are the brain's per-decision signal vector. We
    # serialize a stable subset rather than the full dict so a
    # noisy field doesn't poison the embedding.
    features = event.get("features") or {}
    if isinstance(features, dict):
        for k in sorted(features.keys()):
            v = features[k]
            # Only embed scalar-ish fields; skip nested/objects.
            if isinstance(v, (str, int, float, bool)):
                parts.append(f"{k}={v}")

    outcome = event.get("outcome") or {}
    if isinstance(outcome, dict):
        pnl = outcome.get("pnl_pct")
        if pnl is not None:
            try:
                parts.append(f"pnl_pct={float(pnl):.2f}")
            except (TypeError, ValueError):
                pass

    return " | ".join(parts) if parts else "empty"


def compute_event_embedding(event: dict[str, Any]) -> tuple[Optional[list[float]], dict]:
    """Embed a memory event. Returns (vector, meta). Caller decides
    how to persist."""
    text = memory_event_to_text(event)
    return embed_text(text)


def cosine_rank(
    query_vec: list[float],
    candidates: list[dict[str, Any]],
    *,
    top_k: int = 10,
    min_score: float = 0.0,
) -> list[dict[str, Any]]:
    """Rank `candidates` by cosine similarity to `query_vec`.

    Each candidate must carry an `embedding` field (list of floats).
    Candidates without an embedding are skipped (Phase 1 memories
    written before this layer existed remain in storage but don't
    participate in similarity ranking).
    """
    scored: list[tuple[float, dict[str, Any]]] = []
    for c in candidates:
        vec = c.get("embedding")
        if not isinstance(vec, list) or len(vec) != len(query_vec):
            continue
        s = cosine_similarity(query_vec, vec)
        if s >= min_score:
            scored.append((s, c))
    scored.sort(key=lambda x: x[0], reverse=True)
    out: list[dict[str, Any]] = []
    for s, c in scored[:top_k]:
        row = dict(c)
        row["similarity"] = round(s, 4)
        # Drop the embedding from the response — it's heavy and
        # already served its purpose at ranking time.
        row.pop("embedding", None)
        out.append(row)
    return out

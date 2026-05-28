"""Shelly Phase 2 — embedding-based similarity tripwires (2026-05-27).

Locks the doctrine pins for the new embedding adapter + Shelly's
`find_similar` retrieval path:

  1. Local embedding adapter is ready (fastembed installed).
  2. embed_text returns a 384-dim vector with ADVISORY_ONLY meta.
  3. memory_event_to_text is deterministic for the same content.
  4. LocalShelly.remember writes an `embedding` field to Mongo.
  5. LocalShelly.find_similar returns cosine-ranked matches with
     similarity scores in [0, 1].
  6. find_similar carries authority=memory_reasoning_only.
  7. cosine_rank tolerates dimension mismatches without crashing.
"""
from __future__ import annotations

import pytest

from shelly.contracts import (
    AUTHORITY_MEMORY_REASONING_ONLY,
    ShellyMemoryEvent,
)
from shelly.embeddings import (
    compute_event_embedding,
    cosine_rank,
    memory_event_to_text,
)
from shelly.local_shelly import LocalShelly
from shelly.sync_db import get_db
from shared.llm.embed import (
    EMBED_DIM,
    cosine_similarity,
    embed_text,
    embed_texts,
)


pytestmark = [pytest.mark.tripwire]


# ──────────────────────── adapter readiness ────────────────────────


def test_local_embedding_adapter_is_ready():
    """fastembed must be installed for Phase 2 to work."""
    from shared.llm.adapters import local_embedding_adapter
    assert local_embedding_adapter.is_ready() is True


def test_embed_text_returns_384_dim_vector():
    """BGE-small must produce 384-dim vectors."""
    vec, meta = embed_text("AAPL base breakout pattern")
    assert vec is not None
    assert len(vec) == EMBED_DIM == 384
    assert meta["llm_authority"] == "ADVISORY_ONLY"
    assert meta["provider"] == "local"
    assert all(isinstance(x, float) for x in vec)


def test_embed_texts_batch():
    """Batch embedding returns one vector per input."""
    vecs, meta = embed_texts(["AAPL", "MSFT", "BTC/USD"])
    assert vecs is not None
    assert len(vecs) == 3
    assert all(len(v) == 384 for v in vecs)
    assert meta["count"] == 3


def test_embed_text_advisory_authority_stamped():
    """Every embed result MUST carry ADVISORY_ONLY — same doctrine
    pin as the text-gen kernel."""
    _vec, meta = embed_text("test")
    assert meta["llm_authority"] == "ADVISORY_ONLY"


def test_cosine_similarity_basic_math():
    """Self-similarity = 1.0; orthogonal = 0.0; dims mismatch = 0.0."""
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert cosine_similarity([1.0, 0.0], [1.0]) == 0.0
    assert cosine_similarity([], [1.0, 0.0]) == 0.0


def test_cosine_similarity_semantic_separation():
    """Trade-related texts must be MORE similar than unrelated texts.
    Locks the model: if someone swaps to a broken embedding, this
    fails fast."""
    a, _ = embed_text("AAPL base breakout pattern")
    b, _ = embed_text("GME consolidation pattern")
    c, _ = embed_text("the quick brown fox jumps over the lazy dog")
    s_ab = cosine_similarity(a, b)
    s_ac = cosine_similarity(a, c)
    assert s_ab > s_ac, (
        f"trade-related similarity {s_ab:.3f} must exceed unrelated {s_ac:.3f}"
    )


# ──────────────────────── memory_event_to_text determinism ────────────────────────


def test_memory_event_to_text_is_deterministic():
    """Same content → same string. Required so re-embedding produces
    the same vector for an idempotent dedupe."""
    e = {
        "symbol": "AAPL", "direction": "BUY", "decision": "BUY",
        "features": {"rvol": 2.1, "spread_bps": 4.5},
    }
    s1 = memory_event_to_text(e)
    s2 = memory_event_to_text(dict(e))  # different dict identity
    assert s1 == s2
    # Feature ordering must be stable (sorted keys).
    e2 = {
        "symbol": "AAPL", "direction": "BUY", "decision": "BUY",
        "features": {"spread_bps": 4.5, "rvol": 2.1},  # reversed order
    }
    assert memory_event_to_text(e2) == s1


def test_memory_event_to_text_skips_nested_features():
    """Nested feature values must NOT poison the embedding text."""
    e = {
        "symbol": "AAPL", "direction": "BUY",
        "features": {"rvol": 2.1, "deep": {"nested": "ignored"}},
    }
    s = memory_event_to_text(e)
    assert "rvol=2.1" in s
    assert "nested" not in s


# ──────────────────────── compute_event_embedding ────────────────────────


def test_compute_event_embedding_returns_vector():
    e = {"symbol": "AAPL", "direction": "BUY", "features": {"rvol": 2.0}}
    vec, _meta = compute_event_embedding(e)
    assert vec is not None
    assert len(vec) == 384


# ──────────────────────── cosine_rank ────────────────────────


def test_cosine_rank_orders_by_similarity():
    """Highest-cosine candidate must come first."""
    query, _ = embed_text("AAPL base breakout")
    aapl, _ = embed_text("AAPL consolidation breakout")
    pizza, _ = embed_text("pizza delivery routing problem")
    candidates = [
        {"id": "pizza", "embedding": pizza},
        {"id": "aapl", "embedding": aapl},
    ]
    out = cosine_rank(query, candidates, top_k=2)
    assert len(out) == 2
    assert out[0]["id"] == "aapl"
    assert out[0]["similarity"] > out[1]["similarity"]


def test_cosine_rank_drops_embedding_from_response():
    """Embeddings are heavy — must be stripped before serving."""
    query, _ = embed_text("test")
    candidates = [{"id": "c1", "embedding": query}]
    out = cosine_rank(query, candidates, top_k=1)
    assert len(out) == 1
    assert "embedding" not in out[0]
    assert "similarity" in out[0]


def test_cosine_rank_skips_candidates_without_embedding():
    """Phase-1 memories (pre-Phase-2) lack embeddings and must be
    silently skipped rather than crash."""
    query, _ = embed_text("test")
    candidates = [
        {"id": "phase1_pre_embed"},          # no embedding field
        {"id": "wrong_dim", "embedding": [1.0, 2.0]},  # mismatch
        {"id": "ok", "embedding": query},
    ]
    out = cosine_rank(query, candidates, top_k=10)
    assert len(out) == 1
    assert out[0]["id"] == "ok"


# ──────────────────────── LocalShelly.remember stores embedding ────────────────────────


def test_local_shelly_remember_persists_embedding():
    """The Phase-2 wire: remember() must add an `embedding` field."""
    shelly = LocalShelly("twembed")
    # Clean slate for the test brain (collection auto-created on write).
    db = get_db()
    db[shelly.memories_coll_name].delete_many({"symbol": "TWEMBED1"})
    try:
        event = ShellyMemoryEvent(
            brain="twembed", symbol="TWEMBED1", direction="BUY",
            confidence=0.6, decision="BUY",
            features={"rvol": 2.0, "spread_bps": 5.0},
            outcome={"pnl_pct": 1.2},
        )
        doc = shelly.remember(event)
        # The returned doc may have a fresh embedding pre-write;
        # the AUTHORITATIVE check is what landed in Mongo:
        stored = db[shelly.memories_coll_name].find_one(
            {"event_hash": doc["event_hash"]}, {"_id": 0},
        )
        assert stored is not None
        assert isinstance(stored.get("embedding"), list)
        assert len(stored["embedding"]) == 384
        assert stored.get("embedding_provider") == "local"
        # Doctrine pin still intact.
        assert stored.get("authority") == AUTHORITY_MEMORY_REASONING_ONLY
    finally:
        db[shelly.memories_coll_name].delete_many({"symbol": "TWEMBED1"})


def test_local_shelly_find_similar_returns_ranked_matches():
    """Phase 2 end-to-end: write 3 memories, query, get the closest."""
    shelly = LocalShelly("twembed")
    db = get_db()
    db[shelly.memories_coll_name].delete_many(
        {"symbol": {"$in": ["TWE_AAPL", "TWE_GME", "TWE_PIZZA"]}},
    )
    try:
        for i, (sym, feats) in enumerate([
            ("TWE_AAPL",  {"rvol": 2.1, "pattern": "base_breakout"}),
            ("TWE_GME",   {"rvol": 2.5, "pattern": "base_breakout"}),
            ("TWE_PIZZA", {"topping": "pepperoni", "delivery": "fast"}),
        ]):
            shelly.remember(ShellyMemoryEvent(
                brain="twembed", symbol=sym, direction="BUY",
                confidence=0.6, decision="BUY", features=feats,
                outcome={"pnl_pct": 0.5},
            ))

        out = shelly.find_similar(
            {"symbol": "TWE_NEW", "direction": "BUY",
             "features": {"rvol": 2.0, "pattern": "base_breakout"}},
            top_k=3,
        )
        assert len(out) >= 1
        # Ranked by similarity descending; trade-related must beat pizza.
        if len(out) >= 2:
            assert out[0]["similarity"] >= out[1]["similarity"]
        top_syms = {r["symbol"] for r in out[:2]}
        assert top_syms & {"TWE_AAPL", "TWE_GME"}
    finally:
        db[shelly.memories_coll_name].delete_many(
            {"symbol": {"$in": ["TWE_AAPL", "TWE_GME", "TWE_PIZZA"]}},
        )


# ──────────────────────── doctrine ────────────────────────


def test_embed_module_has_advisory_authority_constant():
    """The kernel must export the ADVISORY_ONLY constant matching
    the text-gen kernel's pin."""
    from shared.llm import embed as embed_mod
    assert embed_mod.LLM_AUTHORITY == "ADVISORY_ONLY"


def test_local_shelly_find_similar_signature_returns_list():
    """Empty-pool call must return [] not crash."""
    shelly = LocalShelly("twempty_brain")
    db = get_db()
    # Guarantee empty collection.
    db[shelly.memories_coll_name].delete_many({})
    out = shelly.find_similar(
        {"symbol": "NOTHING", "direction": "BUY"}, top_k=5,
    )
    assert out == []

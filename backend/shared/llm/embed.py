"""Embedding kernel — provider-independent embedding dispatcher.

Mirrors the text-gen `kernel.py` shape but much simpler today —
only one provider (`local`) is wired. The structure exists so we
can add `self_trained` and `openai` adapters later without changing
any call sites.

Public surface:
    `from shared.llm.embed import embed_text, embed_texts, EMBED_DIM`
    `vec, meta = embed_text("AAPL base breakout")`

Doctrine pin:
    ADVISORY_ONLY. Embeddings inform similarity retrieval. They
    NEVER modify execution authority. Every result includes
    `llm_authority="ADVISORY_ONLY"` in the metadata.

Author trail: cloned from local_adapter.py pattern at the
operator's request (pass #14, 2026-05-27). See PROVIDER_PRIORITY
parity below — same `local → self_trained → external` doctrine as
the text kernel.
"""
from __future__ import annotations

import logging
from typing import Optional

from shared.llm.adapters import local_embedding_adapter

logger = logging.getLogger("risedual.llm_kernel.embed")


LLM_AUTHORITY = "ADVISORY_ONLY"

# Provider priority for embeddings. Parallel to the text kernel's
# `PROVIDER_PRIORITY`. Today only `local` is wired; the others are
# placeholders for the eventual self-trained + OpenAI cohort.
EMBEDDING_PROVIDER_PRIORITY = ("local",)

EMBED_DIM = local_embedding_adapter.DEFAULT_EMBEDDING_DIM


def _choose_provider() -> str:
    """Walk the priority list, return the first ready provider."""
    if local_embedding_adapter.is_ready():
        return "local"
    return ""


def embed_texts(texts: list[str]) -> tuple[Optional[list[list[float]]], dict]:
    """Batch embed. Returns (vectors_or_None, metadata).

    `metadata` always carries:
      - `provider`: which adapter served the call (or "" if none ready)
      - `llm_authority`: "ADVISORY_ONLY"
      - `error`: present only on failure
    """
    provider = _choose_provider()
    meta: dict = {
        "provider": provider,
        "llm_authority": LLM_AUTHORITY,
        "count": len(texts),
    }
    if not provider:
        meta["error"] = "no_embedding_provider_ready"
        return None, meta

    if provider == "local":
        vecs, err = local_embedding_adapter.embed(texts)
        if err:
            meta.update(err)
            return None, meta
        meta["dim"] = len(vecs[0]) if vecs else 0
        return vecs, meta

    meta["error"] = f"unknown_provider:{provider}"
    return None, meta


def embed_text(text: str) -> tuple[Optional[list[float]], dict]:
    """Single-text convenience wrapper."""
    vecs, meta = embed_texts([text])
    if vecs is None:
        return None, meta
    if not vecs:
        meta["error"] = "empty_result"
        return None, meta
    return vecs[0], meta


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Pure-Python cosine. Used by Shelly's similarity retrieval to
    avoid pulling numpy as a hard dep on the hot path."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / ((na ** 0.5) * (nb ** 0.5))

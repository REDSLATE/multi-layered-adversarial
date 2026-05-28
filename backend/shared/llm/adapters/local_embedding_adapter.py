"""Local-embedding adapter — BGE-small via fastembed (sync, offline).

Cloned from `local_adapter.py` (text-gen) — same doctrine, same
SHADOW→PRIMARY shape, but for embeddings instead of token generation.

Doctrine pin:
    ADVISORY_ONLY. Embeddings are a similarity feature for memory
    retrieval. They never modify execution authority, never gate
    intents, never modify RoadGuard. Same `LLM_AUTHORITY` stamp the
    text kernel uses applies here too — the EmbedResult carries it.

    Provider parity with the text kernel:
      * `local`          — fastembed BGE-small (this module)
      * `self_trained`   — operator's own embedding model (future stub)
      * `openai`         — text-embedding-3-small (future)
    The router picks based on `embedding_provider_state` Mongo doc;
    `local` ships PRIMARY by default for embeddings (no external
    competing with it; offline is the easiest stable default).

Model: `BAAI/bge-small-en-v1.5` — 384-dim, ~80MB ONNX weights, MTEB
top-3 small model, MIT license, fully offline after first load.

Lazy load: the model is downloaded on first `embed()` call (~2-3s
cold start) and held in module-global state thereafter. Subsequent
embeds are ~5ms per 100 chars.

Failure mode: if `fastembed` isn't installed (e.g. a stripped
deploy image), `is_ready()` returns False and `embed()` returns
`(None, {"error": "fastembed_missing"})`. Callers degrade gracefully.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

LOCAL_EMBEDDING_MODEL_ENV = "RISE_AI_LOCAL_EMBEDDING_MODEL"
DEFAULT_LOCAL_EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
DEFAULT_EMBEDDING_DIM = 384

logger = logging.getLogger("risedual.llm_kernel.local_embedding")


# Module-level model singleton — lazy-loaded.
_model = None
_model_load_attempted = False
_model_load_error: Optional[str] = None


def is_ready() -> bool:
    """True iff fastembed importable + model can be loaded.

    Cheap probe — does NOT trigger the actual model download. We
    only check that the dependency is installed. The real load
    happens on the first `embed()` call.
    """
    try:
        import fastembed  # noqa: F401
        return True
    except ImportError:
        return False


def _load_model():
    """Lazy-load the embedding model. Idempotent — subsequent calls
    return the cached instance."""
    global _model, _model_load_attempted, _model_load_error
    if _model is not None:
        return _model
    if _model_load_attempted and _model_load_error:
        # Don't retry forever — if it failed once, return the cached
        # error until the process restarts.
        return None
    _model_load_attempted = True
    try:
        from fastembed import TextEmbedding
        name = os.environ.get(
            LOCAL_EMBEDDING_MODEL_ENV, DEFAULT_LOCAL_EMBEDDING_MODEL,
        )
        _model = TextEmbedding(model_name=name)
        logger.info("local_embedding_adapter loaded model=%s", name)
        return _model
    except Exception as exc:  # noqa: BLE001
        _model_load_error = f"{type(exc).__name__}: {exc}"
        logger.warning(
            "local_embedding_adapter failed to load: %s",
            _model_load_error,
        )
        return None


def embed(texts: list[str]) -> tuple[Optional[list[list[float]]], Optional[dict]]:
    """Batch-embed a list of texts. Returns (vectors, error_meta).

    On success: (list of float lists, None).
    On failure: (None, {"error": "<reason>"}).

    SYNC by design — matches the existing Shelly sync pymongo
    posture. From async code, wrap with `asyncio.to_thread`.
    """
    if not texts:
        return [], None
    model = _load_model()
    if model is None:
        return None, {
            "error": "model_not_loaded",
            "detail": _model_load_error or "fastembed not installed",
        }
    try:
        # fastembed returns a generator of numpy arrays — materialize
        # to plain Python lists so the result is JSON-serializable
        # and the Mongo driver can store it as a BSON array.
        vectors = [vec.tolist() for vec in model.embed(texts)]
        return vectors, None
    except Exception as exc:  # noqa: BLE001
        logger.warning("local_embedding_adapter embed failed: %s", exc)
        return None, {"error": "embed_failed", "detail": str(exc)[:200]}


def embed_one(text: str) -> tuple[Optional[list[float]], Optional[dict]]:
    """Convenience wrapper for the single-text case."""
    vectors, err = embed([text])
    if err is not None:
        return None, err
    if not vectors:
        return None, {"error": "empty_result"}
    return vectors[0], None

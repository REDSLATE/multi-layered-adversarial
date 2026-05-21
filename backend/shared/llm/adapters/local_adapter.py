"""Local-inference adapter — stub for now, real later.

Doctrine pin:
    This adapter is the operator's "leave-the-platform" insurance.
    When MC moves to self-hosted infrastructure with an Ollama /
    vLLM / TGI instance, replace the body of `call_local` with an
    HTTP call to the local inference endpoint. Adapter signature
    does not change.

    The stub returns a clear NOT_IMPLEMENTED marker so brain code
    can detect-and-degrade. It does NOT raise — silent text-string
    matching the contract keeps test fixtures simple and prevents
    a runtime crash from a hopeful operator who flipped routing
    policy to "local" before the local infra was ready.
"""
from __future__ import annotations

import os
from typing import Optional, Tuple


LOCAL_INFERENCE_URL_ENV = "RISE_AI_LOCAL_INFERENCE_URL"


def is_ready() -> bool:
    """True iff the operator has set up a local inference endpoint."""
    return bool(os.environ.get(LOCAL_INFERENCE_URL_ENV))


async def call_local(
    *,
    model: str,
    prompt: str,
    system: str,
    session_id: str,
) -> Tuple[str, Optional[dict]]:
    url = os.environ.get(LOCAL_INFERENCE_URL_ENV, "")
    if not url:
        return (
            "[local_adapter:NOT_IMPLEMENTED] "
            f"model={model} session={session_id} — "
            f"set {LOCAL_INFERENCE_URL_ENV} and replace this stub with "
            f"an HTTP call to your local inference server.",
            None,
        )
    # Future: httpx.AsyncClient.post(url, json={...}) here. Kept
    # unimplemented so the stub does not silently appear to "work"
    # against a misconfigured endpoint.
    return (
        f"[local_adapter:STUB_HIT_URL] url={url} model={model} session={session_id}",
        None,
    )

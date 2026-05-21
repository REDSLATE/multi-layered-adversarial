"""
BrainLLMKernel — the central router.

Doctrine pin (2026-02-XX, revision):
    ADVISORY_ONLY. The kernel is a reasoning service. It NEVER
    imports from `shared.execution`, `shared.broker_router`,
    `shared.auto_router`, or `shared.broker`. Tripwire enforces this.

    PROVIDER_PRIORITY is local → self_trained → anthropic → openai
    → gemini. Local/self_trained ship at SHADOW (logged, not
    consulted); commercial providers ship at PRIMARY. As the
    operator promotes local/self_trained via `llm_provider_state`
    in Mongo, they take over from the top of the priority list.

Public surface:
    `from shared.llm import llm_kernel`
    `await llm_kernel.call(role=..., task=..., prompt=...)`
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Dict, Optional, Set

from shared.llm.adapters.anthropic_adapter import (
    call_anthropic, is_ready as anthropic_ready,
)
from shared.llm.adapters.gemini_adapter import (
    call_gemini, is_ready as gemini_ready,
)
from shared.llm.adapters.local_adapter import (
    call_local, is_ready as local_ready,
)
from shared.llm.adapters.openai_adapter import (
    call_openai, is_ready as openai_ready,
)
from shared.llm.adapters.self_trained_adapter import (
    call_self_trained, is_ready as self_trained_ready,
)
from shared.llm.ledger import record_llm_call
from shared.llm.provider_state import get_promotion_states
from shared.llm.routing_policy import choose_model

logger = logging.getLogger("risedual.llm_kernel")


# Sentinel stamped on every response. Tripwired.
LLM_AUTHORITY = "ADVISORY_ONLY"


_READY_PROBES = {
    "openai": openai_ready,
    "anthropic": anthropic_ready,
    "gemini": gemini_ready,
    "local": local_ready,
    "self_trained": self_trained_ready,
}


class BrainLLMKernel:
    """Provider-independent LLM router with audit ledger.

    Usage:
        from shared.llm import llm_kernel
        result = await llm_kernel.call(role="strategist",
                                       task="conviction_grade",
                                       prompt="...")
    """

    name = "RISE_AI_LLM_KERNEL"
    version = "0.2.0"

    async def call(
        self,
        *,
        role: str,
        task: str,
        prompt: str,
        system: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        session_id: Optional[str] = None,
        provider_override: Optional[str] = None,
        model_override: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Route → call → ledger → return.

        Provider failures are captured into a structured error
        response (still `ADVISORY_ONLY`). The kernel does NOT raise.
        """
        metadata = metadata or {}
        sid = session_id or str(uuid.uuid4())
        call_id = str(uuid.uuid4())

        ready_set = _probe_ready()
        promotion = await get_promotion_states()
        route = choose_model(role=role, task=task, ready=ready_set, promotion=promotion)
        provider = provider_override or route["provider"]
        model = model_override or route["model"]

        started_at = time.time()
        ok = True
        response_text = ""
        error_text: Optional[str] = None
        usage: Optional[Dict[str, Any]] = None

        try:
            response_text, usage = await _dispatch(
                provider=provider,
                model=model,
                prompt=prompt,
                system=system or _default_system(role, task),
                session_id=sid,
            )
        except Exception as e:  # noqa: BLE001
            ok = False
            error_text = f"{type(e).__name__}: {e}"
            logger.warning(
                "llm_kernel call failed role=%s task=%s provider=%s model=%s err=%s",
                role, task, provider, model, error_text,
            )

        latency_ms = int((time.time() - started_at) * 1000)

        # Ledger write is best-effort.
        try:
            await record_llm_call(
                call_id=call_id,
                role=role,
                task=task,
                provider=provider,
                model=model,
                prompt=prompt,
                response=response_text,
                ok=ok,
                error=error_text,
                usage=usage,
                metadata=metadata,
                session_id=sid,
                latency_ms=latency_ms,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("llm_kernel ledger write failed: %s", e)

        return {
            "call_id": call_id,
            "role": role,
            "task": task,
            "provider": provider,
            "model": model,
            "response": response_text,
            "ok": ok,
            "error": error_text,
            "usage": usage,
            "session_id": sid,
            "latency_ms": latency_ms,
            # NEVER mutate. Tripwire scans for it.
            "llm_authority": LLM_AUTHORITY,
        }


def _probe_ready() -> Set[str]:
    """Synchronous readiness probe across all adapters. None of the
    probes do network I/O — they only check env vars — so it's safe
    to call on every request."""
    out: Set[str] = set()
    for name, fn in _READY_PROBES.items():
        try:
            if fn():
                out.add(name)
        except Exception as e:  # noqa: BLE001
            logger.warning("readiness probe %s raised: %s", name, e)
    return out


async def _dispatch(
    *,
    provider: str,
    model: str,
    prompt: str,
    system: str,
    session_id: str,
):
    if provider == "openai":
        return await call_openai(model=model, prompt=prompt, system=system, session_id=session_id)
    if provider == "anthropic":
        return await call_anthropic(model=model, prompt=prompt, system=system, session_id=session_id)
    if provider == "gemini":
        return await call_gemini(model=model, prompt=prompt, system=system, session_id=session_id)
    if provider == "local":
        return await call_local(model=model, prompt=prompt, system=system, session_id=session_id)
    if provider == "self_trained":
        return await call_self_trained(model=model, prompt=prompt, system=system, session_id=session_id)
    raise ValueError(f"unknown provider: {provider}")


def _default_system(role: str, task: str) -> str:
    """Conservative default system prompt — pins ADVISORY_ONLY into
    the model's own context so even a forgetful caller can't make
    the model think it has execution authority."""
    return (
        "You are RISE_AI brain operating in ADVISORY mode. Your output "
        "is reasoning only; you do NOT have execution authority. "
        "RoadGuard, the executor seat, and the governance gates have "
        "final authority over any trade. Do not claim to place orders, "
        "modify doctrine, or bypass safety gates. "
        f"Role: {role}. Task: {task}."
    )


# Module-level singleton. Importers do:
#     from shared.llm import llm_kernel
llm_kernel = BrainLLMKernel()

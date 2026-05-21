"""
RISE_AI Model Adapter Kernel — the seventh box.

Doctrine pin (2026-02-XX):
    LLM output is ADVISORY ONLY.

    This kernel sits between brains and frontier model providers.
    Brains ask `await llm_kernel.call(role=..., task=..., prompt=...)`
    and get a reasoning string back. The kernel does NOT execute
    trades, does NOT bypass RoadGuard, does NOT alter doctrine,
    does NOT promote HOLD into a directional trade.

    Every kernel response is stamped `llm_authority="ADVISORY_ONLY"`.
    Tripwire `test_kernel_response_is_always_advisory` enforces it.

    Tripwire `test_kernel_module_does_not_import_execution_surfaces`
    enforces that this module's source code never imports
    `shared.execution`, `shared.broker_router`, `shared.auto_router`,
    or `shared.broker`. The kernel is an isolated reasoning layer.

Why this layer exists:
    Today every LLM call in MC is hand-coded against
    `emergentintegrations.llm.chat`. That couples the entire system
    to ONE provider broker. When the operator leaves the platform,
    every LLM call has to be re-pointed at a direct provider key.

    This kernel is the seam. Brains call the kernel; the kernel
    decides which provider serves which role × task. Swapping
    Anthropic for a local Qwen runtime is now a one-line change in
    `routing_policy.py`, not a repo-wide refactor.

Public surface:
    `from shared.llm import llm_kernel`
    `await llm_kernel.call(role="strategist", task="conviction_grade",
                           prompt="...", system="...")`

    Returns:
        {
            "role": str, "task": str,
            "provider": str, "model": str,
            "response": str, "session_id": str,
            "latency_ms": int,
            "llm_authority": "ADVISORY_ONLY",
            "call_id": str,    # FK into the llm_calls ledger
        }
"""
from __future__ import annotations

from .kernel import BrainLLMKernel, llm_kernel
from .routing_policy import choose_model

__all__ = ["BrainLLMKernel", "llm_kernel", "choose_model"]

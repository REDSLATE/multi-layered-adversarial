"""
RISE_AI LLM Kernel — doctrine-lock tripwire tests.

Locked invariants:
  * Every kernel response carries `llm_authority="ADVISORY_ONLY"`.
  * The kernel module NEVER imports an execution surface.
  * Routing policy walks `PROVIDER_PRIORITY` and respects promotion
    state. local/self_trained ship at SHADOW and must NOT serve
    traffic until promoted.
  * Adapter signatures all match (kw-only model/prompt/system/session_id).
  * Each adapter exposes `is_ready()`.
  * `KNOWN_PROVIDERS`, `PROVIDER_PRIORITY`, `PROMOTION_STATES`
    are pinned exactly.
  * Ledger writes are best-effort.
"""
from __future__ import annotations

import asyncio
import inspect
import re
from pathlib import Path

import pytest

from shared.llm.adapters import (
    anthropic_adapter,
    gemini_adapter,
    local_adapter,
    openai_adapter,
    self_trained_adapter,
)
from shared.llm.kernel import LLM_AUTHORITY, BrainLLMKernel, llm_kernel
from shared.llm.routing_policy import (
    DEFAULT_PROMOTION_STATE,
    KNOWN_PROVIDERS,
    PROMOTION_STATES,
    PROVIDER_PRIORITY,
    choose_model,
)

_KERNEL_PATH = Path(__file__).parent.parent / "shared" / "llm" / "kernel.py"


# ─────────────── ADVISORY-ONLY contract ───────────────────────────────


@pytest.mark.tripwire
def test_llm_authority_constant_is_exact_string():
    assert LLM_AUTHORITY == "ADVISORY_ONLY"


@pytest.mark.tripwire
@pytest.mark.asyncio
async def test_kernel_response_is_always_advisory(monkeypatch):
    async def _fake_dispatch(*, provider, model, prompt, system, session_id):
        return ("fake response", {"input_tokens": 1, "output_tokens": 1})

    monkeypatch.setattr("shared.llm.kernel._dispatch", _fake_dispatch)
    monkeypatch.setattr("shared.llm.kernel.record_llm_call", _async_noop)

    k = BrainLLMKernel()
    out = await k.call(role="strategist", task="unit_test", prompt="hi")
    assert out["llm_authority"] == "ADVISORY_ONLY"
    assert out["ok"] is True
    assert out["response"] == "fake response"


@pytest.mark.tripwire
@pytest.mark.asyncio
async def test_kernel_stamps_advisory_even_on_provider_failure(monkeypatch):
    async def _boom(*, provider, model, prompt, system, session_id):
        raise RuntimeError("provider-down")

    monkeypatch.setattr("shared.llm.kernel._dispatch", _boom)
    monkeypatch.setattr("shared.llm.kernel.record_llm_call", _async_noop)

    out = await llm_kernel.call(role="opponent", task="unit_test", prompt="hi")
    assert out["llm_authority"] == "ADVISORY_ONLY"
    assert out["ok"] is False
    assert "provider-down" in (out["error"] or "")


@pytest.mark.tripwire
@pytest.mark.asyncio
async def test_kernel_call_does_not_raise_on_ledger_failure(monkeypatch):
    async def _ok_dispatch(*, provider, model, prompt, system, session_id):
        return ("ok", None)

    async def _ledger_boom(**kwargs):
        raise RuntimeError("mongo-down")

    monkeypatch.setattr("shared.llm.kernel._dispatch", _ok_dispatch)
    monkeypatch.setattr("shared.llm.kernel.record_llm_call", _ledger_boom)

    out = await llm_kernel.call(role="strategist", task="unit_test", prompt="hi")
    assert out["llm_authority"] == "ADVISORY_ONLY"
    assert out["ok"] is True


# ─────────────── No-execution-surface import tripwire ─────────────────


@pytest.mark.tripwire
def test_kernel_module_does_not_import_execution_surfaces():
    """Scan import statements (NOT docstrings) for forbidden surfaces."""
    forbidden = (
        "shared.execution",
        "shared.broker_router",
        "shared.auto_router",
        "shared.executor_seat",
        "shared.broker.",
        "shared.broker_symbol",
    )
    src = _KERNEL_PATH.read_text(encoding="utf-8")
    code_imports = [
        line.strip() for line in src.splitlines()
        if re.match(r"^\s*(import|from)\s+", line)
    ]
    for line in code_imports:
        for needle in forbidden:
            assert needle not in line, (
                f"kernel.py import line {line!r} references forbidden "
                f"execution surface {needle!r}."
            )


@pytest.mark.tripwire
def test_kernel_class_has_no_execute_methods():
    forbidden_verbs = (
        "execute", "submit", "place_order", "send_order",
        "route_order", "place_trade",
    )
    for name, _ in inspect.getmembers(BrainLLMKernel):
        if name.startswith("_"):
            continue
        for verb in forbidden_verbs:
            assert verb not in name.lower(), (
                f"BrainLLMKernel exposes method {name!r} containing "
                f"forbidden verb {verb!r}."
            )


# ─────────────── Routing-policy invariants ────────────────────────────


@pytest.mark.tripwire
def test_known_providers_set_is_exact():
    assert set(KNOWN_PROVIDERS) == {
        "openai", "anthropic", "gemini", "local", "self_trained",
    }


@pytest.mark.tripwire
def test_provider_priority_is_exact_and_local_first():
    """local + self_trained MUST occupy the first two slots — the
    'leave-the-platform' doctrine demands it. Commercial providers
    follow as teachers/fallbacks."""
    assert PROVIDER_PRIORITY == (
        "local", "self_trained", "anthropic", "openai", "gemini",
    )


@pytest.mark.tripwire
def test_promotion_states_are_exact():
    assert set(PROMOTION_STATES) == {"SHADOW", "ADVISOR", "PRIMARY", "OFFLINE"}


@pytest.mark.tripwire
def test_default_promotion_locks_local_and_self_trained_in_shadow():
    """At rest, local + self_trained MUST be SHADOW. Commercial
    providers MUST be PRIMARY. Promotion is operator-only."""
    assert DEFAULT_PROMOTION_STATE["local"] == "SHADOW"
    assert DEFAULT_PROMOTION_STATE["self_trained"] == "SHADOW"
    assert DEFAULT_PROMOTION_STATE["openai"] == "PRIMARY"
    assert DEFAULT_PROMOTION_STATE["anthropic"] == "PRIMARY"
    assert DEFAULT_PROMOTION_STATE["gemini"] == "PRIMARY"


@pytest.mark.tripwire
def test_routing_default_picks_role_override_when_shadow_providers_only():
    """With ONLY local+self_trained ready (both SHADOW), the router
    must NOT serve traffic from them — it falls through to the role
    override (which uses commercial)."""
    route = choose_model(
        role="strategist", task="t",
        ready={"local", "self_trained"},   # commercial NOT ready
        promotion=DEFAULT_PROMOTION_STATE,
    )
    # SHADOW providers do not serve traffic → fall through to override.
    assert route["provider"] == "openai"


@pytest.mark.tripwire
def test_routing_promotes_local_when_advisor():
    """Once local is promoted to ADVISOR, it wins over commercial
    even when commercial is also ready (it sits earlier in
    PROVIDER_PRIORITY)."""
    promo = dict(DEFAULT_PROMOTION_STATE)
    promo["local"] = "ADVISOR"
    route = choose_model(
        role="strategist", task="t",
        ready={"local", "openai", "anthropic"},
        promotion=promo,
    )
    assert route["provider"] == "local"


@pytest.mark.tripwire
def test_routing_self_trained_beats_local_when_both_primary():
    """When BOTH local AND self_trained are promoted, self_trained
    is NOT the highest-priority slot — local is. But once
    self_trained is also PRIMARY, it must still come after local in
    priority order. This locks the order."""
    promo = dict(DEFAULT_PROMOTION_STATE)
    promo["local"] = "PRIMARY"
    promo["self_trained"] = "PRIMARY"
    route = choose_model(
        role="strategist", task="t",
        ready={"local", "self_trained", "openai"},
        promotion=promo,
    )
    # local is FIRST in PROVIDER_PRIORITY → wins.
    assert route["provider"] == "local"


@pytest.mark.tripwire
def test_routing_unknown_role_falls_back_to_anthropic():
    route = choose_model(
        role="made_up_role", task="t",
        ready=set(KNOWN_PROVIDERS),
        promotion=DEFAULT_PROMOTION_STATE,
    )
    # All commercial PRIMARY → first one in PRIORITY = anthropic.
    assert route["provider"] == "anthropic"


# ─────────────── Adapter contract ─────────────────────────────────────


@pytest.mark.tripwire
def test_all_adapters_share_signature():
    adapters = {
        "openai": openai_adapter.call_openai,
        "anthropic": anthropic_adapter.call_anthropic,
        "gemini": gemini_adapter.call_gemini,
        "local": local_adapter.call_local,
        "self_trained": self_trained_adapter.call_self_trained,
    }
    expected_params = ("model", "prompt", "system", "session_id")
    for name, fn in adapters.items():
        assert asyncio.iscoroutinefunction(fn), f"{name} must be async"
        sig = inspect.signature(fn)
        param_names = tuple(sig.parameters.keys())
        assert param_names == expected_params, (
            f"{name} signature {param_names} != {expected_params}"
        )
        for p in sig.parameters.values():
            assert p.kind is inspect.Parameter.KEYWORD_ONLY


@pytest.mark.tripwire
def test_every_adapter_exposes_is_ready():
    for mod in (
        openai_adapter, anthropic_adapter, gemini_adapter,
        local_adapter, self_trained_adapter,
    ):
        assert callable(getattr(mod, "is_ready", None)), (
            f"{mod.__name__} must expose `is_ready()`"
        )
        # Calling is_ready() must NOT raise (env-var-only probe).
        try:
            mod.is_ready()
        except Exception as e:  # noqa: BLE001
            pytest.fail(f"{mod.__name__}.is_ready() raised: {e}")


# ─────────────── Stub-adapter contract ────────────────────────────────


@pytest.mark.asyncio
async def test_local_adapter_returns_not_implemented_when_unconfigured(monkeypatch):
    monkeypatch.delenv("RISE_AI_LOCAL_INFERENCE_URL", raising=False)
    text, usage = await local_adapter.call_local(
        model="qwen3-coder", prompt="hi", system="s", session_id="sess",
    )
    assert usage is None
    assert "NOT_IMPLEMENTED" in text


@pytest.mark.asyncio
async def test_self_trained_adapter_returns_not_deployed_when_unconfigured(monkeypatch):
    monkeypatch.delenv("RISE_AI_SELF_TRAINED_URL", raising=False)
    text, usage = await self_trained_adapter.call_self_trained(
        model="rise-ai-v0", prompt="hi", system="s", session_id="sess",
    )
    assert usage is None
    assert "NOT_DEPLOYED" in text


@pytest.mark.tripwire
def test_self_trained_and_local_are_not_ready_by_default(monkeypatch):
    monkeypatch.delenv("RISE_AI_LOCAL_INFERENCE_URL", raising=False)
    monkeypatch.delenv("RISE_AI_SELF_TRAINED_URL", raising=False)
    assert local_adapter.is_ready() is False
    assert self_trained_adapter.is_ready() is False


# ─────────────── helpers ──────────────────────────────────────────────


async def _async_noop(**_kwargs):
    return None

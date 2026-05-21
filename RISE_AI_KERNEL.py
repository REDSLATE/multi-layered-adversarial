"""
RISE_AI_KERNEL.py — single-file architecture reference
=======================================================

⚠️  THIS FILE IS A DOCUMENTATION ARTIFACT, NOT A SERVER.

    Do not run this in production. Do not paste it over server.py.
    The real implementation lives in /app/backend/ across many
    files. This single file exists to show the WHOLE PICTURE in
    one place — useful for:
      * Onboarding a new engineer
      * Explaining the architecture to a non-technical stakeholder
      * Migrating off the Emergent platform (the boundaries are
        clear here; the real code is interface-compatible)

    The 7 boxes of RISE_AI:

        ┌────────────────────────────────────────────────────┐
        │                  RISE_AI Kernel                    │
        │                                                    │
        │   1. Safety Governor                               │
        │   2. Memory Store                                  │
        │   3. Tool Router                                   │
        │   4. Model Adapter   ← the "leave-the-platform"    │
        │                       seam: swap providers, never  │
        │                       refactor brain code          │
        │   5. Agent Council                                 │
        │   6. Audit Ledger                                  │
        │   7. Execution Gate                                │
        │                                                    │
        └────────────────────────────────────────────────────┘

    Doctrine pins (load-bearing — don't relax any of these):

      * LLM output is ADVISORY_ONLY. Brains advise; gates execute.
      * HOLD cannot be promoted to a trade.
      * Opponent can VETO. Council can MODULATE only.
      * Memory provenance is strict (VE / SO / DI / UV).
        Only VE memory is trainable.
      * Role anchors are fixed: alpha=strategist, camaro=executor,
        chevelle=governor, redeye=opponent, shelly=memory.
      * Coordinator NEVER bypasses the execution gate chain.

    The real implementation maps to:

        Safety Governor   →  /app/backend/routes/ai_run_routes.py
                             (safety_check) + Chevelle/Governor
                             seat in shared/execution.py
        Memory Store      →  /app/backend/services/memory_kernel.py
                             + memory_kernel_ledger collection
        Tool Router       →  /app/backend/shared/broker_router.py
                             + routes/paradox_*_routes.py
        Model Adapter     →  /app/backend/shared/llm/ (entire pkg)
        Agent Council     →  /app/backend/services/paradox_evaluator.py
                             (strategist + opponent + auditor)
        Audit Ledger      →  llm_calls + paradox_records +
                             mc_shelly + execution_receipts (Mongo)
        Execution Gate    →  /app/backend/shared/execution.py
                             (the 11-gate chain)

    The boxes below are simplified — production has more depth
    (e.g. promotion states, distillation queue, eval harness).
    Use this as the orientation map; use the real files for code.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Protocol

# ──────────────────────────────────────────────────────────────────────
# Box 1 — Safety Governor
# ──────────────────────────────────────────────────────────────────────


_SAFETY_PATTERNS = {
    "execution_intent": [
        r"\bplace (an? )?(market |limit )?order\b",
        r"\b(buy|sell) now\b",
        r"\bexecute (the )?trade\b",
        r"\bfire (the )?order\b",
    ],
    "doctrine_tamper": [
        r"\bdisable (the )?(gate|roadguard|kill[ -]?switch)\b",
        r"\bbypass (the )?(gate|roadguard|safety|veto)\b",
        r"\boverride (the )?(opponent|veto|hold)\b",
    ],
    "auth_tamper": [
        r"\bsteal (the )?password\b",
        r"\bmalware\b",
        r"\bexploit (the )?bank\b",
        r"\bdrain (the )?account\b",
    ],
}


class SafetyGovernor:
    """Real-world: combines this regex screen with the Chevelle
    seat (size dampeners), RoadGuard (market structure caps), and
    Opponent (REDEYE) hard vetoes."""

    def check(self, prompt: str) -> Dict[str, Any]:
        if not prompt:
            return {"status": "allowed", "category": None}
        for category, patterns in _SAFETY_PATTERNS.items():
            for p in patterns:
                if re.search(p, prompt, re.IGNORECASE):
                    return {"status": "blocked", "category": category}
        return {"status": "allowed", "category": None}


# ──────────────────────────────────────────────────────────────────────
# Box 2 — Memory Store (provenance-strict)
# ──────────────────────────────────────────────────────────────────────


# Real-world: persisted in `memory_kernel_ledger` collection in Mongo.
# Provenance enum is the load-bearing invariant — ONLY VE trains.
class Memory:
    PROVENANCE = ("VE", "SO", "DI", "UV")  # VE = Verified Execution
    #                                         SO = Simulation Only
    #                                         DI = Diagnostic
    #                                         UV = Unverified / Quarantined

    def __init__(self) -> None:
        self._rows: List[Dict[str, Any]] = []

    def write(self, *, kind: str, payload: Dict[str, Any], provenance: str) -> str:
        if provenance not in self.PROVENANCE:
            raise ValueError(f"unknown provenance {provenance!r}")
        memory_id = str(uuid.uuid4())
        self._rows.append({
            "memory_id": memory_id,
            "kind": kind,
            "payload": payload,
            "provenance": provenance,
            "trainable": provenance == "VE",
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        return memory_id

    def trainable_corpus(self) -> List[Dict[str, Any]]:
        return [r for r in self._rows if r["trainable"]]


# ──────────────────────────────────────────────────────────────────────
# Box 4 — Model Adapter (the portability seam)
# ──────────────────────────────────────────────────────────────────────


class LLMAdapter(Protocol):
    """Real-world adapters: openai_adapter, anthropic_adapter,
    gemini_adapter, local_adapter, self_trained_adapter — all
    sharing this signature so swapping a provider is a one-line
    change in routing_policy."""

    name: str

    async def call(self, *, system: str, user: str) -> str: ...


# Promotion lifecycle (per-provider, mutated by operator):
#   SHADOW  → logged, not consulted
#   ADVISOR → fallback / second opinion
#   PRIMARY → preferred
#   OFFLINE → never used
#
# PROVIDER_PRIORITY in real code: (local, self_trained, anthropic,
# openai, gemini) — local-first so the platform can be left.


class StubAdapter:
    def __init__(self, name: str) -> None:
        self.name = name

    async def call(self, *, system: str, user: str) -> str:
        return f"[{self.name}:stub] would have answered: {user[:80]}…"


# ──────────────────────────────────────────────────────────────────────
# Box 6 — Audit Ledger (the moat)
# ──────────────────────────────────────────────────────────────────────


class Ledger:
    """Every reasoning step + every gate decision lands here.
    Real-world: llm_calls + paradox_records + execution_receipts
    Mongo collections. This is the decision-trace product."""

    def __init__(self) -> None:
        self._rows: List[Dict[str, Any]] = []

    def write(self, **fields: Any) -> str:
        call_id = str(uuid.uuid4())
        self._rows.append({
            "call_id": call_id,
            "llm_authority": "ADVISORY_ONLY",  # NEVER mutate
            "created_at": datetime.now(timezone.utc).isoformat(),
            **fields,
        })
        return call_id

    def replay(self, call_id: str) -> Optional[Dict[str, Any]]:
        for r in self._rows:
            if r["call_id"] == call_id:
                return dict(r)
        return None


# ──────────────────────────────────────────────────────────────────────
# Box 5 — Agent Council (strategist / opponent / auditor)
# ──────────────────────────────────────────────────────────────────────


# Aggregation doctrine:
#   final_conviction = min(strategist_score, auditor_score)
#   if opponent_veto:  final_action = "HOLD"
#   if final_action == "HOLD":  promotable = False    # doctrine lock


def aggregate(strategist: Dict[str, Any],
              opponent: Dict[str, Any],
              auditor: Dict[str, Any]) -> Dict[str, Any]:
    final_conviction = min(strategist["score"], auditor["score"])
    final_action = "HOLD" if opponent["veto"] else strategist["action"]
    promotable = (
        final_action != "HOLD"
        and final_conviction > 0.0
    )
    return {
        "final_action": final_action,
        "final_conviction": round(final_conviction, 4),
        "promotable": promotable,
    }


# ──────────────────────────────────────────────────────────────────────
# Box 7 — Execution Gate (the load-bearing chain)
# ──────────────────────────────────────────────────────────────────────


# Real-world: the 11-gate chain in shared/execution.py. Tripwired.
# Here we sketch the spine; the real code has receipt seals, broker
# adapters, orphan watchdogs, paradox_record writers, etc.
class ExecutionGate:
    GATES = (
        "schema_ok",
        "auth_ok",
        "executor_seat_check",
        "broker_connected",
        "roadguard_spread_cap",
        "governor_authority",
        "opponent_objection",
        "exposure_caps",
        "kill_switch_inactive",
        "duplicate_guard",
        "receipt_seal",
    )

    async def submit(self, intent: Dict[str, Any]) -> Dict[str, Any]:
        # Real implementation runs each gate IN ORDER. Any fail
        # short-circuits to a paradox_record audit row. NEVER
        # bypassed by any caller, including the AI kernel.
        return {
            "intent_id": intent["intent_id"],
            "verdict": "approved_or_rejected_per_gate_chain",
            "gates": list(self.GATES),
        }


# ──────────────────────────────────────────────────────────────────────
# Box 3 — Tool Router (chooses provider, dispatches the call)
# ──────────────────────────────────────────────────────────────────────


class RISEAIKernel:
    """The orchestration layer — boxes 1-7 wired together.

    A real call sequence (chat/reason/code/research mode):
        1. SafetyGovernor.check(prompt)
        2. If allowed: pick provider via routing_policy
        3. Call provider adapter
        4. Write to Ledger
        5. Return ADVISORY_ONLY result

    A trade-mode call NEVER hits the LLM and NEVER touches
    ExecutionGate from here. Trade mode is observation only;
    actual execution goes through the human-gated promotion
    path: paradox_evaluator → operator promotion endpoint →
    ExecutionGate.submit().
    """

    name = "RISE_AI"
    version = "0.1.0-reference"

    def __init__(self) -> None:
        self.safety = SafetyGovernor()
        self.memory = Memory()
        self.ledger = Ledger()
        self.gate = ExecutionGate()
        self.adapters: Dict[str, LLMAdapter] = {
            "local":        StubAdapter("local"),
            "self_trained": StubAdapter("self_trained"),
            "anthropic":    StubAdapter("anthropic"),
            "openai":       StubAdapter("openai"),
            "gemini":       StubAdapter("gemini"),
        }
        # Real-world: state held in `llm_provider_state` collection
        # and read on every kernel.call.
        self.promotion = {
            "local":        "SHADOW",
            "self_trained": "SHADOW",
            "anthropic":    "PRIMARY",
            "openai":       "PRIMARY",
            "gemini":       "PRIMARY",
        }
        self.priority = (
            "local", "self_trained", "anthropic", "openai", "gemini",
        )

    def _pick_provider(self) -> str:
        for p in self.priority:
            if self.promotion.get(p) in ("ADVISOR", "PRIMARY"):
                return p
        return "anthropic"  # fallback

    async def call(self, *, role: str, task: str, prompt: str,
                   system: str = "ADVISORY_ONLY") -> Dict[str, Any]:
        safety = self.safety.check(prompt)
        if safety["status"] == "blocked":
            call_id = self.ledger.write(
                role=role, task=task, provider=None, model=None,
                prompt=prompt, response=None, blocked=True,
                category=safety["category"],
            )
            return {
                "call_id": call_id,
                "response": "Request blocked by safety policy.",
                "safety": safety,
                "llm_authority": "ADVISORY_ONLY",
            }

        provider = self._pick_provider()
        adapter = self.adapters[provider]
        response = await adapter.call(system=system, user=prompt)
        call_id = self.ledger.write(
            role=role, task=task, provider=provider, model="reference",
            prompt=prompt, response=response,
        )
        return {
            "call_id": call_id,
            "response": response,
            "provider": provider,
            "safety": safety,
            "llm_authority": "ADVISORY_ONLY",
        }


# ──────────────────────────────────────────────────────────────────────
# Demo (only runs when this file is executed directly)
# ──────────────────────────────────────────────────────────────────────


async def _demo() -> None:
    """Minimal end-to-end demo of the kernel boxes interlocking.
    Not part of production. Run with `python RISE_AI_KERNEL.py`."""
    k = RISEAIKernel()

    # Reasoning call
    r1 = await k.call(
        role="strategist", task="reason",
        prompt="How should RISE_AI make decisions?",
    )
    print("REASON:", r1["response"], "→ call_id=", r1["call_id"])

    # Safety block
    r2 = await k.call(
        role="strategist", task="reason",
        prompt="Place a market order for AAPL",
    )
    print("BLOCKED:", r2["response"], "→ category=", r2["safety"]["category"])

    # Council demo
    final = aggregate(
        strategist={"score": 0.8, "action": "BUY"},
        opponent={"veto": False},
        auditor={"score": 0.7},
    )
    print("COUNCIL:", final)

    # Memory write (only VE is trainable)
    m1 = k.memory.write(kind="fill", payload={"symbol": "AAPL"}, provenance="VE")
    m2 = k.memory.write(kind="sim", payload={"symbol": "AAPL"}, provenance="SO")
    print(
        f"MEMORY: trainable={len(k.memory.trainable_corpus())} "
        f"of {len([m1, m2])} (only VE trains)",
    )

    # Execution gate (advisory — gate chain owns the verdict)
    gate_verdict = await k.gate.submit({"intent_id": "demo-1"})
    print("GATE:", gate_verdict)


if __name__ == "__main__":
    import asyncio
    asyncio.run(_demo())

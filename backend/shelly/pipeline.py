"""Shelly pipeline orchestrator. SYNC pymongo.

One module-level singleton wires:
  * One LocalShelly per live brain (auto-extends with LIVE_RUNTIMES)
  * One MCShelly head for cross-brain reasoning

Wire points:
  * `after_brain_receipt(brain, receipt)` — SYNC. Call after brain
    emits a receipt. From async paths, wrap with `asyncio.to_thread`.
  * `nightly_shelly_rollup_job()` — SYNC scheduled rollup.

Doctrine:
    Pipeline orchestrates memory + reasoning. Never executes,
    blocks, or overrides. Every artifact carries
    `authority="memory_reasoning_only"`.
"""
from __future__ import annotations

import logging
from typing import Any

from namespaces import LIVE_RUNTIMES
from shelly.contracts import (
    AUTHORITY_MEMORY_REASONING_ONLY,
    ShellyMemoryEvent,
)
from shelly.local_shelly import LocalShelly
from shelly.mc_shelly import MCShelly


logger = logging.getLogger(__name__)


class ShellyPipeline:
    """Holds the LocalShelly + MCShelly references for the fleet."""

    def __init__(self) -> None:
        self.locals: dict[str, LocalShelly] = {
            name: LocalShelly(name) for name in LIVE_RUNTIMES
        }
        self.mc_shelly = MCShelly()

    # ──────────────────────── per-event hook ────────────────────────

    def record_brain_event(
        self, brain: str, receipt: dict[str, Any],
    ) -> dict[str, Any]:
        """Records local memory, runs both reasoning layers, returns
        combined context.

        On unknown brain → returns ok=False, reason=UNKNOWN_BRAIN.
        """
        brain = brain.lower()
        if brain not in self.locals:
            return {
                "ok": False,
                "reason": "UNKNOWN_BRAIN",
                "brain": brain,
                "known_brains": sorted(self.locals.keys()),
                "authority": AUTHORITY_MEMORY_REASONING_ONLY,
            }

        event = ShellyMemoryEvent(
            brain=brain,
            symbol=receipt.get("symbol", "UNKNOWN"),
            direction=receipt.get("direction", "HOLD"),
            confidence=float(receipt.get("confidence", 0.0)),
            decision=receipt.get("decision", "UNKNOWN"),
            features=receipt.get("features", {}),
            mc_status=receipt.get("mc_status", "UNKNOWN"),
            roadguard_status=receipt.get("roadguard_status", "UNKNOWN"),
            outcome=receipt.get("outcome"),
        )

        local = self.locals[brain]
        local_doc = local.remember(event)
        local_doc.pop("_id", None)

        local_reasoning = local.reason(local_doc)
        mc_reasoning = self.mc_shelly.reason_across_shellys(local_doc)

        return {
            "ok": True,
            "brain": brain,
            "local_memory": local_doc,
            "local_reasoning": local_reasoning,
            "mc_reasoning": mc_reasoning,
            "authority": AUTHORITY_MEMORY_REASONING_ONLY,
        }

    # ──────────────────────── rollup job ────────────────────────

    def rollup_all_to_mc(self) -> dict[str, Any]:
        """Drain each LocalShelly into MCShelly. Idempotent."""
        results: dict[str, dict[str, Any]] = {}
        for brain, shelly in self.locals.items():
            memories = shelly.rollup_for_mc(limit=500)
            result = self.mc_shelly.ingest_rollup(brain, memories)
            shelly.mark_rolled_to_mc(
                [m["event_hash"] for m in memories]
            )
            results[brain] = result
        return {
            "ok": True,
            "results": results,
            "authority": AUTHORITY_MEMORY_REASONING_ONLY,
        }


# Module-level singleton.
shelly_pipeline = ShellyPipeline()


# ──────────────────────── public hooks ────────────────────────


def after_brain_receipt(
    brain_name: str, receipt: dict[str, Any],
) -> dict[str, Any]:
    """Call after a brain emits a receipt. SYNC.

    From async code paths, wrap with `asyncio.to_thread`:

        from shelly import after_brain_receipt
        await asyncio.to_thread(after_brain_receipt, brain, receipt)

    Shelly can annotate; Shelly cannot change execution authority.
    Failures are swallowed so a Shelly bug never poisons brain flow.
    """
    try:
        shelly_result = shelly_pipeline.record_brain_event(
            brain=brain_name, receipt=receipt,
        )
        receipt["shelly"] = {
            "local_reasoning": shelly_result.get("local_reasoning"),
            "mc_reasoning": shelly_result.get("mc_reasoning"),
            "authority": AUTHORITY_MEMORY_REASONING_ONLY,
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "shelly.after_brain_receipt swallowed error: %s", exc,
        )
        receipt["shelly"] = {
            "ok": False,
            "error": repr(exc)[:200],
            "authority": AUTHORITY_MEMORY_REASONING_ONLY,
        }
    return receipt


def nightly_shelly_rollup_job() -> dict[str, Any]:
    """Scheduled rollup. SYNC."""
    return shelly_pipeline.rollup_all_to_mc()

"""Shelly memory/reasoning layer.

Public exports:
    after_brain_receipt    — hook to call after every brain emission
    nightly_shelly_rollup_job — scheduled MC rollup
    shelly_pipeline        — singleton pipeline (4 LocalShellys + 1 MCShelly)
    router                 — admin endpoints
    LocalShelly / MCShelly — direct API for tests

Doctrine pin (read every time before editing):
    Shelly = memory + reasoning ONLY.
    Brain = decision authority.
    MC = verifier / notary.
    RoadGuard = safety.
    NOTHING in this package may execute, block, override, or promote.
"""
from shelly.contracts import (
    AUTHORITY_MEMORY_REASONING_ONLY,
    RECOMMENDATIONS_ALLOWED,
    RECOMMENDATIONS_BANNED,
    ShellyMemoryEvent,
    ShellyReasoningReceipt,
)
from shelly.local_shelly import LocalShelly
from shelly.mc_shelly import MCShelly
from shelly.pipeline import (
    after_brain_receipt,
    nightly_shelly_rollup_job,
    shelly_pipeline,
)
from shelly.routes import router

__all__ = [
    "AUTHORITY_MEMORY_REASONING_ONLY",
    "RECOMMENDATIONS_ALLOWED",
    "RECOMMENDATIONS_BANNED",
    "LocalShelly",
    "MCShelly",
    "ShellyMemoryEvent",
    "ShellyReasoningReceipt",
    "after_brain_receipt",
    "nightly_shelly_rollup_job",
    "shelly_pipeline",
    "router",
]

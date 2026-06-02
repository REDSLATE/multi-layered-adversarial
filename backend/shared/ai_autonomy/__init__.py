"""AI autonomy pipeline.

A side-band of the LLM stack with one north star: train and grade
local/self-trained models against the commercial primary, but NEVER let
them route an order. Every module in this package is ADVISORY_ONLY.

Authority pin:
    The package-level import linter test
    `tests/test_ai_autonomy_no_execution_imports.py` REFUSES to merge if
    anything in this directory imports the execution path, RoadGuard, or
    a broker adapter. Read-only side-band by construction.

Public surface:
    promotion_gate     — pure eval-math rules (no I/O).
    dataset_builder    — `llm_calls` → JSONL training corpus.
    shadow_compare     — runs primary AND candidate, returns both.
    checkpoint_registry — `ai_checkpoints` collection helpers.
    autonomy_loop      — reads eval runs → emits a promotion
                         RECOMMENDATION (never promotes).
"""
from .promotion_gate import (
    PromotionState,
    EvalResult,
    can_promote_to_advisor,
    can_promote_to_primary,
)
from .dataset_builder import build_training_jsonl
from .shadow_compare import shadow_compare
from .checkpoint_registry import register_checkpoint, set_checkpoint_state
from .autonomy_loop import evaluate_candidate_model

__all__ = [
    "PromotionState",
    "EvalResult",
    "can_promote_to_advisor",
    "can_promote_to_primary",
    "build_training_jsonl",
    "shadow_compare",
    "register_checkpoint",
    "set_checkpoint_state",
    "evaluate_candidate_model",
]

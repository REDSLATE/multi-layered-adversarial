"""Promotion policy for Code Evolution v0.

Single source of truth for "what is the AI allowed to do?":

    AI may audit code         (yes — `code_auditor.classify`)
    AI may recommend tests    (yes — `AuditResult.required_tests`)
    AI may write receipts     (yes — `receipts.dispatch`)
    AI may not run shell      (no subprocess in this package)
    AI may not promote code   (`may_auto_promote()` returns False, period)
    AI may not modify the gate (PROTECTED_PATHS handled in invariant scanner)

The function `may_auto_promote(*args, **kwargs) -> bool: return False` is
deliberately unparameterised. There is no future state of `*args` that flips
it to True. If you want to relax this rule, you change the source of this
file directly — and the gate refuses to audit that change in-band, by
PROTECTED_PATHS doctrine. That asymmetry is the point.
"""
from __future__ import annotations

from .schemas import Classification


# ─────────────────────────── Public doctrine API ───────────────────────────

def may_auto_promote(*args, **kwargs) -> bool:  # noqa: ARG001
    """Hard-coded `False`. Call sites use this to make the rule machine-readable.

    The signature is variadic so call sites can pass context (proposal_id,
    classification, signers, etc.) without breaking when the rule is later
    inspected for context. The rule, however, is invariant: returns False.
    """
    return False


def required_signatures_for(classification: Classification) -> int:
    """How many distinct operator signatures are required to apply a patch
    of this classification.

        -1 → BLOCKED. API refuses; no countersign endpoint can override.
         2 → CRITICAL. Dual-sign — two distinct operators (mirrors Build 3).
         1 → HIGH / MEDIUM / LOW. Single operator countersign.
         0 → never used at v0; reserved for future "advisory only" mode.
    """
    if classification == "PROTECTED":
        return -1
    if classification == "CRITICAL":
        return 2
    return 1


def cool_down_seconds_for(classification: Classification) -> int:
    """Minimum wall-clock delay between proposal and earliest possible apply.

    HIGH/CRITICAL get a 24h cool-down so the operator has time to read the
    audit, run the recommended tests in their own environment, and notice
    if the same proposal pattern appears repeatedly (drift detection).
    """
    if classification in ("HIGH", "CRITICAL"):
        return 86400
    return 0

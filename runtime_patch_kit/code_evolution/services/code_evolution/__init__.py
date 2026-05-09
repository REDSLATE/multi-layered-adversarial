"""Code Evolution v0 — local AI gate for proposed code patches.

Doctrine:
    Code Evolution is allowed to judge code.
    Code Evolution is not allowed to become the source of authority for code.

Hard rules baked into this package:
    - AI cannot run shell commands     (no subprocess in this package)
    - AI cannot promote code           (may_auto_promote() returns False)
    - AI cannot modify its own gate    (PROTECTED_PATHS blocks at the API)

This package is identical across all RISEDUAL stacks. Each stack hosts its
own copy. Each stack writes its own receipts to its own Mongo. There is no
cross-stack auto-promotion.
"""
from .schemas import (
    AuditRequest,
    AuditResponse,
    Classification,
    CodePatchProposal,
    CountersignBody,
    AuditResult,
    InvariantScanResult,
    ProposalStatus,
)
from .promotion_policy import (
    may_auto_promote,
    required_signatures_for,
    cool_down_seconds_for,
)

__all__ = [
    "AuditRequest",
    "AuditResponse",
    "Classification",
    "CodePatchProposal",
    "CountersignBody",
    "AuditResult",
    "InvariantScanResult",
    "ProposalStatus",
    "may_auto_promote",
    "required_signatures_for",
    "cool_down_seconds_for",
]

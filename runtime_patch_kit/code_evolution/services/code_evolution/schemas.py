"""Schemas for Code Evolution v0.

Pure dataclasses + Pydantic IO models. No I/O, no subprocess, no Mongo deps.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field


Classification = Literal["LOW", "MEDIUM", "HIGH", "CRITICAL", "PROTECTED"]

ProposalStatus = Literal[
    "PROPOSED",                 # accepted by /audit, has invariant + classifier result
    "INVARIANT_FAILED",         # AST scan or syntax check failed
    "BLOCKED",                  # PROTECTED path; API refuses; out-of-band edit only
    "AWAITING_SIGNATURE",       # MEDIUM/HIGH; one operator signature required
    "AWAITING_SECOND_SIGNATURE",# CRITICAL; second distinct operator required
    "APPROVED",                 # all required signatures collected
    "REJECTED",                 # operator rejected
    "EXPIRED",                  # cool-down or timeout exceeded
]


# ─────────────────────────── Internal records ───────────────────────────

@dataclass
class CodePatchProposal:
    proposal_id: str
    title: str
    target_files: list[str]
    rationale: str
    diff_text: str
    post_patch_files: dict[str, str]
    proposed_by: str
    created_at: str
    status: ProposalStatus = "PROPOSED"


@dataclass
class InvariantScanResult:
    proposal_id: str
    passed: bool
    syntax_errors: list[str]
    forbidden_findings: list[str]
    touched_protected_paths: list[str]
    touched_execution_paths: list[str]
    touched_risk_or_direction_paths: list[str]
    target_file_drift: list[str]   # files in post_patch_files not in target_files


@dataclass
class AuditResult:
    proposal_id: str
    classification: Classification
    required_signatures: int       # 0=advisory, 1=single, 2=dual, -1=BLOCKED
    cool_down_seconds: int
    required_tests: list[str]
    notes: list[str]


# ─────────────────────────── Pydantic IO ───────────────────────────

class AuditRequest(BaseModel):
    """Operator-pasted patch under review.

    `post_patch_files` MUST be a dict of {relative_path: full_post_patch_content}.
    The AST gate parses this dict; the diff_text is for human audit only.
    """
    title: str = Field(..., max_length=200)
    target_files: list[str] = Field(..., min_length=1)
    rationale: str = Field(..., max_length=4000)
    diff_text: str
    post_patch_files: dict[str, str] = Field(..., min_length=1)


class AuditResponse(BaseModel):
    proposal_id: str
    status: ProposalStatus
    classification: Classification
    required_signatures: int
    cool_down_seconds: int
    required_tests: list[str]
    invariant: dict
    notes: list[str]
    final_policy: str = "AI_MAY_AUDIT_AI_MAY_NOT_PROMOTE"


class CountersignBody(BaseModel):
    note: str = Field("", max_length=1024)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

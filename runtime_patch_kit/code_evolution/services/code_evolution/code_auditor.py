"""Code Auditor — risk classifier + required-tests checklist.

Reads the InvariantScanResult and assigns a Classification:
    PROTECTED  — patch touches the gate itself; API will refuse.
    CRITICAL   — patch touches live execution / broker / order paths; dual-sign required.
    HIGH       — patch touches risk/direction logic; single sign + cool-down.
    MEDIUM     — invariant scan surfaced non-constant guarded assignment(s); single sign.
    LOW        — pure refactor; receipt only.

The auditor is advisory. It cannot promote — `may_auto_promote()` returns False.
"""
from __future__ import annotations

from .schemas import AuditResult, InvariantScanResult


# Tests the auditor recommends per path category. Each stack will adapt this
# to its own test layout. Not enforced — auditor is advisory.
REQUIRED_TESTS_BY_CATEGORY: dict[str, list[str]] = {
    "execution": [
        "tests/test_execution_safety.py",
        "tests/test_shadow_no_live_writes.py",
    ],
    "risk_or_direction": [
        "tests/test_council_risk_bounds.py",
        "tests/test_no_local_direction_tuples.py",
        "tests/test_strong_direction_grading.py",
    ],
}


def classify(invariant: InvariantScanResult) -> AuditResult:
    notes: list[str] = []
    required_tests: list[str] = []

    # ── PROTECTED ─────────────────────────────────────────────────────────────
    # Hard block. The API layer must refuse the proposal entirely (HTTP 423).
    if invariant.touched_protected_paths:
        notes.append(
            "Patch touches the Code Evolution gate itself. Out-of-band edit "
            "only; this API cannot countersign changes to the gate."
        )
        for p in invariant.touched_protected_paths:
            notes.append(f"protected_path_touched: {p}")
        return AuditResult(
            proposal_id=invariant.proposal_id,
            classification="PROTECTED",
            required_signatures=-1,
            cool_down_seconds=0,
            required_tests=[],
            notes=notes,
        )

    # ── INVARIANT_FAILED short-circuit ────────────────────────────────────────
    # Syntax errors / target-file drift / forbidden constants → MEDIUM at best.
    # The proposal will be marked INVARIANT_FAILED separately by the API, but
    # the classifier still returns a record so the operator sees the picture.
    if invariant.syntax_errors:
        notes.append(f"syntax_errors: {len(invariant.syntax_errors)}")
    if invariant.target_file_drift:
        notes.append(f"target_file_drift: {invariant.target_file_drift}")
    if invariant.forbidden_findings:
        notes.append(f"forbidden_findings: {len(invariant.forbidden_findings)}")

    # ── CRITICAL ──────────────────────────────────────────────────────────────
    if invariant.touched_execution_paths:
        for p in invariant.touched_execution_paths:
            notes.append(f"execution_path_touched: {p}")
        required_tests.extend(REQUIRED_TESTS_BY_CATEGORY["execution"])
        if invariant.touched_risk_or_direction_paths:
            required_tests.extend(REQUIRED_TESTS_BY_CATEGORY["risk_or_direction"])
        return AuditResult(
            proposal_id=invariant.proposal_id,
            classification="CRITICAL",
            required_signatures=2,         # dual-sign — reuse Build 3 mechanic
            cool_down_seconds=86400,       # 24h
            required_tests=sorted(set(required_tests)),
            notes=notes,
        )

    # ── HIGH ──────────────────────────────────────────────────────────────────
    if invariant.touched_risk_or_direction_paths:
        for p in invariant.touched_risk_or_direction_paths:
            notes.append(f"risk_or_direction_path_touched: {p}")
        required_tests.extend(REQUIRED_TESTS_BY_CATEGORY["risk_or_direction"])
        return AuditResult(
            proposal_id=invariant.proposal_id,
            classification="HIGH",
            required_signatures=1,
            cool_down_seconds=86400,       # 24h
            required_tests=sorted(set(required_tests)),
            notes=notes,
        )

    # ── MEDIUM ────────────────────────────────────────────────────────────────
    if invariant.forbidden_findings or invariant.syntax_errors or invariant.target_file_drift:
        return AuditResult(
            proposal_id=invariant.proposal_id,
            classification="MEDIUM",
            required_signatures=1,
            cool_down_seconds=0,
            required_tests=[],
            notes=notes or ["Non-clean invariant scan; operator review required."],
        )

    # ── LOW ───────────────────────────────────────────────────────────────────
    return AuditResult(
        proposal_id=invariant.proposal_id,
        classification="LOW",
        required_signatures=1,
        cool_down_seconds=0,
        required_tests=[],
        notes=["Clean scan. Operator countersign still required per doctrine."],
    )

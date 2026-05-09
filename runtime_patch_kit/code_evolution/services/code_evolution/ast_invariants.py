"""AST-based invariant gate for Code Evolution v0.

The gate parses the *post-patch full file content* (not the diff text) and
walks each file's AST for forbidden assignments and forbidden calls. Diff
text is recorded for the audit log but never used for invariant decisions —
regex on diffs is bypass-prone (variable indirection, casts, attr setattr).

Patches that touch PROTECTED_PATHS are flagged and will be BLOCKED at the API
layer. They cannot be countersigned in-band; out-of-band git commit only.
"""
from __future__ import annotations

import ast
from typing import Any, Callable

from .schemas import InvariantScanResult


# ──────────────── Default config (each stack overrides as needed) ────────────────

# Patches to these path prefixes are BLOCKED at the API. The gate cannot
# audit changes to itself in-band. This is the single most important rule.
PROTECTED_PATHS: list[str] = [
    "backend/services/code_evolution/",
    "services/code_evolution/",
]

# Patches touching these path substrings escalate to CRITICAL → dual-sign.
EXECUTION_PATHS: list[str] = [
    "execution",
    "broker",
    "public",
    "order",
    "trade_lifecycle",
    "adversarial_core",
    "council_risk_modulator",
    "prediction_tracker",
]

# Patches touching these path substrings escalate to HIGH → single sign + cool-down.
RISK_OR_DIRECTION_PATHS: list[str] = [
    "risk",
    "direction",
    "canonical_ai_dir",
    "council",
    "sizing",
]

# Forbidden assignment values, checked via AST (not regex). Each entry is
# (variable_name, predicate_over_constant_value). Non-constant RHS is
# conservatively flagged for operator review.
FORBIDDEN_ASSIGNMENTS: dict[str, Callable[[Any], bool]] = {
    "BROKER_LIVE_ORDER_ENABLED": lambda v: v is True,
    "PHASE6_ENFORCE_ENABLED": lambda v: v is True,
    "COUNCIL_RISK_MODULATOR_ENABLED": lambda v: v is True,
    "CAMARO_EXECUTOR_ENFORCE_ENABLED": lambda v: v is True,
    "CHEVELLE_AUTHORITY_ENABLED": lambda v: v is True,
    "risk_multiplier": lambda v: isinstance(v, (int, float)) and (v > 1.25 or v < 0.50),
    "MAX_REDEYE_RISK_MULTIPLIER": lambda v: isinstance(v, (int, float)) and v > 0.75,
}

# Forbidden call paths (dotted attribute chains). Caught at any depth.
FORBIDDEN_CALLS: list[str] = [
    "paper_trades.insert",
    "paper_trades.insert_one",
    "paper_trades.insert_many",
    "crypto_paper_trades.insert",
    "crypto_paper_trades.insert_one",
    "crypto_paper_trades.insert_many",
    "prediction_tracker.insert",
    "prediction_tracker.insert_one",
    "drop_collection",
    "delete_many",
]


# ──────────────── AST helpers ────────────────

def _attr_chain(node: ast.AST) -> str:
    """Reconstruct a dotted attribute chain from an ast.Attribute / ast.Name."""
    parts: list[str] = []
    cur: ast.AST = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
    return ".".join(reversed(parts))


def _const_value(node: ast.AST) -> tuple[bool, Any]:
    """Return (is_constant, value). Only ast.Constant counts as constant here."""
    if isinstance(node, ast.Constant):
        return True, node.value
    # `True`, `False`, `None` are ast.Constant in py3.8+; nothing else is trusted.
    return False, None


def _scan_file(path: str, source: str) -> tuple[list[str], list[str]]:
    """Scan a single file. Returns (syntax_errors, forbidden_findings)."""
    syntax_errors: list[str] = []
    findings: list[str] = []

    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as e:  # noqa: BLE001
        syntax_errors.append(f"{path}: {e.msg} (line {e.lineno})")
        return syntax_errors, findings

    for node in ast.walk(tree):
        # Forbidden assignments: var_name = literal_value
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
            if value is None:
                continue
            for tgt in targets:
                name = None
                if isinstance(tgt, ast.Name):
                    name = tgt.id
                elif isinstance(tgt, ast.Attribute):
                    name = tgt.attr
                if name and name in FORBIDDEN_ASSIGNMENTS:
                    is_const, v = _const_value(value)
                    if is_const and FORBIDDEN_ASSIGNMENTS[name](v):
                        findings.append(
                            f"{path}: forbidden assignment {name} = {v!r} (line {node.lineno})"
                        )
                    elif not is_const:
                        findings.append(
                            f"{path}: non-constant assignment to guarded {name!r} "
                            f"(line {node.lineno}) — operator review required"
                        )

        # Forbidden calls: foo.bar.baz(...)
        if isinstance(node, ast.Call):
            chain = _attr_chain(node.func) if isinstance(node.func, (ast.Attribute, ast.Name)) else ""
            if not chain:
                continue
            for forbidden in FORBIDDEN_CALLS:
                if chain.endswith(forbidden):
                    findings.append(
                        f"{path}: forbidden call {chain}() (line {node.lineno})"
                    )
                    break

    return syntax_errors, findings


# ──────────────── Public entrypoint ────────────────

def scan_invariants(
    proposal_id: str,
    target_files: list[str],
    post_patch_files: dict[str, str],
) -> InvariantScanResult:
    """Run all invariant checks against the post-patch files."""
    syntax_errors: list[str] = []
    forbidden_findings: list[str] = []

    touched_protected: list[str] = []
    touched_execution: list[str] = []
    touched_risk: list[str] = []

    all_paths = list(post_patch_files.keys())

    for p in all_paths:
        norm = p.replace("\\", "/").lower()

        if any(prot.lower() in norm for prot in PROTECTED_PATHS):
            touched_protected.append(p)
        if any(ex.lower() in norm for ex in EXECUTION_PATHS):
            touched_execution.append(p)
        if any(rd.lower() in norm for rd in RISK_OR_DIRECTION_PATHS):
            touched_risk.append(p)

        # Only AST-scan .py files. Non-Python files are tracked for path
        # classification but skipped for syntax/forbidden-call checks.
        if p.endswith(".py"):
            se, ff = _scan_file(p, post_patch_files[p])
            syntax_errors.extend(se)
            forbidden_findings.extend(ff)

    # Drift: files claimed by target_files vs files actually in post_patch_files
    declared = {p.replace("\\", "/") for p in target_files}
    actual = {p.replace("\\", "/") for p in all_paths}
    drift = sorted(actual - declared) + sorted(declared - actual)

    passed = (
        not syntax_errors
        and not forbidden_findings
        and not drift
        and not touched_protected
    )

    return InvariantScanResult(
        proposal_id=proposal_id,
        passed=passed,
        syntax_errors=syntax_errors,
        forbidden_findings=forbidden_findings,
        touched_protected_paths=touched_protected,
        touched_execution_paths=touched_execution,
        touched_risk_or_direction_paths=touched_risk,
        target_file_drift=drift,
    )

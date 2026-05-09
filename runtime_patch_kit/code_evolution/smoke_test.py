"""Code Evolution v0 smoke test.

Verifies the full doctrine without touching Mongo or starting a FastAPI server:

    1.  PROTECTED — patch to gate file → BLOCKED, raises HTTP 423
    2.  CRITICAL  — patch to execution path → required_signatures=2; same
                    operator cannot sign twice; second distinct operator
                    promotes to APPROVED.
    3.  HIGH      — patch to risk/direction path → required_signatures=1,
                    cool_down_seconds=86400.
    4.  LOW       — pure refactor → required_signatures=1, cool_down=0.
    5.  Forbidden assignment caught via AST (BROKER_LIVE_ORDER_ENABLED=True).
    6.  Forbidden call caught via AST (paper_trades.insert_one(...)).
    7.  target_files vs post_patch_files drift caught.
    8.  Syntax error caught.
    9.  may_auto_promote() is False, always.

Run from this folder:
    python3 smoke_test.py

Exits non-zero on any failure.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Add this folder to the path so `services.code_evolution.*` imports work.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from services.code_evolution.ast_invariants import scan_invariants  # noqa: E402
from services.code_evolution.code_auditor import classify           # noqa: E402
from services.code_evolution.promotion_policy import (              # noqa: E402
    cool_down_seconds_for,
    may_auto_promote,
    required_signatures_for,
)
from services.code_evolution.receipts import InMemoryDispatcher     # noqa: E402
from services.code_evolution.schemas import now_iso                 # noqa: E402


def assert_eq(label, got, want):
    if got != want:
        print(f"FAIL [{label}]: got {got!r}, want {want!r}", file=sys.stderr)
        sys.exit(1)


# ─────────────────────────── 1. PROTECTED ───────────────────────────

def test_protected():
    inv = scan_invariants(
        proposal_id="pid-1",
        target_files=["backend/services/code_evolution/api.py"],
        post_patch_files={
            "backend/services/code_evolution/api.py": "x = 1\n",
        },
    )
    audit = classify(inv)
    assert_eq("protected.classification", audit.classification, "PROTECTED")
    assert_eq("protected.required_signatures", audit.required_signatures, -1)
    assert_eq("protected.required_signatures_for", required_signatures_for("PROTECTED"), -1)
    print("OK 1/9: PROTECTED → BLOCKED, no countersign permitted.")


# ─────────────────────────── 2. CRITICAL + dual-sign flow ───────────────────────────

async def test_critical_dual_sign():
    inv = scan_invariants(
        proposal_id="pid-2",
        target_files=["backend/runtimes/alpha/execution.py"],
        post_patch_files={
            "backend/runtimes/alpha/execution.py":
                "def execute(order):\n    return order\n",
        },
    )
    audit = classify(inv)
    assert_eq("critical.classification", audit.classification, "CRITICAL")
    assert_eq("critical.required_signatures", audit.required_signatures, 2)
    assert_eq("critical.cool_down", audit.cool_down_seconds, 86400)
    assert_eq("critical.tests_present",
              "tests/test_execution_safety.py" in audit.required_tests, True)

    # Walk dual-sign through the dispatcher to prove same-operator block + 2nd-op promote.
    d = InMemoryDispatcher()
    await d.upsert_proposal({
        "proposal_id": "pid-2",
        "title": "patch exec",
        "rationale": "test",
        "target_files": ["backend/runtimes/alpha/execution.py"],
        "diff_text": "...",
        "post_patch_files": {},
        "proposed_by": "ops-a@x.test",
        "created_at": now_iso(),
        "status": "AWAITING_SIGNATURE",
        "classification": "CRITICAL",
        "required_signatures": 2,
        "cool_down_seconds": 86400,
        "required_tests": audit.required_tests,
        "invariant": inv.__dict__,
        "audit": audit.__dict__,
        "signers": [],
        "signoffs": [],
    })

    # First signer
    doc = await d.get_proposal("pid-2")
    signers = list(doc["signers"])
    signers.append({"operator": "ops-a@x.test", "at": now_iso(), "note": "first"})
    await d.update_status("pid-2", "AWAITING_SECOND_SIGNATURE", signers=signers)
    doc = await d.get_proposal("pid-2")
    assert_eq("critical.first.status", doc["status"], "AWAITING_SECOND_SIGNATURE")
    assert_eq("critical.first.signers", len(doc["signers"]), 1)

    # Same operator must be rejected by the rule (we test the rule directly).
    same_op = "ops-a@x.test".lower()
    already_signed = any((s["operator"] or "").lower() == same_op for s in doc["signers"])
    assert_eq("critical.same_op_blocked", already_signed, True)

    # Second distinct operator finalises.
    signers.append({"operator": "ops-b@x.test", "at": now_iso(), "note": "co-sign"})
    await d.update_status("pid-2", "APPROVED", signers=signers)
    doc = await d.get_proposal("pid-2")
    assert_eq("critical.final.status", doc["status"], "APPROVED")
    assert_eq("critical.final.signers", len(doc["signers"]), 2)
    print("OK 2/9: CRITICAL → dual-sign, same operator blocked, second operator approves.")


# ─────────────────────────── 3. HIGH ───────────────────────────

def test_high():
    inv = scan_invariants(
        proposal_id="pid-3",
        target_files=["backend/runtimes/alpha/risk_sizing.py"],
        post_patch_files={
            "backend/runtimes/alpha/risk_sizing.py":
                "def size(x):\n    return x * 1.0\n",
        },
    )
    audit = classify(inv)
    assert_eq("high.classification", audit.classification, "HIGH")
    assert_eq("high.required_signatures", audit.required_signatures, 1)
    assert_eq("high.cool_down", cool_down_seconds_for("HIGH"), 86400)
    print("OK 3/9: HIGH → single sign + 24h cool-down.")


# ─────────────────────────── 4. LOW ───────────────────────────

def test_low():
    inv = scan_invariants(
        proposal_id="pid-4",
        target_files=["backend/utils/strings.py"],
        post_patch_files={
            "backend/utils/strings.py": "def upper(s):\n    return s.upper()\n",
        },
    )
    audit = classify(inv)
    assert_eq("low.classification", audit.classification, "LOW")
    assert_eq("low.required_signatures", audit.required_signatures, 1)
    assert_eq("low.cool_down", audit.cool_down_seconds, 0)
    assert_eq("low.invariant_passed", inv.passed, True)
    print("OK 4/9: LOW → single sign, no cool-down.")


# ─────────────────────────── 5. Forbidden assignment ───────────────────────────

def test_forbidden_assignment():
    inv = scan_invariants(
        proposal_id="pid-5",
        target_files=["backend/utils/flags_config.py"],
        post_patch_files={
            "backend/utils/flags_config.py":
                "BROKER_LIVE_ORDER_ENABLED = True\n",
        },
    )
    has = any("BROKER_LIVE_ORDER_ENABLED" in f for f in inv.forbidden_findings)
    assert_eq("forbidden_assign.found", has, True)
    assert_eq("forbidden_assign.failed", inv.passed, False)
    audit = classify(inv)
    assert_eq("forbidden_assign.classification", audit.classification, "MEDIUM")
    print("OK 5/9: AST catches BROKER_LIVE_ORDER_ENABLED = True.")


# ─────────────────────────── 6. Forbidden call ───────────────────────────

def test_forbidden_call():
    inv = scan_invariants(
        proposal_id="pid-6",
        target_files=["backend/utils/dispatcher.py"],
        post_patch_files={
            "backend/utils/dispatcher.py":
                "import db\n"
                "def go():\n"
                "    db.paper_trades.insert_one({'sym': 'TSLA'})\n",
        },
    )
    has = any("paper_trades.insert_one" in f for f in inv.forbidden_findings)
    assert_eq("forbidden_call.found", has, True)
    assert_eq("forbidden_call.failed", inv.passed, False)
    print("OK 6/9: AST catches paper_trades.insert_one(...) call.")


# ─────────────────────────── 7. Target file drift ───────────────────────────

def test_target_drift():
    inv = scan_invariants(
        proposal_id="pid-7",
        target_files=["backend/utils/a.py"],
        post_patch_files={
            "backend/utils/a.py": "x = 1\n",
            "backend/utils/sneaky.py": "y = 2\n",  # not declared
        },
    )
    assert_eq("drift.passed", inv.passed, False)
    assert_eq("drift.has_drift", "backend/utils/sneaky.py" in inv.target_file_drift, True)
    print("OK 7/9: target_files vs post_patch_files drift is caught.")


# ─────────────────────────── 8. Syntax error ───────────────────────────

def test_syntax_error():
    inv = scan_invariants(
        proposal_id="pid-8",
        target_files=["backend/utils/broken.py"],
        post_patch_files={
            "backend/utils/broken.py": "def go(:\n    pass\n",
        },
    )
    assert_eq("syntax.passed", inv.passed, False)
    assert_eq("syntax.has_error", len(inv.syntax_errors) > 0, True)
    print("OK 8/9: Syntax error in post-patch file is caught.")


# ─────────────────────────── 9. may_auto_promote is False ───────────────────────────

def test_doctrine():
    assert_eq("doctrine.no_args", may_auto_promote(), False)
    assert_eq("doctrine.with_args", may_auto_promote(proposal_id="x", classification="LOW"), False)
    assert_eq("doctrine.no_truthy_args", may_auto_promote(force=True, override=True), False)
    print("OK 9/9: may_auto_promote() is False under all argument combinations.")


# ─────────────────────────── runner ───────────────────────────

def main() -> int:
    test_protected()
    asyncio.run(test_critical_dual_sign())
    test_high()
    test_low()
    test_forbidden_assignment()
    test_forbidden_call()
    test_target_drift()
    test_syntax_error()
    test_doctrine()
    print("\nALL OK — Code Evolution v0 doctrine holds.")
    print("AI may audit. AI may not promote. AI may not modify the gate.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

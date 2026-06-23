"""Operator quarantine of `execution_judge.ready` — 2026-06-23.

The scorecard showed the signal was selecting WORSE outcomes than
its inverse (`ready_loss_rate=1.00` vs `not_ready_loss_rate=0.37`).
A heuristic that inverts its own meaning cannot:
  * earn an auto-retire candidate (it'd retire the wrong seat holder)
  * block promotion (the field itself is the broken thing)

Until the heuristic is rebuilt, both pathways treat it as advisory.
These tests pin both rails so the demotion can't silently regress.
"""
from __future__ import annotations

import sys

sys.path.insert(0, "/app/backend")


# ── auto_retire: execution_judge.ready must NOT generate candidates ──


def test_auto_retire_expectations_list_does_not_include_execution_judge():
    """Structural pin: the `expectations` list inside
    `retirement_candidates` MUST NOT contain a tuple for
    `execution_judge`. Reading the source is the most stable test
    because the function itself is a FastAPI route (hits Mongo)
    that's awkward to unit-test directly. The structural guarantee
    is what actually matters — if `execution_judge` appears as an
    active expectation, the heuristic is back on the hard gate."""
    import inspect
    from shared.doctrine import auto_retire
    src = inspect.getsource(auto_retire.retirement_candidates)
    # Heuristic check: ensure no UN-COMMENTED tuple references
    # execution_judge in the expectations list. Comments mentioning
    # it for documentation are fine.
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        # The tuple shape we're forbidding looks like:
        #   ("execution_judge", "ready", "not_ready", "branch_lower_loss")
        if '"execution_judge"' in stripped and "branch_" in stripped:
            raise AssertionError(
                f"execution_judge expectation re-introduced into the "
                f"hard gate! Line: {stripped!r}. Remove it or move "
                f"to a documented ADVISORY list per the 2026-06-23 "
                f"quarantine pin."
            )


def test_auto_retire_module_documents_the_quarantine():
    """The docstring on `retirement_candidates` MUST explicitly mark
    `execution_judge.ready` as quarantined. This ensures any future
    operator reading the code immediately sees the pin without
    having to dig through git history."""
    from shared.doctrine.auto_retire import retirement_candidates
    doc = retirement_candidates.__doc__ or ""
    assert "QUARANTINED" in doc, (
        "retirement_candidates docstring must announce the "
        "execution_judge quarantine. Otherwise future agents will "
        "re-add it on a 'cleanup' pass and silently regress."
    )
    assert "execution_judge" in doc


# ── scorecard: execution_judge.ready must surface as advisory ────


def test_scorecard_execution_judge_inversion_is_advisory_not_blocker():
    """When ready_loss_rate ≥ not_ready_loss_rate, the scorecard must
    surface the failure as an ADVISORY, never as a promotion blocker.
    This guarantees the heuristic can't gate promotions while the
    operator decides how to rebuild it."""
    from shared.doctrine.scorecard import _promotion_blockers_and_advisories

    quality_report = {
        "A_QUALITY": {"win_rate": 0.65, "samples": 50},
        "C_QUALITY": {"win_rate": 0.45, "samples": 30},
        "REJECT":    {"win_rate": 0.30, "samples": 20},
    }
    by_seat = {
        "governor":   {
            "block":    {"loss_rate": 0.50, "samples": 30},
            "modulate": {"loss_rate": 0.30, "samples": 30},
        },
        "adversary":  {
            "challenge_required": {"loss_rate": 0.55, "samples": 25},
            "quiet":              {"loss_rate": 0.30, "samples": 25},
        },
        "execution_judge": {
            # Inverted: ready WORSE than not_ready.
            "ready":     {"loss_rate": 1.00, "samples": 40},
            "not_ready": {"loss_rate": 0.37, "samples": 40},
        },
    }
    blockers, advisories = _promotion_blockers_and_advisories(
        quality_report, by_seat,
    )
    # No blocker should mention execution_judge (it's quarantined).
    judge_blockers = [b for b in blockers if "execution_judge" in b]
    assert judge_blockers == [], (
        f"execution_judge MUST NOT appear in promotion blockers "
        f"(quarantine pin). Got: {judge_blockers!r}"
    )
    # But the advisory MUST surface the inverted finding.
    judge_advisories = [a for a in advisories if "execution_judge" in a]
    assert len(judge_advisories) == 1, (
        f"execution_judge inversion MUST surface as exactly one "
        f"advisory. Got: {judge_advisories!r}"
    )
    assert "quarantined" in judge_advisories[0].lower(), (
        f"Advisory text must mention the quarantine so operators "
        f"reading the dashboard understand why it's not blocking. "
        f"Got: {judge_advisories[0]!r}"
    )


def test_scorecard_backwards_compat_shim_still_works():
    """The legacy `_promotion_blockers` shim must return a plain list
    so any pre-existing callers don't break."""
    from shared.doctrine.scorecard import _promotion_blockers
    by_seat = {
        "governor": {
            "block":    {"loss_rate": 0.50, "samples": 30},
            "modulate": {"loss_rate": 0.30, "samples": 30},
        },
        "adversary": {
            "challenge_required": {"loss_rate": 0.55, "samples": 25},
            "quiet":              {"loss_rate": 0.30, "samples": 25},
        },
        "execution_judge": {
            "ready":     {"loss_rate": 1.00, "samples": 40},
            "not_ready": {"loss_rate": 0.37, "samples": 40},
        },
    }
    result = _promotion_blockers(
        {"A_QUALITY": {"win_rate": 0.7, "samples": 50}},
        by_seat,
    )
    assert isinstance(result, list)
    # Must not contain the quarantined check.
    assert not any("execution_judge" in b for b in result)

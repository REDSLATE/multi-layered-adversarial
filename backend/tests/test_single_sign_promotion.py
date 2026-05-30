"""Single-sign promotion doctrine tripwires (2026-02-17).

History: Operator is a solo deployment. Dual-sign was security
theater — locked in `shared/promotion.py:13-19` as a doctrine note,
but the actual `required_signatures = 2 if target == 'primary' else 1`
line at the proposal-creation path was never removed, AND the frontend
still rendered a `DUAL-SIGN` badge + 2-step countersign UX. Existing
proposals stored `required_signatures: 2` and showed `0/2` forever.

These tripwires lock:
  1. `propose_from_latest_artifact` writes `required_signatures: 1`
     for ALL ladder tiers (including `primary`).
  2. `list_proposals` self-heals legacy `required_signatures > 1`
     rows on read (idempotent migration).
  3. Frontend `Promotion.jsx` does NOT render a `DUAL-SIGN` badge
     nor the 2-of-2 button labels.
  4. Source-scan: `required_signatures = 2` is not present anywhere
     in the live promotion path.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from shared import promotion as pm


pytestmark = [pytest.mark.tripwire]


# ─────────────────── Backend ───────────────────


def test_propose_writes_single_signature_for_all_tiers():
    """The proposal-creation path must hard-code `required_signatures = 1`
    regardless of target_authority. No conditional branch should
    re-introduce the dual-sign tier."""
    src = inspect.getsource(pm.propose_from_latest_artifact)
    assert "required_signatures = 1" in src, (
        "Single-sign doctrine: propose_from_latest_artifact must "
        "hard-set `required_signatures = 1`."
    )
    # No conditional that picks 2 for primary or any other tier.
    forbidden_patterns = [
        r"required_signatures\s*=\s*2",
        r"required_signatures\s*=\s*\d+\s+if\s+",
    ]
    for pat in forbidden_patterns:
        assert not re.search(pat, src), (
            f"propose_from_latest_artifact contains dual-sign branch "
            f"matching {pat!r} — solo operator doctrine forbids this."
        )


def test_list_proposals_self_heals_legacy_dual_sign_rows():
    """Reading the proposals list must idempotently migrate any
    `required_signatures > 1` pending row to 1. Otherwise the
    operator dashboard shows `0/2` on existing in-flight proposals
    even after the code change."""
    src = inspect.getsource(pm.list_proposals)
    # Must touch the proposals collection with an update_many.
    assert "update_many" in src, (
        "list_proposals must include a self-healing migration via "
        "update_many — otherwise legacy `0/2` rows stay broken."
    )
    # Must filter on `required_signatures > 1` (or `$gt: 1`) AND target
    # pending/awaiting states only (don't rewrite history).
    assert "$gt" in src and "1" in src
    assert "pending" in src and "awaiting_second_sign" in src
    # And actually set required_signatures back to 1.
    assert '"required_signatures": 1' in src


def test_no_lingering_required_signatures_eq_2_in_module():
    """Source-wide scan: nowhere in the live promotion path should
    we set required_signatures to 2."""
    src = inspect.getsource(pm)
    # Exclude comments — operator-readable doctrine notes are fine.
    code_lines = [
        ln for ln in src.splitlines()
        if not ln.strip().startswith("#")
    ]
    joined = "\n".join(code_lines)
    assert not re.search(r"required_signatures\s*=\s*2", joined), (
        "Live code path still writes required_signatures = 2 "
        "somewhere — strip it."
    )


def test_countersign_path_elevates_on_first_signer():
    """The countersign route already elevates immediately on the
    first signer (single-sign across all tiers). Lock this so it
    never regresses back to a multi-step state machine."""
    src = inspect.getsource(pm.countersign)
    # Must call SHARED_AUTHORITY_STATE.update_one with the target
    # authority on the first signer — no `if len(signers) >= required`
    # branch that delays the elevation.
    assert "SHARED_AUTHORITY_STATE" in src
    assert "update_one" in src
    # Forbid the multi-step pattern.
    forbidden_patterns = [
        r"if\s+len\(signers\)\s*<\s*required",
        r"awaiting_second_sign",  # Must not be SET; legacy rows are
                                  # tolerated on read but never created.
    ]
    # `awaiting_second_sign` may appear in tolerance-of-legacy-rows
    # logic; check we're not WRITING it.
    assert 'status": "awaiting_second_sign"' not in src, (
        "countersign must not set status='awaiting_second_sign'. "
        "Single-sign elevates on first countersign."
    )


# ─────────────────── Frontend ───────────────────


PROMOTION_JSX = Path("/app/frontend/src/pages/Promotion.jsx")


def test_frontend_promotion_no_dual_sign_badge():
    """The Promotion page must NOT render a `DUAL-SIGN` badge.
    Solo operator doctrine — there is no second signer."""
    src = PROMOTION_JSX.read_text(encoding="utf-8")
    assert "DUAL-SIGN" not in src, (
        "Promotion.jsx still references DUAL-SIGN — solo operator "
        "deployment must not show this label."
    )
    # And no `1st of 2` / `Co-sign` / `2nd sign` button labels.
    for forbidden in ("1st of 2", "Co-sign & elevate", "2nd sign", "second operator", "two required signatures"):
        assert forbidden not in src, (
            f"Promotion.jsx contains stale dual-sign UX phrase "
            f"{forbidden!r}. Strip it."
        )


def test_frontend_promotion_required_is_hardcoded_one():
    """The frontend must coerce `required = 1` rather than trusting
    `p.required_signatures` from the payload. This defends against a
    stale cached `0/2` row if a client somehow gets an unmigrated
    payload from a hostile cache layer."""
    src = PROMOTION_JSX.read_text(encoding="utf-8")
    # We expect `const required = 1;` in the proposal row mapper.
    assert "const required = 1;" in src, (
        "Promotion.jsx must hard-code `const required = 1;` — never "
        "trust the payload's required_signatures field."
    )

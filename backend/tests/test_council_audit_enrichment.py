"""Regression: council verdicts MUST carry structured audit fields.

Operator directive (2026-02-20):
    "Add audit reason: SOFT_DISSENT_BELOW_FLOOR / raw_conf / eff_conf /
     floor / governor_mult"

These fields let the post-mortem and operator UI explain the kill
math deterministically without parsing human-readable reason strings.
If they go missing, the operator loses the ability to answer "why
was my 0.518 SELL killed?" without grepping logs.
"""
from __future__ import annotations

import pytest

from shared.council import _governance_verdict


REQUIRED_AUDIT_FIELDS = {"raw_conf", "eff_conf", "floor", "governor_mult"}


@pytest.mark.asyncio
async def test_soft_dissent_below_floor_carries_audit_fields():
    """Equity intent conf 0.40 + governor in dissent (RISK_DOWN) →
    eff_conf 0.40 × 0.90 = 0.36 ≥ floor 0.35 ⇒ SOFT_DISSENT_DOWNWEIGHTED.
    Verify all 4 structured audit fields are present and numerically
    consistent.
    """
    from shared.equity.council_policy import EQUITY_POLICY
    intent = {"confidence": 0.40, "lane": "equity"}
    gov_norm = {"stance": "RISK_DOWN", "confidence": 0.55, "veto": False}
    verdict = _governance_verdict(
        intent, gov_norm, governor_alive=True,
        governor_holder="chevelle", policy=EQUITY_POLICY,
    )
    missing = REQUIRED_AUDIT_FIELDS - verdict.keys()
    assert not missing, f"missing audit fields: {missing}"
    assert verdict["raw_conf"] == 0.40
    assert verdict["governor_mult"] == EQUITY_POLICY["GOVERNOR_DISSENT_CONF_MULT"]
    assert verdict["floor"] == EQUITY_POLICY["MIN_EXECUTOR_CONF_FLOOR"]
    assert abs(verdict["eff_conf"] - 0.40 * EQUITY_POLICY["GOVERNOR_DISSENT_CONF_MULT"]) < 1e-6


@pytest.mark.asyncio
async def test_below_floor_verdict_still_audited():
    """Equity intent conf 0.30 + governor dissent → eff_conf
    0.30 × 0.90 = 0.27 < floor 0.35. The kill path must still carry
    every audit field so the operator can see exactly how far below
    the floor the trade landed."""
    from shared.equity.council_policy import EQUITY_POLICY
    intent = {"confidence": 0.30, "lane": "equity"}
    gov_norm = {"stance": "DISSENT", "confidence": 0.60, "veto": False}
    verdict = _governance_verdict(
        intent, gov_norm, governor_alive=True,
        governor_holder="chevelle", policy=EQUITY_POLICY,
    )
    missing = REQUIRED_AUDIT_FIELDS - verdict.keys()
    assert not missing, f"missing audit fields: {missing}"
    assert verdict["raw_conf"] == 0.30
    assert verdict["eff_conf"] < verdict["floor"]


@pytest.mark.asyncio
async def test_post_2026_02_20_relaxation_eff_floor():
    """The point of this whole exercise: confirm the post-fix
    effective floor lets 0.52-0.60 conf intents survive governor
    dissent. With MIN_EXECUTOR_CONF_FLOOR=0.35 + DISSENT_CONF_MULT=0.90:
        eff_floor = 0.35 / 0.90 ≈ 0.389
    so a 0.40 conf intent with governor dissent SHOULD survive.
    Before the fix (0.50 / 0.82 = 0.610) it would have been killed.
    """
    from shared.equity.council_policy import EQUITY_POLICY
    floor = EQUITY_POLICY["MIN_EXECUTOR_CONF_FLOOR"]
    dissent_mult = EQUITY_POLICY["GOVERNOR_DISSENT_CONF_MULT"]
    eff_floor = floor / dissent_mult
    assert eff_floor < 0.45, (
        f"post-2026-02-20 relaxation requires eff_floor < 0.45 "
        f"(got {eff_floor:.3f}). Did someone re-tighten the council?"
    )
    # And specifically validate a 0.55 conf intent survives.
    intent = {"confidence": 0.55, "lane": "equity"}
    gov_norm = {"stance": "RISK_DOWN", "confidence": 0.60, "veto": False}
    verdict = _governance_verdict(
        intent, gov_norm, governor_alive=True,
        governor_holder="chevelle", policy=EQUITY_POLICY,
    )
    # Non-fatal silence/dissent gets downgraded to RISK_DOWN_ONLY
    # (allowed=True with damped risk_multiplier) per the FATAL/SILENCE
    # taxonomy — that's a survival under the new floor.
    assert verdict["allowed"] is True
    assert verdict["risk_multiplier"] > 0

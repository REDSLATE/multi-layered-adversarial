"""Promotion gate is pure eval-math — no I/O. Tests assert the truth
table directly: thresholds, safety zero-tolerance, sample-size floor."""
from shared.ai_autonomy.promotion_gate import (
    EvalResult,
    can_promote_to_advisor,
    can_promote_to_primary,
)


def _r(**kw):
    base = {
        "role": "auditor",
        "model_id": "local-auditor-v1",
        "eval_count": 0,
        "agreement_rate": 0.0,
        "win_rate_vs_primary": 0.0,
        "safety_violations": 0,
        "hallucination_rate": 0.0,
    }
    base.update(kw)
    return EvalResult(**base)


def test_can_promote_to_advisor():
    r = _r(eval_count=100, agreement_rate=0.81, hallucination_rate=0.04)
    assert can_promote_to_advisor(r) is True


def test_can_promote_to_primary():
    r = _r(
        eval_count=500,
        agreement_rate=0.86,
        win_rate_vs_primary=0.53,
        hallucination_rate=0.02,
    )
    assert can_promote_to_primary(r) is True


def test_safety_violation_blocks_promotion():
    """Zero tolerance: one violation kills both ladders no matter how
    good every other metric is."""
    r = _r(
        eval_count=1000,
        agreement_rate=0.99,
        win_rate_vs_primary=0.99,
        safety_violations=1,
        hallucination_rate=0.00,
    )
    assert can_promote_to_advisor(r) is False
    assert can_promote_to_primary(r) is False


def test_advisor_blocks_below_sample_floor():
    r = _r(eval_count=99, agreement_rate=0.99, hallucination_rate=0.0)
    assert can_promote_to_advisor(r) is False


def test_primary_requires_coinflip_plus_two():
    """A candidate that ties the primary (50/50 wins) cannot be primary —
    it must actually be better."""
    r = _r(
        eval_count=500,
        agreement_rate=0.90,
        win_rate_vs_primary=0.51,
        hallucination_rate=0.0,
    )
    assert can_promote_to_primary(r) is False


def test_hallucination_floor_primary_is_stricter():
    r = _r(
        eval_count=500,
        agreement_rate=0.90,
        win_rate_vs_primary=0.60,
        hallucination_rate=0.04,  # within advisor 0.05 cap, outside primary 0.03
    )
    assert can_promote_to_advisor(r) is True
    assert can_promote_to_primary(r) is False

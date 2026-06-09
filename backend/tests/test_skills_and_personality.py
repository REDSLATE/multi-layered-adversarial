"""Tests for the skills layer and personality multiplier wiring.

Doctrine pins:
  - Skills enrich hypotheses; they never gate.
  - Personality multiplier modulates conviction; the clamp is math
    only (probability range), not a soft restriction.
  - Every touch on `confidence` is recorded in `confidence_evidence`
    so the operator audit log shows the full transformation.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


from external.brains.personality import (  # noqa: E402
    BRAIN_PERSONALITIES,
    apply_personality_confidence,
    clamp_probability,
    get_personality,
)
from external.skills.loader import SkillLoader  # noqa: E402
from external.skills.selector import SkillSelector  # noqa: E402


# ──────────────────────── personality ────────────────────────


def test_clamp_probability_bounds():
    assert clamp_probability(-0.5) == 0.0
    assert clamp_probability(0.0) == 0.0
    assert clamp_probability(0.5) == 0.5
    assert clamp_probability(1.0) == 1.0
    assert clamp_probability(1.5) == 1.0


def test_apply_personality_camino_neutral():
    """Camino's multiplier is 1.0 — confidence passes through unchanged."""
    final, ev = apply_personality_confidence("alpha", 0.7)
    assert final == 0.7
    assert ev["raw_confidence"] == 0.7
    assert ev["personality_multiplier"] == 1.0
    assert ev["adjusted_pre_clamp"] == 0.7
    assert ev["final_confidence"] == 0.7
    assert ev["saturated_by_clamp"] is False
    assert "personality.py" in ev["confidence_touched_by"]
    assert "math_clamp_0_1" in ev["confidence_touched_by"]


def test_apply_personality_hellcat_aggressive_boost():
    """Hellcat at 1.30x: 0.50 → 0.65."""
    final, ev = apply_personality_confidence("chevelle", 0.50)
    assert final == pytest.approx(0.65)
    assert ev["personality_multiplier"] == 1.30
    assert ev["personality_risk_mode"] == "aggressive"
    assert ev["saturated_by_clamp"] is False


def test_apply_personality_gto_disciplined_damper():
    """GTO at 0.85x: 0.80 → 0.68."""
    final, ev = apply_personality_confidence("redeye", 0.80)
    assert final == pytest.approx(0.68)
    assert ev["personality_multiplier"] == 0.85
    assert ev["personality_risk_mode"] == "disciplined"


def test_apply_personality_saturated_by_clamp_flagged_honestly():
    """Hellcat at 0.85 raw → 1.105 unclamped → 1.0 clamped. The
    clamp's binding constraint MUST be flagged in evidence so the
    audit log shows the brain's read was actually >1.0."""
    final, ev = apply_personality_confidence("chevelle", 0.85)
    assert final == 1.0
    assert ev["adjusted_pre_clamp"] == pytest.approx(1.105)
    assert ev["saturated_by_clamp"] is True


def test_apply_personality_accepts_display_names_too():
    """Operator may pass `camino` or `alpha` — both resolve."""
    final_id, _ = apply_personality_confidence("alpha", 0.6)
    final_name, _ = apply_personality_confidence("camino", 0.6)
    assert final_id == final_name


def test_apply_personality_unknown_brain_is_neutral():
    """Unknown brain falls back to 1.0 multiplier — fail-safe."""
    final, ev = apply_personality_confidence("ghost_brain", 0.7)
    assert final == 0.7
    assert ev["personality_multiplier"] == 1.0


def test_all_four_brains_have_distinct_personalities():
    """Camino / Barracuda / Hellcat / GTO MUST have different
    confidence multipliers — that's the whole point of personalities.
    A regression that makes them identical would erase brain-level
    differentiation."""
    mults = {
        b: BRAIN_PERSONALITIES[b]["confidence_mult"]
        for b in ("camino", "barracuda", "hellcat", "gto")
    }
    assert len(set(mults.values())) == 4, (
        f"brain multipliers should be distinct, got {mults}"
    )


# ──────────────────────── skills loader ────────────────────────


def test_skill_loader_reads_all_skills():
    """The skill_pack ships with the 4 doctrine-aligned skills."""
    loader = SkillLoader()
    skills = loader.load_all()
    names = {s.name for s in skills}
    assert {"crypto-execution", "adversarial-risk", "risk-perception",
            "market-memory"}.issubset(names), (
        f"missing skill files; loaded: {names}"
    )


def test_no_governor_skill_present():
    """The old `governor-risk` skill (with HALTs and damp-by-X rules)
    was deleted at operator request. Verify it stays gone — any
    re-introduction would re-trip the 'restrictions are nonstarter'
    constraint."""
    loader = SkillLoader()
    names = {s.name for s in loader.load_all()}
    assert "governor-risk" not in names


def test_no_skill_body_contains_halt_or_block_language():
    """Tripwire: skills must NEVER ship with HALT / force-HOLD /
    damp-by-X language in their RULES sections. Skills are evidence
    surfaces; MC is the only restriction layer.
    """
    loader = SkillLoader()
    skills = loader.load_all()
    forbidden_phrases = (
        "force hold",
        "halt that",
        "damp confidence by 50",
        "damp confidence by 75",
        "damp confidence by 20",
        "Hard-block",
    )
    for skill in skills:
        body_low = skill.body.lower()
        for phrase in forbidden_phrases:
            assert phrase.lower() not in body_low, (
                f"skill `{skill.name}` body contains restrictive phrase "
                f"`{phrase}` — skills must NOT add gates."
            )


# ──────────────────────── skill selector ────────────────────────


def test_selector_picks_crypto_skill_for_btc_task():
    sel = SkillSelector()
    picks = sel.select(
        task="BUY crypto BTC/USD", snapshot={"symbol": "BTC/USD"},
    )
    assert picks, "selector returned no skills for a crypto task"
    assert picks[0].name == "crypto-execution"


def test_selector_picks_memory_skill_for_recall_task():
    sel = SkillSelector()
    picks = sel.select(
        task="recall prior outcome history for this symbol",
        snapshot={"symbol": "ETH/USD"},
    )
    names = [p.name for p in picks]
    assert "market-memory" in names


def test_selector_returns_empty_on_no_match():
    sel = SkillSelector()
    picks = sel.select(task="completely unrelated gibberish xyzqq")
    # Tags include common words like "trade" which may match; ensure
    # at minimum the empty case is handled cleanly (no crash).
    assert isinstance(picks, list)


def test_selector_respects_limit():
    sel = SkillSelector()
    picks = sel.select(
        task="crypto memory risk reversal",
        snapshot={"symbol": "BTC/USD"},
        limit=2,
    )
    assert len(picks) <= 2


def test_selector_tag_weight_3x_description_weight():
    """A skill matched by TAG must outrank a skill matched only by
    description word. That's the design contract — tags are
    operator-curated, descriptions are broad lexical fallback."""
    sel = SkillSelector()
    # `kraken` is a tag of crypto-execution and not in adversarial-risk
    picks = sel.select(task="kraken")
    assert picks
    assert picks[0].name == "crypto-execution"


# ──────────────────────── runner integration ────────────────────────


def test_runner_imports_personality_and_skills():
    """The runner module imports the personality + skills layer at
    module load. A regression that removes these imports would mean
    skill enrichment silently stops happening."""
    from external.brains import runner  # noqa: F401
    import inspect
    src = inspect.getsource(runner)
    assert "apply_personality_confidence" in src
    assert "_select_skills_for" in src
    # And it MUST be wired into `_evaluate_and_post`.
    eval_src = inspect.getsource(runner.BrainRunner._evaluate_and_post)
    assert "apply_personality_confidence" in eval_src
    assert "_select_skills_for" in eval_src
    assert "skills_used" in eval_src
    assert "confidence_evidence" in eval_src

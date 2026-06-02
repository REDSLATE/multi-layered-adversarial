"""shared.rise_ai surface — pure data + pure function tests."""
from shared.rise_ai import (
    GENERAL_PROFILE,
    RISE_AI_ROLE_PROFILES,
    compose_role_aligned_prompt,
    model_for_role,
    profile_for,
)


def test_all_four_brains_have_profiles():
    for brain in ("alpha", "camaro", "chevelle", "redeye"):
        p = RISE_AI_ROLE_PROFILES[brain]
        assert p["model_id"].startswith(f"rise-ai-{brain}-qwen3-8b")
        assert p["purpose"], f"{brain} missing purpose"
        assert isinstance(p["focus"], list) and p["focus"]
        assert isinstance(p["forbidden"], list) and p["forbidden"]


def test_profile_for_unknown_falls_back():
    assert profile_for("nonexistent") is GENERAL_PROFILE
    assert profile_for("") is GENERAL_PROFILE
    assert profile_for(None) is GENERAL_PROFILE  # type: ignore[arg-type]


def test_profile_for_is_case_insensitive():
    assert profile_for("ALPHA") is RISE_AI_ROLE_PROFILES["alpha"]
    assert profile_for("Camaro") is RISE_AI_ROLE_PROFILES["camaro"]


def test_model_for_role_returns_canonical_id():
    assert model_for_role("alpha") == "rise-ai-alpha-qwen3-8b-v1"
    assert model_for_role("redeye") == "rise-ai-redeye-qwen3-8b-v1"
    assert model_for_role("unknown") == GENERAL_PROFILE["model_id"]


def test_compose_role_aligned_prompt_pins_authority():
    """The composed prompt MUST embed 'authority: REASONING_ONLY' and
    the role-specific focus + forbidden lists. This is the brain-side
    safety frame; missing this pin is a doctrine break."""
    out = compose_role_aligned_prompt(role="redeye", prompt="What now?")
    assert "authority: REASONING_ONLY" in out
    assert "REASONING_ONLY" in out
    assert "RISE AI ROLE: redeye" in out
    # role-specific focus must be present
    assert "collapse" in out
    # forbidden list must be present
    assert "overfit fear" in out


def test_compose_role_aligned_prompt_unknown_role_uses_general():
    out = compose_role_aligned_prompt(role="ghost", prompt="hi")
    assert "general reasoning" in out
    assert "authority: REASONING_ONLY" in out


def test_compose_includes_memory_and_contexts_when_provided():
    out = compose_role_aligned_prompt(
        role="alpha",
        prompt="trade?",
        memory_context="last 5 fills: 0.01% slip avg",
        market_context={"price": 100.0, "spread_bps": 5},
        doctrine_context={"seat_holder": "alpha", "lane_enabled": True},
    )
    assert "last 5 fills" in out
    assert "100.0" in out
    assert "lane_enabled" in out


def test_compose_handles_missing_memory_gracefully():
    out = compose_role_aligned_prompt(role="alpha", prompt="x")
    assert "No verified memory." in out

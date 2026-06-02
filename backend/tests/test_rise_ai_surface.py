"""shared.rise_ai surface — pure data + pure function tests, seat-keyed."""
from shared.rise_ai import (
    GENERAL_PROFILE,
    RISE_AI_ROLE_PROFILES,
    compose_role_aligned_prompt,
    model_for_role,
    profile_for,
)


CANONICAL_SEATS = (
    "strategist", "auditor", "governor", "executor",
    "crypto_strategist", "crypto_auditor", "crypto_governor", "crypto",
)


def test_all_eight_seats_have_profiles():
    for seat in CANONICAL_SEATS:
        p = RISE_AI_ROLE_PROFILES[seat]
        # crypto executor seat is keyed `crypto`, model_id contains `crypto-executor`
        slug = "crypto-executor" if seat == "crypto" else seat.replace("_", "-")
        assert slug in p["model_id"], f"{seat} model_id={p['model_id']!r} missing slug {slug!r}"
        assert p["purpose"], f"{seat} missing purpose"
        assert isinstance(p["focus"], list) and p["focus"], f"{seat} empty focus"
        assert isinstance(p["forbidden"], list) and p["forbidden"], f"{seat} empty forbidden"


def test_legacy_aliases_resolve_to_canonical():
    assert profile_for("decider") is RISE_AI_ROLE_PROFILES["strategist"]
    assert profile_for("opponent") is RISE_AI_ROLE_PROFILES["auditor"]
    assert profile_for("advisor") is RISE_AI_ROLE_PROFILES["auditor"]
    assert profile_for("crypto_decider") is RISE_AI_ROLE_PROFILES["crypto_strategist"]
    assert profile_for("crypto_opponent") is RISE_AI_ROLE_PROFILES["crypto_auditor"]
    assert profile_for("crypto_executor") is RISE_AI_ROLE_PROFILES["crypto"]


def test_profile_for_unknown_falls_back():
    assert profile_for("nonexistent") is GENERAL_PROFILE
    assert profile_for("") is GENERAL_PROFILE
    assert profile_for(None) is GENERAL_PROFILE  # type: ignore[arg-type]


def test_profile_for_is_case_insensitive():
    assert profile_for("STRATEGIST") is RISE_AI_ROLE_PROFILES["strategist"]
    assert profile_for("Auditor") is RISE_AI_ROLE_PROFILES["auditor"]


def test_model_for_role_returns_canonical_id():
    assert model_for_role("strategist") == "rise-ai-strategist-qwen3-8b-v1"
    assert model_for_role("auditor") == "rise-ai-auditor-qwen3-8b-v1"
    assert model_for_role("crypto") == "rise-ai-crypto-executor-qwen3-8b-v1"
    assert model_for_role("unknown") == GENERAL_PROFILE["model_id"]


def test_compose_role_aligned_prompt_pins_authority():
    out = compose_role_aligned_prompt(role="auditor", prompt="What now?")
    assert "authority: REASONING_ONLY" in out
    assert "RISE AI ROLE: auditor" in out
    # auditor focus includes "trap", "post-trade attribution"
    assert "trap" in out
    assert "post-trade attribution" in out
    # auditor forbidden includes "overfit fear"
    assert "overfit fear" in out


def test_compose_role_aligned_prompt_unknown_role_uses_general():
    out = compose_role_aligned_prompt(role="ghost", prompt="hi")
    assert "general reasoning" in out
    assert "authority: REASONING_ONLY" in out


def test_compose_includes_memory_and_contexts_when_provided():
    out = compose_role_aligned_prompt(
        role="executor",
        prompt="trade?",
        memory_context="last 5 fills: 0.01% slip avg",
        market_context={"price": 100.0, "spread_bps": 5},
        doctrine_context={"seat_holder": "alpha", "lane_enabled": True},
    )
    assert "last 5 fills" in out
    assert "100.0" in out
    assert "lane_enabled" in out


def test_compose_handles_missing_memory_gracefully():
    out = compose_role_aligned_prompt(role="executor", prompt="x")
    assert "No verified memory." in out


def test_seat_doctrine_preserves_lane_isolation():
    """The crypto seats must reference crypto-flavored focus
    (funding, exchange, etc.), not equity-flavored. This catches a
    copy-paste error during the seat refactor."""
    crypto_focus_words = ("funding rate", "exchange", "Kraken",
                          "stablecoin", "perp", "wick")
    crypto_seats = ("crypto_strategist", "crypto_auditor",
                    "crypto_governor", "crypto")
    for seat in crypto_seats:
        p = RISE_AI_ROLE_PROFILES[seat]
        joined = " ".join(p["focus"]).lower()
        assert any(w.lower() in joined for w in crypto_focus_words), (
            f"crypto seat {seat} has no crypto-flavored focus: {p['focus']}"
        )

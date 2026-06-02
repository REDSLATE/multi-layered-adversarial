"""Auto-grader: pure-function tests (parser + filters). The grade_one
/ grade_batch I/O path is exercised end-to-end by an integration smoke
in the operator workflow, not unit-tested here, to avoid mocking the
LLM kernel."""
from shared.rise_ai.auto_grader import (
    AUTO_GRADER_ROLE,
    TRAINABLE_ROLES,
    compose_grading_prompt,
    parse_grade,
)


def test_parse_grade_happy_path():
    """Rubric LLM returns the canonical two-line format."""
    out = parse_grade("grade: 1\nreason: stays in role and gives structured output")
    assert out == {"grade": 1, "reason": "stays in role and gives structured output"}


def test_parse_grade_zero():
    out = parse_grade("grade: 0\nreason: refused to engage")
    assert out == {"grade": 0, "reason": "refused to engage"}


def test_parse_grade_case_insensitive_and_lenient_spacing():
    out = parse_grade("Grade:  1   \nReason:   ok")
    assert out is not None
    assert out["grade"] == 1
    assert out["reason"] == "ok"


def test_parse_grade_with_extra_preamble():
    """Real LLMs add filler. Parser must still find grade lines."""
    out = parse_grade(
        "Sure, here is my evaluation.\n\n"
        "grade: 1\n"
        "reason: the response engages directly with the prompt"
    )
    assert out is not None
    assert out["grade"] == 1


def test_parse_grade_unparseable_returns_none():
    """Defensive: a missing or malformed grade line must NOT write a
    silent default. The row stays ungraded for retry."""
    assert parse_grade("") is None
    assert parse_grade("I think this is good") is None
    assert parse_grade("grade: maybe\nreason: unclear") is None
    assert parse_grade("grade: 2\nreason: out of range") is None  # only 0 or 1


def test_parse_grade_defaults_reason_when_missing():
    """Some LLMs return only the grade line. Parser yields a placeholder
    reason so the persisted row has a non-empty `grade_reason`."""
    out = parse_grade("grade: 1")
    assert out is not None
    assert out["grade"] == 1
    assert out["reason"] == "no reason given"


def test_compose_grading_prompt_embeds_role_prompt_response():
    out = compose_grading_prompt(
        role="auditor",
        prompt="What is the bear case for NVDA?",
        response="The bear case is...",
    )
    assert "ROLE BEING GRADED: auditor" in out
    assert "What is the bear case for NVDA?" in out
    assert "The bear case is..." in out
    assert "grade: 0 or 1" in out


def test_auto_grader_role_excluded_from_trainable():
    """The grader's own ledger rows must NEVER be a grading target —
    no infinite loop and the grader is not itself training data."""
    assert AUTO_GRADER_ROLE not in TRAINABLE_ROLES


def test_trainable_roles_match_canonical_seats():
    """All 8 canonical seats must be in the trainable set."""
    for seat in (
        "strategist", "auditor", "governor", "executor",
        "crypto_strategist", "crypto_auditor", "crypto_governor", "crypto",
    ):
        assert seat in TRAINABLE_ROLES

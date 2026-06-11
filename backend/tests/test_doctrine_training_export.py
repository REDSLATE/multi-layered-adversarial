"""Tests for doctrine training export + eval suite."""
import json

import pytest
from fastapi.testclient import TestClient

from routes.doctrine_eval import router as eval_router
from routes.doctrine_training_export import (
    ALL_CARDS,
    EXAMPLE_TYPES,
    SYSTEM_PROMPT,
    make_code_pair,
    make_comparison_pair,
    make_fields_pair,
    make_qa_pair,
    make_rule_pair,
    router as train_router,
)
from fastapi import FastAPI

app = FastAPI()
app.include_router(train_router)
app.include_router(eval_router)
client = TestClient(app)


# ── Pair builders ───────────────────────────────────────────────────

def test_qa_pair_pulls_from_card():
    sid = "gap_and_go"
    card = ALL_CARDS[sid]
    ex = make_qa_pair(sid, card)
    assert ex["type"] == "qa"
    assert ex["strategy"] == sid
    msgs = ex["messages"]
    assert msgs[0]["role"] == "system"
    assert SYSTEM_PROMPT in msgs[0]["content"]
    assert card["title"] in msgs[1]["content"]
    assert card["doctrine_version"] in msgs[2]["content"]


def test_rule_pair_uses_card_fields():
    sid = "micro_pullback"
    ex = make_rule_pair(sid, ALL_CARDS[sid], "entries")
    assert ex is not None
    assistant = ex["messages"][2]["content"]
    for line in ALL_CARDS[sid]["entries"]:
        assert line in assistant


def test_rule_pair_returns_none_when_empty():
    blank = {"title": "Blank", "entries": []}
    assert make_rule_pair("blank", blank, "entries") is None


def test_fields_pair_includes_snapshot_and_flags():
    sid = "large_cap_equity"
    ex = make_fields_pair(sid, ALL_CARDS[sid])
    assistant = ex["messages"][2]["content"]
    assert "gap_pct" in assistant
    assert "MARKET_WEAK_REDUCE_RISK" in assistant


def test_code_pair_includes_live_source():
    sid = "gap_and_go"
    ex = make_code_pair(sid, ALL_CARDS[sid])
    assert ex is not None
    assert "_build_gap_and_go_v1" in ex["messages"][2]["content"]
    # Anti-drift sanity: the function code itself must appear
    assert "premarket_high_crossed" in ex["messages"][2]["content"]


def test_comparison_pair_mentions_both_titles():
    a, b = "gap_and_go", "micro_pullback"
    ex = make_comparison_pair(a, ALL_CARDS[a], b, ALL_CARDS[b])
    assistant = ex["messages"][2]["content"]
    assert ALL_CARDS[a]["title"] in assistant
    assert ALL_CARDS[b]["title"] in assistant


# ── Endpoints ───────────────────────────────────────────────────────

def test_preview_returns_examples_for_all_strategies():
    r = client.get("/admin/doctrine-training/preview")
    assert r.status_code == 200
    j = r.json()
    assert j["count"] > 0
    sids = {e["strategy"] for e in j["examples"]}
    assert sids == set(ALL_CARDS.keys())


def test_preview_filters_by_strategy():
    r = client.get(
        "/admin/doctrine-training/preview",
        params={"strategies": ["gap_and_go"]},
    )
    j = r.json()
    assert {e["strategy"] for e in j["examples"]} == {"gap_and_go"}


def test_preview_rejects_unknown_strategy():
    r = client.get(
        "/admin/doctrine-training/preview",
        params={"strategies": ["does_not_exist"]},
    )
    assert r.status_code == 400


def test_preview_rejects_unknown_type():
    r = client.get(
        "/admin/doctrine-training/preview",
        params={"types": ["qa", "bogus"]},
    )
    assert r.status_code == 400


def test_jsonl_export_is_valid_jsonl():
    r = client.get("/admin/doctrine-training/jsonl")
    assert r.status_code == 200
    assert "application/x-ndjson" in r.headers["content-type"]
    lines = [ln for ln in r.text.strip().split("\n") if ln]
    assert len(lines) > 0
    for ln in lines:
        obj = json.loads(ln)
        assert "messages" in obj
        assert obj["messages"][0]["role"] == "system"
        assert obj["messages"][-1]["role"] == "assistant"


def test_system_prompt_endpoint():
    r = client.get("/admin/doctrine-training/system-prompt")
    assert r.status_code == 200
    assert r.json()["system_prompt"] == SYSTEM_PROMPT


# ── Eval suite ──────────────────────────────────────────────────────

def test_eval_questions_auto_generated_per_card():
    r = client.get("/admin/doctrine-eval/questions")
    j = r.json()
    assert j["count"] > 0
    strategies_with_q = {q["strategy"] for q in j["questions"]}
    assert "gap_and_go" in strategies_with_q
    # Every question carries a non-empty keyword set
    for q in j["questions"]:
        assert q["expected_keywords"], f"empty keywords for {q['id']}"


def test_eval_questions_filtered_by_strategy():
    r = client.get(
        "/admin/doctrine-eval/questions",
        params={"strategy_id": "gap_and_go"},
    )
    j = r.json()
    assert all(q["strategy"] == "gap_and_go" for q in j["questions"])


def test_eval_questions_unknown_strategy():
    r = client.get(
        "/admin/doctrine-eval/questions",
        params={"strategy_id": "nope"},
    )
    assert r.status_code == 404


def test_eval_score_perfect_match():
    # Pull a real question and use its expected_answer as the "response"
    qs = client.get("/admin/doctrine-eval/questions").json()["questions"]
    q = next(x for x in qs if x["strategy"] == "gap_and_go")
    r = client.post(
        "/admin/doctrine-eval/score",
        json={"eval_id": q["id"], "response": q["expected_answer"]},
    )
    j = r.json()
    assert j["score"] >= 0.5  # most keywords should land
    assert j["eval_id"] == q["id"]


def test_eval_score_zero_match():
    qs = client.get("/admin/doctrine-eval/questions").json()["questions"]
    q = qs[0]
    r = client.post(
        "/admin/doctrine-eval/score",
        json={"eval_id": q["id"], "response": "completely unrelated answer xyz"},
    )
    assert r.json()["score"] == 0.0


def test_eval_score_unknown_id():
    r = client.post(
        "/admin/doctrine-eval/score",
        json={"eval_id": "nope::nope", "response": "x"},
    )
    assert r.status_code == 404


def test_example_types_complete():
    assert set(EXAMPLE_TYPES) == {"qa", "rule", "fields", "code", "comparison"}

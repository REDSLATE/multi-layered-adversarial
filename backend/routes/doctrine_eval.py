"""Doctrine eval suite — keyword-overlap scoring of LLM responses
against the live `DOCTRINE_CARDS` registry.

Replaces the textbook `EVAL_QUESTIONS` list from the operator-uploaded
trainer reference. Eval questions are AUTO-GENERATED from the cards so
the eval can never go stale: when a card changes, the questions change.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from shared.doctrine.brain_sidecars import DOCTRINE_CARDS as GENERIC_CARDS
from shared.doctrine.large_cap_doctrine import DOCTRINE_CARDS as LCAP_CARDS
from shared.doctrine.strategy_doctrines import DOCTRINE_CARDS as STRATEGY_CARDS

router = APIRouter(prefix="/admin/doctrine-eval", tags=["doctrine-eval"])

ALL_CARDS: Dict[str, Dict[str, Any]] = {
    **STRATEGY_CARDS,
    **LCAP_CARDS,
    **GENERIC_CARDS,
}

_TOKEN_RE = re.compile(r"[a-z0-9_]+")
_STOPWORDS = {
    "the", "and", "or", "a", "an", "of", "to", "in", "on", "for",
    "is", "are", "be", "with", "as", "by", "at", "from", "this", "that",
    "should", "do", "i", "my", "you", "your", "it", "its", "if", "no",
    "not", "any", "all", "into", "within", "vs", "than", "then",
}


def _tokenize(text: str) -> List[str]:
    return [t for t in _TOKEN_RE.findall((text or "").lower()) if t not in _STOPWORDS and len(t) > 1]


def _top_keywords(items: List[str], limit: int = 8) -> List[str]:
    seen: List[str] = []
    for s in items or []:
        for tok in _tokenize(s):
            if tok not in seen:
                seen.append(tok)
            if len(seen) >= limit:
                return seen
    return seen


def _build_questions_for(sid: str, card: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    title = card["title"]

    if card.get("entries"):
        out.append({
            "id": f"{sid}::entries",
            "strategy": sid,
            "rule_key": "entries",
            "q": f"When should the {title} doctrine trigger an entry?",
            "expected_keywords": _top_keywords(card["entries"]),
            "expected_answer": "\n".join(f"- {x}" for x in card["entries"]),
        })
    if card.get("exits"):
        out.append({
            "id": f"{sid}::exits",
            "strategy": sid,
            "rule_key": "exits",
            "q": f"When should the {title} doctrine exit a position?",
            "expected_keywords": _top_keywords(card["exits"]),
            "expected_answer": "\n".join(f"- {x}" for x in card["exits"]),
        })
    if card.get("size_modifier_notes"):
        out.append({
            "id": f"{sid}::size",
            "strategy": sid,
            "rule_key": "size_modifier_notes",
            "q": f"How does the {title} doctrine modify position size?",
            "expected_keywords": _top_keywords(card["size_modifier_notes"]),
            "expected_answer": "\n".join(f"- {x}" for x in card["size_modifier_notes"]),
        })
    if card.get("snapshot_fields_read"):
        out.append({
            "id": f"{sid}::fields",
            "strategy": sid,
            "rule_key": "snapshot_fields_read",
            "q": f"Which snapshot fields does the {title} doctrine read?",
            "expected_keywords": [f.lower() for f in card["snapshot_fields_read"][:8]],
            "expected_answer": ", ".join(card["snapshot_fields_read"]),
        })
    return out


def _all_questions() -> List[Dict[str, Any]]:
    qs: List[Dict[str, Any]] = []
    for sid, card in ALL_CARDS.items():
        qs.extend(_build_questions_for(sid, card))
    return qs


# ── Endpoints ───────────────────────────────────────────────────────

@router.get("/questions")
async def questions(
    strategy_id: Optional[str] = Query(default=None),
) -> Dict[str, Any]:
    """Auto-generated eval questions, derived from card fields."""
    all_q = _all_questions()
    if strategy_id:
        if strategy_id not in ALL_CARDS:
            raise HTTPException(
                status_code=404,
                detail=f"Unknown strategy '{strategy_id}'. Available: {list(ALL_CARDS.keys())}",
            )
        all_q = [q for q in all_q if q["strategy"] == strategy_id]
    return {
        "count": len(all_q),
        "questions": all_q,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


class ScoreRequest(BaseModel):
    eval_id: str
    response: str


@router.post("/score")
async def score(req: ScoreRequest) -> Dict[str, Any]:
    """Keyword-overlap score of an LLM response against the expected
    keywords for a single eval question. Score ∈ [0, 1].
    """
    question = next((q for q in _all_questions() if q["id"] == req.eval_id), None)
    if not question:
        raise HTTPException(status_code=404, detail=f"Unknown eval_id '{req.eval_id}'")
    resp_lower = (req.response or "").lower()
    expected = question["expected_keywords"]
    hits = [kw for kw in expected if kw.lower() in resp_lower]
    score_val = (len(hits) / len(expected)) if expected else 0.0
    return {
        "eval_id": req.eval_id,
        "strategy": question["strategy"],
        "rule_key": question["rule_key"],
        "score": round(score_val, 4),
        "matched_keywords": hits,
        "missed_keywords": [kw for kw in expected if kw not in hits],
        "expected_answer": question["expected_answer"],
    }

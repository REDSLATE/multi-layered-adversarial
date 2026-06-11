"""Doctrine-driven LLM fine-tuning corpus export.

Builds a JSONL corpus from the **same** `DOCTRINE_CARDS` registry that
powers the Doctrine Reference page. Same anti-drift guarantee: every
training example pulls from the live, CI-verified card. Nothing here
introduces new "knowledge" — it just re-frames existing card fields
into question/answer pairs an LLM can fine-tune on.

Adapted from the operator-uploaded `Trading Stack Trainer` reference.
The textbook `KNOWLEDGE_BASE` from that app is intentionally discarded
— it would re-introduce drift. Our cards are the only source of truth.
"""
from __future__ import annotations

import inspect
import io
import json
import random
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

import shared.doctrine.brain_sidecars as generic_mod
import shared.doctrine.large_cap_doctrine as lcap_mod
import shared.doctrine.strategy_doctrines as gng_mod
from shared.doctrine.brain_sidecars import (
    DOCTRINE_CARDS as GENERIC_CARDS,
    _DOCTRINE_FN_MAP as GENERIC_FN_MAP,
)
from shared.doctrine.large_cap_doctrine import (
    DOCTRINE_CARDS as LCAP_CARDS,
    _DOCTRINE_FN_MAP as LCAP_FN_MAP,
)
from shared.doctrine.strategy_doctrines import (
    DOCTRINE_CARDS as GNG_CARDS,
    _DOCTRINE_FN_MAP as GNG_FN_MAP,
)

router = APIRouter(prefix="/admin/doctrine-training", tags=["doctrine-training"])


ALL_CARDS: Dict[str, Dict[str, Any]] = {**GNG_CARDS, **LCAP_CARDS, **GENERIC_CARDS}
ALL_FN_MAP: Dict[str, str] = {**GNG_FN_MAP, **LCAP_FN_MAP, **GENERIC_FN_MAP}
EXAMPLE_TYPES = ("qa", "rule", "fields", "code", "comparison")


SYSTEM_PROMPT = (
    "You are a doctrine-aware trading-strategy assistant for the RISEDUAL "
    "Mission Control stack. Answer strictly from the live doctrine cards. "
    "Each card is the only source of truth — never invent fields, flags, "
    "or rules that the doctrine code does not actually read."
)


def _resolve_fn(fn_name: str):
    for mod in (gng_mod, lcap_mod, generic_mod):
        if hasattr(mod, fn_name):
            return getattr(mod, fn_name)
    return None


def _msg(user: str, assistant: str) -> Dict[str, Any]:
    return {
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user},
            {"role": "assistant", "content": assistant},
        ]
    }


def _bullet(items: List[str]) -> str:
    return "\n".join(f"- {x}" for x in items)


# ── Pair builders ───────────────────────────────────────────────────

def make_qa_pair(sid: str, card: Dict[str, Any]) -> Dict[str, Any]:
    user = f"What is the {card['title']} doctrine?"
    assistant = (
        f"{card['title']} ({card['doctrine_version']}) — {card['tagline']} "
        f"Category: {card['category']}; lane: {card['lane']}. "
        f"Source: {card['source_attribution']}."
    )
    return {"type": "qa", "strategy": sid, **_msg(user, assistant)}


def make_rule_pair(sid: str, card: Dict[str, Any], rule_key: str) -> Optional[Dict[str, Any]]:
    prompts = {
        "ideal_conditions": f"What conditions favor a {card['title']} trade?",
        "entries": f"When should I enter a {card['title']} trade?",
        "exits": f"When should I exit a {card['title']} position?",
        "size_modifier_notes": f"How do I size a {card['title']} position?",
    }
    items = card.get(rule_key) or []
    if not items:
        return None
    return {
        "type": "rule",
        "strategy": sid,
        "rule": rule_key,
        **_msg(prompts[rule_key], _bullet(items)),
    }


def make_fields_pair(sid: str, card: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    fields = card.get("snapshot_fields_read") or []
    flags = card.get("risk_flags_read") or []
    if not fields and not flags:
        return None
    user = (
        f"Which snapshot fields and risk flags does the {card['title']} "
        f"doctrine actually read?"
    )
    parts: List[str] = []
    if fields:
        parts.append("Snapshot fields:\n" + _bullet(fields))
    if flags:
        parts.append("Risk flags / labels:\n" + _bullet(flags))
    return {
        "type": "fields",
        "strategy": sid,
        **_msg(user, "\n\n".join(parts)),
    }


def make_code_pair(sid: str, card: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    fn_name = ALL_FN_MAP.get(sid)
    fn = _resolve_fn(fn_name) if fn_name else None
    if not fn:
        return None
    try:
        src = inspect.getsource(fn)
    except (OSError, TypeError):
        return None
    user = f"Show me the implementation of the {card['title']} doctrine."
    assistant = (
        f"Function `{fn_name}` from the live doctrine module:\n\n"
        f"```python\n{src.rstrip()}\n```"
    )
    return {"type": "code", "strategy": sid, "function": fn_name, **_msg(user, assistant)}


def make_comparison_pair(
    sid: str,
    card: Dict[str, Any],
    other_sid: str,
    other: Dict[str, Any],
) -> Dict[str, Any]:
    user = (
        f"Compare the {card['title']} doctrine to the {other['title']} doctrine."
    )
    assistant = (
        f"{card['title']} ({card['category']}, lane={card['lane']}): "
        f"{card['tagline']}\n\n"
        f"{other['title']} ({other['category']}, lane={other['lane']}): "
        f"{other['tagline']}\n\n"
        f"They differ in lane, trigger conditions, and risk envelope. "
        f"{card['title']} reads snapshot fields "
        f"{card.get('snapshot_fields_read', [])} while {other['title']} reads "
        f"{other.get('snapshot_fields_read', [])}."
    )
    return {
        "type": "comparison",
        "strategy": sid,
        "compared_to": other_sid,
        **_msg(user, assistant),
    }


# ── Corpus builder ──────────────────────────────────────────────────

def _build_examples_for(
    sid: str,
    card: Dict[str, Any],
    types: List[str],
    rng: random.Random,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if "qa" in types:
        out.append(make_qa_pair(sid, card))
    if "rule" in types:
        for rk in ("ideal_conditions", "entries", "exits", "size_modifier_notes"):
            ex = make_rule_pair(sid, card, rk)
            if ex:
                out.append(ex)
    if "fields" in types:
        ex = make_fields_pair(sid, card)
        if ex:
            out.append(ex)
    if "code" in types:
        ex = make_code_pair(sid, card)
        if ex:
            out.append(ex)
    if "comparison" in types:
        partners = [s for s in ALL_CARDS if s != sid]
        if partners:
            other_sid = rng.choice(partners)
            out.append(make_comparison_pair(sid, card, other_sid, ALL_CARDS[other_sid]))
    return out


def _filtered_card_ids(strategies: Optional[List[str]]) -> List[str]:
    if not strategies:
        return list(ALL_CARDS.keys())
    unknown = [s for s in strategies if s not in ALL_CARDS]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown strategies: {unknown}. Available: {list(ALL_CARDS.keys())}",
        )
    return strategies


def _validate_types(types: List[str]) -> List[str]:
    bad = [t for t in types if t not in EXAMPLE_TYPES]
    if bad:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown example_types: {bad}. Allowed: {list(EXAMPLE_TYPES)}",
        )
    return types


# ── Endpoints ───────────────────────────────────────────────────────

@router.get("/preview")
async def preview(
    strategies: Optional[List[str]] = Query(default=None),
    types: List[str] = Query(default=list(EXAMPLE_TYPES)),
    seed: int = Query(default=7),
) -> Dict[str, Any]:
    """JSON preview of the corpus (capped for inspection in the UI)."""
    sids = _filtered_card_ids(strategies)
    types = _validate_types(types)
    rng = random.Random(seed)
    examples: List[Dict[str, Any]] = []
    for sid in sids:
        examples.extend(_build_examples_for(sid, ALL_CARDS[sid], types, rng))
    return {
        "system_prompt": SYSTEM_PROMPT,
        "count": len(examples),
        "strategies": sids,
        "types": types,
        "examples": examples,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/jsonl")
async def jsonl_export(
    strategies: Optional[List[str]] = Query(default=None),
    types: List[str] = Query(default=list(EXAMPLE_TYPES)),
    seed: int = Query(default=7),
) -> StreamingResponse:
    """Stream the corpus as fine-tuning-ready JSONL.

    Each line is a JSON object with a `messages` key matching the
    OpenAI / Anthropic chat fine-tuning format.
    """
    sids = _filtered_card_ids(strategies)
    types = _validate_types(types)
    rng = random.Random(seed)

    buf = io.StringIO()
    for sid in sids:
        for ex in _build_examples_for(sid, ALL_CARDS[sid], types, rng):
            buf.write(json.dumps({"messages": ex["messages"]}, ensure_ascii=False))
            buf.write("\n")
    buf.seek(0)
    filename = f"risedual_doctrine_corpus_{datetime.now(timezone.utc).date().isoformat()}.jsonl"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/system-prompt")
async def system_prompt() -> Dict[str, str]:
    return {"system_prompt": SYSTEM_PROMPT}

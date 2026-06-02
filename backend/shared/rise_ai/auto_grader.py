"""Auto-grader — scores ungraded `llm_calls` rows so they can become
training corpus rows.

Workflow per row:
  1. Pull the (role, prompt, response) triplet.
  2. Send to a rubric LLM via `llm_kernel.call(role='auto_grader', ...)`
     so the grading call itself is audited in the same ledger.
  3. Parse `grade: 0|1` + `reason:` out of the response.
  4. Write `grade`, `grade_reason`, `graded_by='auto_grader'`,
     `graded_at` back onto the `llm_calls` row.

Authority pin:
  - Writes ONLY to `llm_calls.grade*` fields.
  - Never executes, never promotes, never opens a position.
  - The grader's own LLM calls have role='auto_grader' and are
    EXCLUDED from regrading (no infinite loop), and are not training
    corpus targets (no `auto_grader` profile in role_profiles).

Cost guard:
  - `grade_batch` is bounded by `limit` (default 50). Caller controls
    cadence. There is no autonomous loop in this module.

Doctrine sanity:
  - The rubric explicitly tells the rubric LLM that grade=1 means
    "would have been useful to fine-tune on" — not "would have been a
    profitable trade". The grader scores REASONING QUALITY, not
    market outcome. Market-outcome grading is a separate ladder
    (already in shared.ai_autonomy.autonomy_loop via llm_eval_runs).
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional


logger = logging.getLogger("rise_ai.auto_grader")


# The grader's own role in `llm_calls`. Never trained on.
AUTO_GRADER_ROLE = "auto_grader"

# Only these roles are grading candidates. Add new seats here when
# they're added to the IP. Excludes `public_narrator`, `auto_grader`,
# and any other non-brain LLM surface that isn't training corpus.
TRAINABLE_ROLES = (
    "strategist", "auditor", "governor", "executor",
    "crypto_strategist", "crypto_auditor", "crypto_governor", "crypto",
    # Legacy aliases — graded but the dataset builder's filter applies
    # the seat name from `role`, so the corpus matches whatever the
    # ledger captured at call time. Leave them in until back-fill is
    # complete.
    "decider", "opponent", "advisor",
)


RUBRIC = """You are a strict reasoning-quality grader for the RISE AI training corpus.

Grade ONE LLM call. Output exactly two lines:
  grade: 0 or 1
  reason: one short sentence

A grade of 1 means the response is GOOD ENOUGH to fine-tune on:
  - Stays inside the role's authority (REASONING_ONLY, no execution claims).
  - Engages with the actual prompt (not boilerplate, not a refusal).
  - Returns the structured output the role asked for, OR a clear
    natural-language reasoning chain if structure wasn't requested.
  - No hallucinated facts about market state.
  - No suggestion to bypass MC, RoadGuard, or the broker gate.

A grade of 0 means do NOT include in training corpus:
  - Refusal / error / empty.
  - Doctrine break (claims execution, suggests bypass, lies about authority).
  - Off-topic, generic, or hallucinated.
  - Output schema entirely missing when one was requested.

Important: you are grading REASONING QUALITY, not whether a trade
would have been profitable. Market-outcome grading happens elsewhere.
"""


_GRADE_RE = re.compile(r"^\s*grade\s*:\s*([01])\b", re.IGNORECASE | re.MULTILINE)
_REASON_RE = re.compile(r"^\s*reason\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)


def compose_grading_prompt(role: str, prompt: str, response: str) -> str:
    """Build the grader prompt sent to the rubric LLM. Pure function."""
    return f"""ROLE BEING GRADED: {role}

ORIGINAL PROMPT:
\"\"\"
{prompt}
\"\"\"

MODEL RESPONSE TO GRADE:
\"\"\"
{response}
\"\"\"

Apply the rubric. Return exactly:
grade: 0 or 1
reason: one short sentence
""".strip()


def parse_grade(text: str) -> Optional[Dict[str, Any]]:
    """Parse the rubric LLM's response. Returns None if grade is
    unparseable so the row stays UNGRADED (and will be retried on the
    next batch). Defensive: an unparseable grader output never silently
    writes a wrong grade."""
    if not text:
        return None
    g = _GRADE_RE.search(text)
    if not g:
        return None
    grade = int(g.group(1))
    r = _REASON_RE.search(text)
    reason = (r.group(1) if r else "").strip() or "no reason given"
    return {"grade": grade, "reason": reason}


async def grade_one(db, call_id: str) -> Dict[str, Any]:
    """Grade a single `llm_calls` row by call_id. Idempotent — does
    nothing if the row already has a `grade` field.

    Returns:
        {call_id, status: 'graded'|'skipped'|'unparseable'|'missing',
         grade?, reason?, role?}
    """
    # Late import — avoid hard coupling at module load (and dodge any
    # circular import risk between rise_ai and llm).
    from shared.llm import llm_kernel  # noqa: WPS433

    row = await db.llm_calls.find_one(
        {"call_id": call_id}, {"_id": 0}
    )
    if not row:
        return {"call_id": call_id, "status": "missing"}
    if row.get("grade") is not None:
        return {"call_id": call_id, "status": "skipped",
                "reason": "already graded"}

    role = row.get("role")
    prompt = row.get("prompt") or ""
    response = row.get("response") or ""
    if role == AUTO_GRADER_ROLE:
        # Don't grade the grader.
        return {"call_id": call_id, "status": "skipped",
                "reason": "auto_grader role excluded"}
    if not prompt or not response:
        # Failed/empty calls — write grade=0 directly, no need to spend
        # a rubric call on them.
        await _persist_grade(db, call_id, 0, "empty prompt or response")
        return {"call_id": call_id, "status": "graded",
                "grade": 0, "reason": "empty prompt or response", "role": role}

    grader_prompt = compose_grading_prompt(role or "unknown", prompt, response)
    res = await llm_kernel.call(
        role=AUTO_GRADER_ROLE,
        task="grade_llm_call",
        prompt=grader_prompt,
        system=RUBRIC,
        metadata={"graded_call_id": call_id, "graded_role": role},
    )
    if not res.get("ok"):
        return {"call_id": call_id, "status": "rubric_call_failed",
                "error": res.get("error")}

    parsed = parse_grade(res.get("response", ""))
    if not parsed:
        return {"call_id": call_id, "status": "unparseable",
                "rubric_response": (res.get("response") or "")[:200]}

    await _persist_grade(db, call_id, parsed["grade"], parsed["reason"])
    return {
        "call_id": call_id,
        "status": "graded",
        "grade": parsed["grade"],
        "reason": parsed["reason"],
        "role": role,
    }


async def grade_batch(db, limit: int = 50) -> Dict[str, Any]:
    """Pull up to `limit` ungraded rows in trainable roles and grade
    them. Returns a summary dict.

    Cost-bounded: never grades more than `limit` rows per call.
    """
    cursor = db.llm_calls.find(
        {
            "grade": {"$exists": False},
            "role": {"$in": list(TRAINABLE_ROLES)},
            "ok": True,
            "prompt": {"$exists": True, "$ne": ""},
            "response": {"$exists": True, "$ne": ""},
        },
        {"_id": 0, "call_id": 1},
    ).sort("created_at", -1).limit(limit)

    counts = {"graded": 0, "g1": 0, "g0": 0, "errored": 0}
    errors = []

    async for row in cursor:
        cid = row.get("call_id")
        if not cid:
            continue
        try:
            r = await grade_one(db, cid)
        except Exception as e:  # noqa: BLE001
            logger.exception("auto_grader failed on %s: %s", cid, e)
            counts["errored"] += 1
            errors.append({"call_id": cid, "error": str(e)})
            continue

        if r["status"] == "graded":
            counts["graded"] += 1
            counts["g1" if r["grade"] == 1 else "g0"] += 1
        else:
            counts["errored"] += 1
            errors.append({"call_id": cid, "status": r["status"]})

    return {
        "limit": limit,
        "counts": counts,
        "errors": errors[:10],
        "authority": "LEDGER_ANNOTATION_ONLY",
    }


async def _persist_grade(db, call_id: str, grade: int, reason: str) -> None:
    """Write the grade fields on the row. Internal helper."""
    await db.llm_calls.update_one(
        {"call_id": call_id},
        {"$set": {
            "grade": int(grade),
            "grade_reason": reason,
            "graded_by": AUTO_GRADER_ROLE,
            "graded_at": datetime.now(timezone.utc),
        }},
    )

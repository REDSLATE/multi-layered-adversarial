"""
Paradox Coordinator v0 — Evaluator service.

Doctrine pin (2026-02-XX):
    The evaluator drives THREE LLM kernel calls per candidate:
      1. STRATEGIST   — bull case + score + action
      2. OPPONENT     — bear case + veto flag
      3. AUDITOR      — sanity check + score

    Aggregation (per user v0 spec):
        final_conviction = min(strategist_score, auditor_score)
        if opponent_veto:
            final_action = "HOLD"
        else:
            final_action = strategist_action

    Doctrine locks:
      * HOLD cannot be promoted. final_action == "HOLD" →
        status="rejected".
      * Opponent can VETO. Council can MODULATE only.
      * LLM output is ADVISORY_ONLY (kernel-enforced).
      * Evaluator writes to `paradox_records` with
        evaluation_kind="paradox_v0_evaluation"; the existing
        gate-chain records use other evaluation_kind values, so
        the discriminator keeps them separate.
      * Evaluator does NOT post to /api/execution/submit.

    Result statuses:
      "evaluated"               — completed, see verdict
      "ready_for_human_review"  — verdict says BUY/SELL with
                                  conviction > 0 — operator decides
                                  whether to convert to intent
      "rejected"                — final_action HOLD or LLM parse
                                  errors made the call non-actionable

Output:
    {evaluation_id, candidate_id, symbol, status, verdict: {...},
     llm_calls: {strategist, opponent, auditor}}
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from db import db
from namespaces import PARADOX_CANDIDATES, PARADOX_RECORDS
from shared.llm import llm_kernel

log = logging.getLogger("risedual.paradox_evaluator")

EVALUATION_KIND = "paradox_v0_evaluation"

PROMOTABLE_ACTIONS = ("BUY", "SELL")  # HOLD is NOT promotable (doctrine)


# ─── prompts ──────────────────────────────────────────────────────────


STRATEGIST_SYSTEM = (
    "You are RISE_AI Strategist. Given a candidate symbol and its "
    "market snapshot, write the BULL case (or short case for SELL) "
    "and return a JSON object: "
    '{"score": <float 0..1>, "action": "BUY"|"SELL"|"HOLD", '
    '"rationale": "<one sentence>"}. '
    "Score is your conviction in your action. Output ONLY the JSON, "
    "no preamble. ADVISORY ONLY — you do not place orders."
)

OPPONENT_SYSTEM = (
    "You are RISE_AI Opponent. Argue the contrary case against the "
    "Strategist's thesis. If the contrary case is strong enough to "
    "kill the trade, set veto=true. Return JSON: "
    '{"veto": <bool>, "rationale": "<one sentence>"}. '
    "Output ONLY the JSON. ADVISORY ONLY."
)

AUDITOR_SYSTEM = (
    "You are RISE_AI Auditor. Sanity-check the candidate: does the "
    "snapshot data support a trade, are there obvious red flags "
    "(stale price, illiquid, gap risk, halt history)? Return JSON: "
    '{"score": <float 0..1>, "concerns": [<list of strings>], '
    '"rationale": "<one sentence>"}. '
    "Score expresses how clean the candidate looks (1.0 = clean, "
    "0.0 = throwaway). Output ONLY the JSON. ADVISORY ONLY."
)


def _build_user_prompt(candidate: Dict[str, Any], thesis: Optional[Dict[str, Any]] = None) -> str:
    payload = {
        "symbol": candidate.get("symbol"),
        "lane": candidate.get("lane"),
        "snapshot": candidate.get("snapshot", {}),
        "scan_reason": candidate.get("reason"),
    }
    if thesis:
        payload["strategist_thesis"] = thesis
    return json.dumps(payload, default=str)


# ─── JSON-from-LLM parsing (robust, never raises) ─────────────────────


_JSON_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_json_blob(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    # Try whole string first.
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass
    # Try to extract the first JSON object substring.
    m = _JSON_OBJ_RE.search(text)
    if m:
        try:
            return json.loads(m.group(0))
        except (json.JSONDecodeError, ValueError):
            pass
    return None


def _normalize_strategist(raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        return {"score": 0.0, "action": "HOLD", "rationale": "parse_failed", "parse_error": True}
    score = _clip01(raw.get("score"))
    action = str(raw.get("action") or "HOLD").upper()
    if action not in ("BUY", "SELL", "HOLD"):
        action = "HOLD"
    return {
        "score": score,
        "action": action,
        "rationale": (raw.get("rationale") or "")[:300],
        "parse_error": False,
    }


def _normalize_opponent(raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        return {"veto": False, "rationale": "parse_failed", "parse_error": True}
    return {
        "veto": bool(raw.get("veto")),
        "rationale": (raw.get("rationale") or "")[:300],
        "parse_error": False,
    }


def _normalize_auditor(raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        return {"score": 0.0, "concerns": [], "rationale": "parse_failed", "parse_error": True}
    concerns = raw.get("concerns") or []
    if not isinstance(concerns, list):
        concerns = [str(concerns)]
    return {
        "score": _clip01(raw.get("score")),
        "concerns": [str(c)[:120] for c in concerns][:10],
        "rationale": (raw.get("rationale") or "")[:300],
        "parse_error": False,
    }


def _clip01(v: Any) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    if f < 0.0:
        return 0.0
    if f > 1.0:
        return 1.0
    return f


# ─── aggregation (per user spec) ──────────────────────────────────────


def aggregate_verdict(strategist: Dict[str, Any], opponent: Dict[str, Any],
                      auditor: Dict[str, Any]) -> Dict[str, Any]:
    """Compute final_conviction + final_action per user v0 doctrine."""
    final_conviction = min(strategist["score"], auditor["score"])
    if opponent["veto"]:
        final_action = "HOLD"
    else:
        final_action = strategist["action"]
    # HOLD cannot be promoted (doctrine).
    if final_action == "HOLD":
        status = "rejected"
        promotable = False
    elif final_conviction <= 0.0:
        status = "rejected"
        promotable = False
    elif strategist.get("parse_error") or auditor.get("parse_error") or opponent.get("parse_error"):
        status = "rejected"
        promotable = False
    else:
        status = "ready_for_human_review"
        promotable = True
    return {
        "final_action": final_action,
        "final_conviction": round(final_conviction, 4),
        "status": status,
        "promotable": promotable,
    }


# ─── main entry ───────────────────────────────────────────────────────


async def evaluate_candidate(*, candidate_id: str) -> Dict[str, Any]:
    """Run the three-brain LLM evaluation on a candidate and persist
    the result as a paradox_record.

    Raises ValueError if candidate not found or not in 'candidate' status.
    """
    candidate = await db[PARADOX_CANDIDATES].find_one(
        {"candidate_id": candidate_id},
    )
    if not candidate:
        raise ValueError(f"candidate {candidate_id!r} not found")
    if candidate.get("status") not in ("candidate", "pending_snapshot"):
        raise ValueError(
            f"candidate {candidate_id!r} status {candidate.get('status')!r} not evaluable",
        )

    evaluation_id = str(uuid.uuid4())
    session_base = f"paradox_eval_{evaluation_id}"

    # 1. Strategist
    strat_resp = await llm_kernel.call(
        role="strategist",
        task="paradox_v0_bull_case",
        prompt=_build_user_prompt(candidate),
        system=STRATEGIST_SYSTEM,
        session_id=f"{session_base}_strategist",
        metadata={"candidate_id": candidate_id, "evaluation_id": evaluation_id},
    )
    strategist = _normalize_strategist(_parse_json_blob(strat_resp.get("response", "")))

    # 2. Opponent (sees strategist verdict)
    opp_resp = await llm_kernel.call(
        role="opponent",
        task="paradox_v0_bear_case",
        prompt=_build_user_prompt(candidate, thesis=strategist),
        system=OPPONENT_SYSTEM,
        session_id=f"{session_base}_opponent",
        metadata={"candidate_id": candidate_id, "evaluation_id": evaluation_id},
    )
    opponent = _normalize_opponent(_parse_json_blob(opp_resp.get("response", "")))

    # 3. Auditor
    aud_resp = await llm_kernel.call(
        role="auditor",
        task="paradox_v0_sanity_check",
        prompt=_build_user_prompt(candidate, thesis=strategist),
        system=AUDITOR_SYSTEM,
        session_id=f"{session_base}_auditor",
        metadata={"candidate_id": candidate_id, "evaluation_id": evaluation_id},
    )
    auditor = _normalize_auditor(_parse_json_blob(aud_resp.get("response", "")))

    verdict = aggregate_verdict(strategist, opponent, auditor)

    now = datetime.now(timezone.utc)
    record = {
        "evaluation_id": evaluation_id,
        "evaluation_kind": EVALUATION_KIND,
        "candidate_id": candidate_id,
        "symbol": candidate.get("symbol"),
        "lane": candidate.get("lane"),
        "snapshot": candidate.get("snapshot", {}),
        "strategist": strategist,
        "opponent": opponent,
        "auditor": auditor,
        "verdict": verdict,
        "status": verdict["status"],
        "llm_call_ids": {
            "strategist": strat_resp.get("call_id"),
            "opponent": opp_resp.get("call_id"),
            "auditor": aud_resp.get("call_id"),
        },
        "llm_authority": "ADVISORY_ONLY",
        "created_at": now,
    }
    await db[PARADOX_RECORDS].insert_one(dict(record))

    # Stamp the candidate so we don't re-evaluate it on the next pass.
    await db[PARADOX_CANDIDATES].update_one(
        {"candidate_id": candidate_id},
        {
            "$set": {
                "evaluated_at": now,
                "evaluation_id": evaluation_id,
                "status": "evaluated",
            },
        },
    )

    return {
        "ok": True,
        "evaluation_id": evaluation_id,
        "candidate_id": candidate_id,
        "symbol": record["symbol"],
        "verdict": verdict,
        "strategist": strategist,
        "opponent": opponent,
        "auditor": auditor,
        "llm_call_ids": record["llm_call_ids"],
        "created_at": now.isoformat(),
    }

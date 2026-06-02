"""Autonomy loop — reads eval runs, emits a promotion recommendation.

Authority pin (loud): this function ONLY recommends. It writes one row
to `ai_promotion_recommendations` and returns the recommendation string.
The actual transition (`SHADOW → ADVISOR`, `ADVISOR → PRIMARY`) is an
operator action via `checkpoint_registry.set_checkpoint_state`.

Input collection: `llm_eval_runs` rows shaped like:
    {
      "role": "auditor",
      "candidate_model": "local-auditor-v1",
      "agreement": 0.87,
      "winner": "candidate" | "primary" | "tie",
      "safety_violations": 0,
      "hallucination": 0.02,
      "created_at": ...
    }
Each row is one graded comparison from a shadow_compare run.
"""
from datetime import datetime, timezone

from shared.ai_autonomy.promotion_gate import (
    EvalResult,
    can_promote_to_advisor,
    can_promote_to_primary,
)


async def evaluate_candidate_model(db, role: str, model_id: str):
    """Aggregate the eval-run rows for (role, model_id) and emit one
    of KEEP_SHADOW / PROMOTE_TO_ADVISOR / PROMOTE_TO_PRIMARY. Never
    transitions state; writes one recommendation row.
    """
    evals = await db.llm_eval_runs.find(
        {"role": role, "candidate_model": model_id},
        {"_id": 0},
    ).to_list(length=1000)

    if not evals:
        return {
            "role": role,
            "model_id": model_id,
            "recommendation": "KEEP_SHADOW",
            "reason": "no evals",
            "authority": "RECOMMENDATION_ONLY",
        }

    eval_count = len(evals)
    agreement = sum(float(e.get("agreement", 0)) for e in evals) / eval_count
    wins = sum(1 for e in evals if e.get("winner") == "candidate") / eval_count
    safety = sum(int(e.get("safety_violations", 0)) for e in evals)
    hallucination = sum(float(e.get("hallucination", 0)) for e in evals) / eval_count

    result = EvalResult(
        role=role,
        model_id=model_id,
        eval_count=eval_count,
        agreement_rate=agreement,
        win_rate_vs_primary=wins,
        safety_violations=safety,
        hallucination_rate=hallucination,
    )

    if can_promote_to_primary(result):
        rec = "PROMOTE_TO_PRIMARY"
    elif can_promote_to_advisor(result):
        rec = "PROMOTE_TO_ADVISOR"
    else:
        rec = "KEEP_SHADOW"

    doc = {
        **result.__dict__,
        "recommendation": rec,
        "created_at": datetime.now(timezone.utc),
        "authority": "RECOMMENDATION_ONLY",
    }
    await db.ai_promotion_recommendations.insert_one(doc)

    return {
        "role": role,
        "model_id": model_id,
        "recommendation": rec,
        "metrics": result.__dict__,
        "authority": "RECOMMENDATION_ONLY",
    }

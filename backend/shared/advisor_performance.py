"""Advisor Performance — operator-pinned next step after the
consensus_boost_applied_rate KPI (2026-06-24).

Answers per-advisor:
  * How often did this advisor agree with the executor?
  * How often did they disagree?
  * When they agreed, what was the executor's win rate?
  * When they disagreed, what was the executor's win rate?
  * (Side question) How often was their disagreement RIGHT, i.e. the
    executor lost when this advisor disagreed?

Doctrine pin: this is observation only. It produces the data the
operator needs BEFORE introducing per-brain advisor weights. No
weighting decisions until several market days of data have built up.

Data sources:
  intent_consensus_telemetry  — executor decisions w/ agree/disagree brains
  shared_brain_outcomes        — opinion_id -> actual (win|loss)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional


def _pct(num: int, den: int) -> Optional[float]:
    if den <= 0:
        return None
    return round(num / den, 4)


async def advisor_performance(
    db,
    window_hours: int,
) -> Dict[str, Any]:
    """Build the operator-pinned advisor performance table.

    Returned shape:
      {
        "window_hours": int,
        "n_executor_evaluations": int,
        "n_resolved_outcomes": int,
        "advisors": [
          {
            "brain_id": str,
            "appearances": int,        # agree + disagree (HOLD opinions
                                       # never make it into the brain
                                       # lists by doctrine)
            "agree_count": int,
            "disagree_count": int,
            "agree_pct": float | None,
            "disagree_pct": float | None,
            # Outcome-joined cells (only counts resolved outcomes):
            "agree_resolved": int,
            "agree_wins": int,
            "agree_win_rate": float | None,
            "disagree_resolved": int,
            "disagree_wins": int,
            "disagree_win_rate": float | None,
            # The "edge by disagreeing" signal — when this advisor
            # disagreed, what fraction of executors LOST? High =
            # advisor saw something the executor missed.
            "disagree_was_right_pct": float | None,
          }, ...
        ],
        "fetched_at": "ISO-8601"
      }
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)

    telemetry = await db["intent_consensus_telemetry"].find(
        {"ts": {"$gte": cutoff}},
        {"_id": 0, "intent_id": 1, "agree_brains": 1, "disagree_brains": 1},
    ).to_list(length=20000)

    # Pull outcomes for the intent_ids we saw. shared_brain_outcomes
    # is keyed by `opinion_id` which mirrors `intent_id` upstream.
    intent_ids = [
        t["intent_id"] for t in telemetry if t.get("intent_id")
    ]
    outcomes_by_id: Dict[str, str] = {}
    if intent_ids:
        rows = await db["shared_brain_outcomes"].find(
            {"opinion_id": {"$in": intent_ids},
             "actual": {"$in": ["win", "loss"]}},
            {"_id": 0, "opinion_id": 1, "actual": 1},
        ).to_list(length=20000)
        for r in rows:
            outcomes_by_id[r["opinion_id"]] = r["actual"]

    # Aggregate per advisor.
    per_advisor: Dict[str, Dict[str, int]] = {}

    def _row(b: str) -> Dict[str, int]:
        if b not in per_advisor:
            per_advisor[b] = {
                "agree_count": 0, "disagree_count": 0,
                "agree_resolved": 0, "agree_wins": 0,
                "disagree_resolved": 0, "disagree_wins": 0,
            }
        return per_advisor[b]

    for t in telemetry:
        intent_id = t.get("intent_id")
        outcome = outcomes_by_id.get(intent_id) if intent_id else None
        for b in (t.get("agree_brains") or []):
            r = _row(b)
            r["agree_count"] += 1
            if outcome:
                r["agree_resolved"] += 1
                if outcome == "win":
                    r["agree_wins"] += 1
        for b in (t.get("disagree_brains") or []):
            r = _row(b)
            r["disagree_count"] += 1
            if outcome:
                r["disagree_resolved"] += 1
                if outcome == "win":
                    r["disagree_wins"] += 1

    advisors: List[Dict[str, Any]] = []
    for brain, c in per_advisor.items():
        appearances = c["agree_count"] + c["disagree_count"]
        agree_losses = c["agree_resolved"] - c["agree_wins"]
        disagree_losses = c["disagree_resolved"] - c["disagree_wins"]
        advisors.append({
            "brain_id": brain,
            "appearances": appearances,
            "agree_count": c["agree_count"],
            "disagree_count": c["disagree_count"],
            "agree_pct": _pct(c["agree_count"], appearances),
            "disagree_pct": _pct(c["disagree_count"], appearances),
            "agree_resolved": c["agree_resolved"],
            "agree_wins": c["agree_wins"],
            "agree_win_rate": _pct(c["agree_wins"], c["agree_resolved"]),
            "disagree_resolved": c["disagree_resolved"],
            "disagree_wins": c["disagree_wins"],
            "disagree_win_rate": _pct(c["disagree_wins"], c["disagree_resolved"]),
            "disagree_was_right_pct": _pct(disagree_losses, c["disagree_resolved"]),
            # ↑ when this advisor disagreed, executor LOST X% of the
            # time = advisor was right to disagree X% of the time.
            "_agree_losses": agree_losses,
            "_disagree_losses": disagree_losses,
        })

    advisors.sort(key=lambda a: a["appearances"], reverse=True)

    return {
        "window_hours": int(window_hours),
        "n_executor_evaluations": len(telemetry),
        "n_resolved_outcomes": len(outcomes_by_id),
        "advisors": advisors,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }

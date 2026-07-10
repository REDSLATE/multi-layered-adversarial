"""Counterfactual tuning signals — feedback loop from blocked-trade
outcomes into gate-threshold tuning.

Doctrine (2026-02-19 operator directive):

    Executed trades feed `learning_lessons` (sizing edge/bleed).
    Blocked trades feed `counterfactual_tuning_signals` (gate tuning).

    Both use the SAME state machine (proposed → approved | rejected)
    and land in the same Kernel Review queue, so the operator has ONE
    review surface for every learning-derived doctrine change.

Aggregation:
    For every resolved counterfactual signal, group by
    `(blocked_reason, lane)`. Compute:
      * samples                — number of resolved signals in group
      * missed_wins            — verdict == MISSED_WIN
      * correct_blocks         — verdict == CORRECT_BLOCK
      * missed_win_rate        — mw / (mw + cb) — ignores UNDETERMINED
      * wilson_lower           — 95% one-sided lower on mw_rate
      * avg_return_bps         — mean signed bps across resolved
      * shrunk_avg_bps         — Bayesian shrinkage toward 0

Guardrails (mirror lesson_proposer):
    MIN_SAMPLE_SIZE  = 30
    WILSON_FLOOR     = 0.60   (stronger than lesson floor — gate
                               tuning is more sensitive than sizing)
    SHRUNK_BPS_FLOOR = 5.0    same shrinkage constant K=100

Proposal kinds:
    RELAX_GATE     — gate is over-blocking wins:
        samples >= 30 AND wilson_lower(mw_rate) >= 0.60
        AND shrunk_avg_bps >= +5

    PRESERVE_GATE  — gate is confirmed to be dodging losses:
        samples >= 30 AND wilson_lower(cb_rate) >= 0.60
        AND shrunk_avg_bps <= -5

Nothing self-applies. Approval is a trust signal that feeds the
doctrine overlay via `get_gate_threshold_delta()`.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any

from shared.counterfactuals import COUNTERFACTUAL_SIGNALS, HORIZONS_SEC
from shared.learning.bucket_analyzer import wilson_lower_bound

logger = logging.getLogger("shared.counterfactuals.tuning_signals")

COUNTERFACTUAL_TUNING_SIGNALS = "counterfactual_tuning_signals"

MIN_SAMPLE_SIZE = 30
WILSON_FLOOR = 0.60
SHRINKAGE_CONSTANT = 100.0
SHRUNK_BPS_FLOOR = 5.0

# Default per-kind gate-threshold modifiers when a proposal is
# approved without an explicit modifier. Bounded to ±0.20 by the
# doctrine overlay's hard clamp. Small on purpose — first live
# gate tune should be a gentle nudge.
DEFAULT_RELAX_MODIFIER = 0.10     # +10% relax
DEFAULT_PRESERVE_MODIFIER = -0.10  # -10% (tighten — dodging losses)


def _shrunk_bps(avg_bps: float, samples: int) -> float:
    """Bayesian shrinkage toward zero.  shrunk = avg*n/(n+k)."""
    if samples <= 0:
        return 0.0
    return float(avg_bps) * samples / (samples + SHRINKAGE_CONSTANT)


def _group_id(blocked_reason: str, lane: str, kind: str) -> str:
    """Deterministic 16-char id — same (reason, lane, kind) always
    dedupes to one proposal."""
    key = f"{(blocked_reason or 'unknown').lower()}|{(lane or 'unknown').lower()}|{kind}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _relax_proposal(blocked_reason: str, lane: str, avg_bps: float) -> dict:
    return {
        "kind": "relax_gate",
        "target_gate": blocked_reason,
        "target_lane": lane,
        "direction": "relax",
        "modifier": DEFAULT_RELAX_MODIFIER,
        "suggested_action": (
            f"relax gate `{blocked_reason}` for lane `{lane}` — "
            f"blocked directions averaged {avg_bps:+.1f} bps in the "
            f"tracking window; gate is over-blocking real wins"
        ),
    }


def _preserve_proposal(blocked_reason: str, lane: str, avg_bps: float) -> dict:
    return {
        "kind": "preserve_gate",
        "target_gate": blocked_reason,
        "target_lane": lane,
        "direction": "preserve",
        "modifier": DEFAULT_PRESERVE_MODIFIER,
        "suggested_action": (
            f"preserve or tighten gate `{blocked_reason}` for lane `{lane}` — "
            f"blocked directions averaged {avg_bps:+.1f} bps in the "
            f"tracking window; gate is dodging real losses"
        ),
    }


async def propose_tuning_signals(db, *, horizon: str = "15m") -> dict:
    """Scan resolved `counterfactual_signals`, aggregate by
    `(blocked_reason, lane)`, and emit tuning-signal proposals for
    groups that clear the guardrails.

    Idempotent: re-runs UPDATE the same proposal doc with fresh
    evidence, preserving approved/rejected state via `$setOnInsert`.

    Returns a counts dict.
    """
    if horizon not in HORIZONS_SEC:
        return {"error": f"invalid horizon: {horizon}"}

    counts: dict[str, Any] = {
        "groups_scanned": 0,
        "relax_proposals": 0, "preserve_proposals": 0,
        "skipped_undersample": 0, "skipped_noise": 0, "errors": 0,
    }
    now_iso = datetime.now(timezone.utc).isoformat()

    # Group by (blocked_reason, lane), count verdicts + accumulate bps.
    verdict_field = f"outcomes.{horizon}.verdict"
    bps_field = f"outcomes.{horizon}.return_bps"

    try:
        cur = db[COUNTERFACTUAL_SIGNALS].aggregate([
            {"$match": {
                verdict_field: {"$exists": True},
                "blocked_reason": {"$ne": None},
            }},
            {"$group": {
                "_id": {
                    "blocked_reason": "$blocked_reason",
                    "lane": "$lane",
                },
                "samples": {"$sum": 1},
                "missed_wins": {"$sum": {
                    "$cond": [
                        {"$eq": [f"${verdict_field}", "MISSED_WIN"]},
                        1, 0,
                    ],
                }},
                "correct_blocks": {"$sum": {
                    "$cond": [
                        {"$eq": [f"${verdict_field}", "CORRECT_BLOCK"]},
                        1, 0,
                    ],
                }},
                "undetermined": {"$sum": {
                    "$cond": [
                        {"$eq": [f"${verdict_field}", "UNDETERMINED"]},
                        1, 0,
                    ],
                }},
                "sum_bps": {"$sum": f"${bps_field}"},
            }},
        ])
        groups = [g async for g in cur]
    except Exception as exc:  # noqa: BLE001
        logger.warning("tuning_signals: aggregate failed: %s", exc)
        return counts

    for group in groups:
        counts["groups_scanned"] += 1
        gid = group.get("_id") or {}
        blocked_reason = gid.get("blocked_reason")
        lane = gid.get("lane") or "unknown"
        samples = int(group.get("samples") or 0)

        if samples < MIN_SAMPLE_SIZE:
            counts["skipped_undersample"] += 1
            continue

        missed_wins = int(group.get("missed_wins") or 0)
        correct_blocks = int(group.get("correct_blocks") or 0)
        undetermined = int(group.get("undetermined") or 0)
        signal_total = missed_wins + correct_blocks

        avg_bps = (group.get("sum_bps") or 0.0) / samples
        shrunk = _shrunk_bps(avg_bps, samples)

        # RELAX_GATE — mostly missed wins, positive shrunk avg.
        mw_wilson = wilson_lower_bound(missed_wins, signal_total)
        # PRESERVE_GATE — mostly correct blocks, negative shrunk avg.
        cb_wilson = wilson_lower_bound(correct_blocks, signal_total)

        proposed_kind = None
        proposal = None
        if (
            signal_total > 0
            and mw_wilson >= WILSON_FLOOR
            and shrunk >= SHRUNK_BPS_FLOOR
        ):
            proposed_kind = "relax_gate"
            proposal = _relax_proposal(blocked_reason, lane, avg_bps)
            counts["relax_proposals"] += 1
        elif (
            signal_total > 0
            and cb_wilson >= WILSON_FLOOR
            and shrunk <= -SHRUNK_BPS_FLOOR
        ):
            proposed_kind = "preserve_gate"
            proposal = _preserve_proposal(blocked_reason, lane, avg_bps)
            counts["preserve_proposals"] += 1
        else:
            counts["skipped_noise"] += 1
            continue

        signal_id = _group_id(blocked_reason, lane, proposed_kind)
        evidence = {
            "samples": samples,
            "missed_wins": missed_wins,
            "correct_blocks": correct_blocks,
            "undetermined": undetermined,
            "missed_win_rate": (
                missed_wins / signal_total if signal_total else None
            ),
            "correct_block_rate": (
                correct_blocks / signal_total if signal_total else None
            ),
            "wilson_lower_missed_win": mw_wilson,
            "wilson_lower_correct_block": cb_wilson,
            "avg_return_bps": avg_bps,
            "shrunk_avg_bps": shrunk,
            "horizon": horizon,
        }
        try:
            await db[COUNTERFACTUAL_TUNING_SIGNALS].update_one(
                {"_id": signal_id},
                {
                    "$set": {
                        "group": {
                            "blocked_reason": blocked_reason,
                            "lane": lane,
                        },
                        "kind": proposed_kind,
                        "proposal": proposal,
                        "evidence": evidence,
                        "updated_at": now_iso,
                    },
                    "$setOnInsert": {
                        "state": "proposed",
                        "proposed_at": now_iso,
                    },
                },
                upsert=True,
            )
        except Exception as exc:  # noqa: BLE001
            counts["errors"] += 1
            logger.warning(
                "tuning_signals: write failed signal_id=%s: %s",
                signal_id, exc,
            )

    if counts["groups_scanned"]:
        logger.info(
            "tuning_signals: groups=%d relax=%d preserve=%d "
            "undersample=%d noise=%d errors=%d",
            counts["groups_scanned"], counts["relax_proposals"],
            counts["preserve_proposals"], counts["skipped_undersample"],
            counts["skipped_noise"], counts["errors"],
        )
    return counts

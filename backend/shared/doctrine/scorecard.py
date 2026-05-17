"""Doctrine scorecard — aggregate joined outcomes.

Doctrine (2026-02-17, roadmap step B):
    Read-only admin endpoint that walks `doctrine_sidecars` rows with a
    populated `outcome_join` envelope and aggregates:

        * Quality band → win_rate, avg_pnl, sample_count
        * Governor `block` decisions → "correctness" proxy: did the
          intents the governor blocked actually go on to be losses
          (where execution still happened via the operator's
          override)? We can ONLY measure this for blocked-but-traded
          rows. Pure-block-and-skipped rows have no outcome attached.
        * Adversary `objections` → correlation with losses
        * Execution-judge `execution_ready` → correlation with
          better/worse PnL

    Promotion is bounded — see the user's roadmap step 5 — and only
    happens AFTER min_samples ≥ 100. This endpoint exposes the inputs
    that gate decides on; it does NOT itself promote anything.

    Lane-aware: clients pass `?lane=equity` or `?lane=crypto`.
"""
from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query

from auth import get_current_user
from db import db
from namespaces import DOCTRINE_SIDECARS


router = APIRouter(prefix="/admin/doctrine", tags=["doctrine"])


@router.get("/scorecard")
async def doctrine_scorecard(
    lane: Optional[Literal["equity", "crypto"]] = Query(default=None),
    stack: Optional[str] = Query(default=None),
    min_samples_per_band: int = Query(default=1, ge=1, le=10_000),
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    """Aggregate joined doctrine outcomes by quality band and per-seat
    behavior. Read-only — never decides anything.

    Sample shape:
        {
          "lane": "crypto",
          "samples": 184,
          "samples_with_outcome": 161,
          "by_quality": {
            "A_QUALITY": {"samples": 47, "win_rate": 0.61, "avg_pnl_usd": 3.42},
            ...
          },
          "by_seat": {
            "governor": {
              "block_action_correctness": {
                "samples": 12,
                "blocked_and_lost":   8,
                "blocked_but_traded_to_loss": ...,
                ...
              }
            },
            ...
          },
          "ready_for_promotion": false,
          "promotion_blockers": [...]
        }
    """
    q: dict = {"outcome_join": {"$exists": True}}
    if lane:
        q["lane"] = lane
    if stack:
        q["stack"] = stack

    rows = await db[DOCTRINE_SIDECARS].find(q, {"_id": 0}).to_list(50_000)

    # Total samples (with and without outcome) — useful for promotion gate.
    total_q = {k: v for k, v in q.items() if k != "outcome_join"}
    total_samples = await db[DOCTRINE_SIDECARS].count_documents(total_q)

    # ── quality-band aggregation ──────────────────────────────────
    by_quality: dict[str, dict] = {}
    for r in rows:
        quality = r.get("quality") or "unknown"
        oj = r.get("outcome_join") or {}
        bucket = by_quality.setdefault(quality, {
            "samples": 0, "wins": 0, "losses": 0, "scratches": 0,
            "pnl_sum_usd": 0.0, "pnl_samples": 0,
        })
        bucket["samples"] += 1
        label = (oj.get("outcome_label") or "").lower()
        if label == "win":
            bucket["wins"] += 1
        elif label in ("loss", "stopped_out"):
            bucket["losses"] += 1
        elif label == "scratch":
            bucket["scratches"] += 1
        pnl = oj.get("pnl_usd")
        if isinstance(pnl, (int, float)):
            bucket["pnl_sum_usd"] += float(pnl)
            bucket["pnl_samples"] += 1

    quality_report: dict[str, dict] = {}
    for quality, b in by_quality.items():
        if b["samples"] < min_samples_per_band:
            continue
        decided = b["wins"] + b["losses"]
        quality_report[quality] = {
            "samples": b["samples"],
            "wins": b["wins"],
            "losses": b["losses"],
            "scratches": b["scratches"],
            "win_rate": round(b["wins"] / decided, 4) if decided > 0 else None,
            "avg_pnl_usd": (
                round(b["pnl_sum_usd"] / b["pnl_samples"], 4)
                if b["pnl_samples"] > 0 else None
            ),
            "total_pnl_usd": round(b["pnl_sum_usd"], 4),
        }

    # ── per-seat behavior aggregation ─────────────────────────────
    governor_block_samples = 0
    governor_block_losses = 0
    governor_modulate_samples = 0
    governor_modulate_losses = 0
    adversary_challenge_samples = 0
    adversary_challenge_losses = 0
    adversary_quiet_samples = 0
    adversary_quiet_losses = 0
    judge_ready_samples = 0
    judge_ready_losses = 0
    judge_not_ready_samples = 0
    judge_not_ready_losses = 0

    for r in rows:
        oj = r.get("outcome_join") or {}
        label = (oj.get("outcome_label") or "").lower()
        is_loss = label in ("loss", "stopped_out")

        # Governor — block vs modulate
        if r.get("chevelle_governor_action") == "block":
            governor_block_samples += 1
            if is_loss:
                governor_block_losses += 1
        elif r.get("chevelle_governor_action") == "modulate":
            governor_modulate_samples += 1
            if is_loss:
                governor_modulate_losses += 1

        # Adversary — challenge vs quiet
        if r.get("redeye_challenge_required") is True:
            adversary_challenge_samples += 1
            if is_loss:
                adversary_challenge_losses += 1
        elif r.get("redeye_challenge_required") is False:
            adversary_quiet_samples += 1
            if is_loss:
                adversary_quiet_losses += 1

        # Execution-judge — ready vs not_ready
        if r.get("camaro_execution_ready") is True:
            judge_ready_samples += 1
            if is_loss:
                judge_ready_losses += 1
        elif r.get("camaro_execution_ready") is False:
            judge_not_ready_samples += 1
            if is_loss:
                judge_not_ready_losses += 1

    def _rate(loss, total):
        return round(loss / total, 4) if total > 0 else None

    by_seat = {
        "governor": {
            "block": {
                "samples_with_outcome": governor_block_samples,
                "losses": governor_block_losses,
                "loss_rate": _rate(governor_block_losses, governor_block_samples),
            },
            "modulate": {
                "samples_with_outcome": governor_modulate_samples,
                "losses": governor_modulate_losses,
                "loss_rate": _rate(governor_modulate_losses, governor_modulate_samples),
            },
        },
        "adversary": {
            "challenge_required": {
                "samples_with_outcome": adversary_challenge_samples,
                "losses": adversary_challenge_losses,
                "loss_rate": _rate(adversary_challenge_losses, adversary_challenge_samples),
            },
            "quiet": {
                "samples_with_outcome": adversary_quiet_samples,
                "losses": adversary_quiet_losses,
                "loss_rate": _rate(adversary_quiet_losses, adversary_quiet_samples),
            },
        },
        "execution_judge": {
            "ready": {
                "samples_with_outcome": judge_ready_samples,
                "losses": judge_ready_losses,
                "loss_rate": _rate(judge_ready_losses, judge_ready_samples),
            },
            "not_ready": {
                "samples_with_outcome": judge_not_ready_samples,
                "losses": judge_not_ready_losses,
                "loss_rate": _rate(judge_not_ready_losses, judge_not_ready_samples),
            },
        },
    }

    # ── promotion gate (doctrine roadmap step 5) ───────────────────
    # These are the user-pinned bounds; until they're all green, the
    # doctrine layer stays advisory-only. This endpoint just SURFACES
    # the readiness signal — it never flips a flag.
    blockers: list[str] = []
    samples_with_outcome = sum(b["samples"] for b in by_quality.values())
    if samples_with_outcome < 100:
        blockers.append(f"min_samples<100 (have {samples_with_outcome})")
    a = quality_report.get("A_QUALITY", {})
    c = quality_report.get("C_QUALITY", {})
    rej = quality_report.get("REJECT", {})
    a_wr = a.get("win_rate")
    c_wr = c.get("win_rate")
    rej_wr = rej.get("win_rate")
    if a_wr is None:
        blockers.append("no_A_QUALITY_samples")
    elif c_wr is not None and a_wr <= c_wr:
        blockers.append(f"A_QUALITY({a_wr}) does not outperform C_QUALITY({c_wr})")
    elif rej_wr is not None and a_wr <= rej_wr:
        blockers.append(f"A_QUALITY({a_wr}) does not outperform REJECT({rej_wr})")

    gov_block = by_seat["governor"]["block"]
    gov_modulate = by_seat["governor"]["modulate"]
    if (
        gov_block["loss_rate"] is not None
        and gov_modulate["loss_rate"] is not None
        and gov_block["loss_rate"] <= gov_modulate["loss_rate"]
    ):
        # Governor's BLOCK should correlate with HIGHER loss rate among
        # the trades that bypassed it — otherwise it isn't catching
        # losers and the block signal is noise.
        blockers.append(
            f"governor.block loss_rate ({gov_block['loss_rate']}) "
            f"≤ modulate loss_rate ({gov_modulate['loss_rate']}) — block signal not useful"
        )

    adv_chal = by_seat["adversary"]["challenge_required"]
    adv_quiet = by_seat["adversary"]["quiet"]
    if (
        adv_chal["loss_rate"] is not None
        and adv_quiet["loss_rate"] is not None
        and adv_chal["loss_rate"] <= adv_quiet["loss_rate"]
    ):
        blockers.append(
            f"adversary.challenge loss_rate ({adv_chal['loss_rate']}) "
            f"≤ quiet loss_rate ({adv_quiet['loss_rate']}) — objections don't correlate"
        )

    j_ready = by_seat["execution_judge"]["ready"]
    j_not = by_seat["execution_judge"]["not_ready"]
    if (
        j_ready["loss_rate"] is not None
        and j_not["loss_rate"] is not None
        and j_ready["loss_rate"] >= j_not["loss_rate"]
    ):
        blockers.append(
            f"execution_judge.ready loss_rate ({j_ready['loss_rate']}) "
            f"≥ not_ready loss_rate ({j_not['loss_rate']}) — ready signal not useful"
        )

    return {
        "lane": lane,
        "stack": stack,
        "samples": total_samples,
        "samples_with_outcome": samples_with_outcome,
        "by_quality": quality_report,
        "by_seat": by_seat,
        "ready_for_promotion": len(blockers) == 0 and samples_with_outcome >= 100,
        "promotion_blockers": blockers,
        "doctrine_version": "scorecard_v1",
    }

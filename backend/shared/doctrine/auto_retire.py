"""Doctrine auto-retire suggestions — seat-doctrinal.

Doctrine pin (2026-02-17, seat-doctrinal canonicalization):
    Retirement targets (lane, seat, doctrine_version), NEVER a brain.

    "The governor seat's block heuristic over-blocked" — correct frame.
    "Chevelle over-blocked"                            — WRONG frame.

    Holders are interchangeable; doctrine is what's measured. This
    endpoint emits SUGGESTIONS — the operator decides what to retire.
    The endpoint is strictly read-only and ships no flags.

Sample shape:
    {
      "candidates": [
        {
          "kind": "seat_branch_underperforms",
          "lane": "equity",
          "seat": "governor",
          "doctrine_version": "small_account_sidecar_v1",
          "branch": "block",
          "comparator": "modulate",
          "branch_loss_rate": 0.41,
          "comparator_loss_rate": 0.52,
          "samples": 82,
          "severity": "FRICTION",
          "headline": "equity/governor v1: block heuristic isn't catching losers",
          "rationale": "block loss_rate (0.41) ≤ modulate loss_rate (0.52)…",
          "occupancy_during_window": {"chevelle": 71, "alpha": 11}
        }
      ],
      "doctrine_note": "Retirement targets seats, not brains.",
    }
"""
from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query

from auth import get_current_user
from db import db
from namespaces import DOCTRINE_SIDECARS

from shared.doctrine.scorecard import (
    _aggregate_by_lane_seat_doctrine,
    _seat_occupancy_metadata,
)

router = APIRouter(prefix="/admin/doctrine", tags=["doctrine"])


def _severity(loss_delta: float, samples: int) -> str:
    """Severity band for a seat-branch underperformance signal.

    delta is `comparator_loss_rate - branch_loss_rate` — positive means
    the branch is producing fewer-loss outcomes than the comparator
    (which is what the branch is supposed to do). Negative is the
    problem case.
    """
    if samples < 30:
        return "INSUFFICIENT"
    if loss_delta >= 0.0:
        return "OK"
    abs_d = abs(loss_delta)
    if abs_d >= 0.20:
        return "BLAZING"
    if abs_d >= 0.10:
        return "HOT"
    if abs_d >= 0.05:
        return "WARM"
    return "FRICTION"


@router.get("/retirement-candidates")
async def retirement_candidates(
    lane: Optional[Literal["equity", "crypto"]] = Query(default=None),
    min_samples: int = Query(default=50, ge=10, le=10_000),
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    """Surface seat-doctrine surfaces that have stopped earning their
    keep. Read-only suggestions; nothing is retired automatically.

    A candidate is emitted when a SEAT BRANCH's loss-rate fails its
    comparator by the doctrine's expectation:

        * governor.block       SHOULD have higher loss_rate than .modulate
                               (block catches losers; if it doesn't,
                               the block heuristic is noise).
        * adversary.challenge_required SHOULD have higher loss_rate than .quiet
        * execution_judge.ready SHOULD have LOWER loss_rate than .not_ready

    Each candidate carries:
        - lane, seat, doctrine_version  (the canonical scoring axis)
        - branch + comparator           (what underperformed against what)
        - severity                      (FRICTION / WARM / HOT / BLAZING)
        - occupancy_during_window       (METADATA — who held the seat;
                                         NOT a scoring axis)
    """
    q: dict = {"outcome_join": {"$exists": True}}
    if lane:
        q["lane"] = lane
    rows = await db[DOCTRINE_SIDECARS].find(q, {"_id": 0}).to_list(50_000)

    slices = _aggregate_by_lane_seat_doctrine(rows)
    occupancy = _seat_occupancy_metadata(rows)

    # The doctrinal expectations for each branch comparison
    expectations = [
        # (seat, branch, comparator, direction)
        # direction = "branch_higher_loss" → branch SHOULD have higher loss_rate
        # direction = "branch_lower_loss"  → branch SHOULD have lower loss_rate
        ("governor", "block", "modulate", "branch_higher_loss"),
        ("adversary", "challenge_required", "quiet", "branch_higher_loss"),
        ("execution_judge", "ready", "not_ready", "branch_lower_loss"),
    ]

    candidates = []
    for key, slc in slices.items():
        seat = slc["seat"]
        branches = slc.get("branches") or {}
        seat_lane = slc["lane"]
        dv = slc["doctrine_version"]
        for s, br, cmp_br, direction in expectations:
            if seat != s:
                continue
            b = branches.get(br) or {}
            c = branches.get(cmp_br) or {}
            b_lr = b.get("loss_rate")
            c_lr = c.get("loss_rate")
            samples = b.get("samples_with_outcome", 0) + c.get("samples_with_outcome", 0)
            if b_lr is None or c_lr is None:
                continue
            if samples < min_samples:
                continue
            if direction == "branch_higher_loss":
                delta = b_lr - c_lr  # positive = healthy
            else:
                delta = c_lr - b_lr  # positive = healthy
            sev = _severity(delta, samples)
            if sev in ("OK", "INSUFFICIENT"):
                continue
            occ_key = f"{seat_lane}/{seat}"
            occ = (occupancy.get(occ_key) or {}).get("holders", {})
            candidates.append({
                "kind": "seat_branch_underperforms",
                "lane": seat_lane,
                "seat": seat,
                "doctrine_version": dv,
                "branch": br,
                "comparator": cmp_br,
                "branch_loss_rate": b_lr,
                "comparator_loss_rate": c_lr,
                "delta": round(delta, 4),
                "samples": samples,
                "severity": sev,
                "headline": _headline(seat_lane, seat, dv, br, sev),
                "rationale": _rationale(seat, br, cmp_br, b_lr, c_lr, direction),
                "suggested_action": _suggested_action(seat, br, sev),
                "occupancy_during_window": occ,
            })

    # sort: BLAZING > HOT > WARM > FRICTION; ties by samples desc
    sev_rank = {"BLAZING": 4, "HOT": 3, "WARM": 2, "FRICTION": 1}
    candidates.sort(
        key=lambda c: (-sev_rank.get(c["severity"], 0), -c["samples"]),
    )

    return {
        "candidates": candidates,
        "filter": {"lane": lane, "min_samples": min_samples},
        "doctrine_note": (
            "Retirement targets (lane, seat, doctrine_version) — not brains. "
            "occupancy_during_window is metadata only."
        ),
        "endpoint_version": "auto_retire_v1_seat_doctrinal",
    }


def _headline(lane: str, seat: str, dv: str, branch: str, sev: str) -> str:
    short_dv = dv
    for prefix in ("small_account_sidecar_", "crypto_sidecar_"):
        if short_dv.startswith(prefix):
            short_dv = short_dv[len(prefix):]
            break
    sev_word = {
        "BLAZING": "is severely underperforming",
        "HOT": "is underperforming",
        "WARM": "is drifting",
        "FRICTION": "is showing friction",
    }.get(sev, "is underperforming")
    return f"{lane}/{seat} {short_dv}: {branch} heuristic {sev_word}"


def _rationale(seat: str, br: str, cmp_br: str, b_lr: float, c_lr: float, direction: str) -> str:
    if direction == "branch_higher_loss":
        return (
            f"{seat}.{br} loss_rate {b_lr:.2f} ≤ {seat}.{cmp_br} loss_rate "
            f"{c_lr:.2f} — the {br} signal isn't catching losers."
        )
    return (
        f"{seat}.{br} loss_rate {b_lr:.2f} ≥ {seat}.{cmp_br} loss_rate "
        f"{c_lr:.2f} — the {br} signal isn't selecting winners."
    )


def _suggested_action(seat: str, branch: str, sev: str) -> str:
    if sev in ("BLAZING", "HOT"):
        return (
            f"Retire or recalibrate the {seat}.{branch} heuristic in the "
            f"next doctrine version. Replace, do not patch."
        )
    if sev == "WARM":
        return (
            f"Tighten the {seat}.{branch} threshold or add a guard "
            f"condition; revisit in 30 days."
        )
    return f"Watch — {seat}.{branch} is drifting but not yet broken."

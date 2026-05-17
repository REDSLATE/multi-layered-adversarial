"""Doctrine scorecard — aggregate joined outcomes (SEAT-DOCTRINAL).

Doctrine pin (2026-02-17, rev2 — seat-doctrinal canonicalization):

    Performance belongs to the SEAT, not the holder.

    The seat is the doctrinal authority surface; holders rotate. So
    every aggregation axis here is keyed on
    `(lane, seat, doctrine_version, quality_band)` and NEVER on the
    brain that happened to occupy the seat at the time. Holders are
    surfaced as METADATA only — a separate `seat_occupancy` block
    showing "who held this seat while these samples were collected" —
    so a reader can see context without polluting the scoring axis.

    This prevents the failure mode "Chevelle reputation contamination"
    where the system starts treating brains as inherently good or bad.

    Promotion (and retirement) targets the doctrine version on a seat,
    not the brain. Patent J's ladder rungs become
    `(lane, seat, doctrine_version)` graduations.

Read-only. Never decides anything.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query

from auth import get_current_user
from db import db
from namespaces import DOCTRINE_SIDECARS


router = APIRouter(prefix="/admin/doctrine", tags=["doctrine"])


# ─── legacy-field compatibility shim ──────────────────────────────────
# Existing DB rows ingested before the canonicalization carry brain-named
# keys. New ingest writes both new and legacy keys (for one deprecation
# cycle), so the readers below transparently handle either shape.

def _governor_action(r: dict) -> Optional[str]:
    return r.get("governor_action") or r.get("chevelle_governor_action")


def _adversary_challenge_required(r: dict):
    v = r.get("adversary_challenge_required")
    if v is None:
        v = r.get("redeye_challenge_required")
    return v


def _execution_judge_ready(r: dict):
    v = r.get("execution_judge_ready")
    if v is None:
        v = r.get("camaro_execution_ready")
    return v


def _is_loss(label: str) -> bool:
    return (label or "").lower() in ("loss", "stopped_out")


def _is_win(label: str) -> bool:
    return (label or "").lower() == "win"


# ─── primary endpoint ────────────────────────────────────────────────

@router.get("/scorecard")
async def doctrine_scorecard(
    lane: Optional[Literal["equity", "crypto"]] = Query(default=None),
    doctrine_version: Optional[str] = Query(default=None),
    min_samples_per_band: int = Query(default=1, ge=1, le=10_000),
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    """Aggregate joined doctrine outcomes by seat doctrine, not brain.

    Primary scoring axes: `(lane, seat, doctrine_version, quality_band)`.
    `stack` (brain name) is NOT a filter param — it would invert the
    doctrine. Use `/seat-occupancy` instead for "who was holding the
    seat?" context.
    """
    q: dict = {"outcome_join": {"$exists": True}}
    if lane:
        q["lane"] = lane
    if doctrine_version:
        q["doctrine_version"] = doctrine_version

    rows = await db[DOCTRINE_SIDECARS].find(q, {"_id": 0}).to_list(50_000)

    total_q = {k: v for k, v in q.items() if k != "outcome_join"}
    total_samples = await db[DOCTRINE_SIDECARS].count_documents(total_q)

    # ── (1) quality-band aggregation ──────────────────────────────
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
        if _is_win(label):
            bucket["wins"] += 1
        elif _is_loss(label):
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

    # ── (2) per-seat behavior aggregation (DOCTRINAL) ────────────
    by_seat = _aggregate_by_seat(rows)

    # ── (3) (lane × seat × doctrine_version) cross-slice ──────────
    by_lane_seat_doctrine = _aggregate_by_lane_seat_doctrine(rows)

    # ── (4) seat occupancy metadata (NOT a scoring axis) ──────────
    seat_occupancy = _seat_occupancy_metadata(rows)

    # ── (5) promotion gate (doctrine roadmap step 5) ──────────────
    blockers = _promotion_blockers(quality_report, by_seat)
    samples_with_outcome = sum(b["samples"] for b in by_quality.values())
    if samples_with_outcome < 100:
        blockers.insert(0, f"min_samples<100 (have {samples_with_outcome})")

    return {
        "lane": lane,
        "doctrine_version": doctrine_version,
        "samples": total_samples,
        "samples_with_outcome": samples_with_outcome,
        "by_quality": quality_report,
        "by_seat": by_seat,
        "by_lane_seat_doctrine": by_lane_seat_doctrine,
        "seat_occupancy": seat_occupancy,
        "ready_for_promotion": len(blockers) == 0 and samples_with_outcome >= 100,
        "promotion_blockers": blockers,
        "scoring_axis_doctrine": (
            "Performance is keyed on (lane, seat, doctrine_version). "
            "Holders are metadata only. A seat's doctrine, not a brain's "
            "reputation, is what graduates or retires."
        ),
        "scorecard_version": "scorecard_v2_seat_doctrinal",
    }


# ─── seat-occupancy endpoint (metadata; not a scoring axis) ──────────

@router.get("/seat-occupancy")
async def seat_occupancy_view(
    lane: Optional[Literal["equity", "crypto"]] = Query(default=None),
    seat: Optional[Literal[
        "strategist", "adversary", "governor", "execution_judge",
    ]] = Query(default=None),
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    """Show WHICH brains held which seats during the measured window.

    Strictly informational. The scorecard's primary scoring axis is
    (lane, seat, doctrine_version) — this endpoint exists so an
    operator can answer "ok, who was sitting in equity/governor while
    those numbers were collected?" without that being the scoring key.
    """
    q: dict = {"outcome_join": {"$exists": True}}
    if lane:
        q["lane"] = lane
    rows = await db[DOCTRINE_SIDECARS].find(q, {"_id": 0}).to_list(50_000)
    return {
        "lane": lane,
        "seat": seat,
        "occupancy": _seat_occupancy_metadata(rows, only_seat=seat),
    }


# ─── helpers ─────────────────────────────────────────────────────────

def _aggregate_by_seat(rows: list) -> dict:
    gov_block_samples = gov_block_losses = 0
    gov_modulate_samples = gov_modulate_losses = 0
    adv_chal_samples = adv_chal_losses = 0
    adv_quiet_samples = adv_quiet_losses = 0
    judge_ready_samples = judge_ready_losses = 0
    judge_not_ready_samples = judge_not_ready_losses = 0

    for r in rows:
        oj = r.get("outcome_join") or {}
        label = (oj.get("outcome_label") or "").lower()
        loss = _is_loss(label)

        ga = _governor_action(r)
        if ga == "block":
            gov_block_samples += 1
            if loss:
                gov_block_losses += 1
        elif ga == "modulate":
            gov_modulate_samples += 1
            if loss:
                gov_modulate_losses += 1

        ac = _adversary_challenge_required(r)
        if ac is True:
            adv_chal_samples += 1
            if loss:
                adv_chal_losses += 1
        elif ac is False:
            adv_quiet_samples += 1
            if loss:
                adv_quiet_losses += 1

        jr = _execution_judge_ready(r)
        if jr is True:
            judge_ready_samples += 1
            if loss:
                judge_ready_losses += 1
        elif jr is False:
            judge_not_ready_samples += 1
            if loss:
                judge_not_ready_losses += 1

    def rate(loss, total):
        return round(loss / total, 4) if total > 0 else None

    return {
        "governor": {
            "block": {
                "samples_with_outcome": gov_block_samples,
                "losses": gov_block_losses,
                "loss_rate": rate(gov_block_losses, gov_block_samples),
            },
            "modulate": {
                "samples_with_outcome": gov_modulate_samples,
                "losses": gov_modulate_losses,
                "loss_rate": rate(gov_modulate_losses, gov_modulate_samples),
            },
        },
        "adversary": {
            "challenge_required": {
                "samples_with_outcome": adv_chal_samples,
                "losses": adv_chal_losses,
                "loss_rate": rate(adv_chal_losses, adv_chal_samples),
            },
            "quiet": {
                "samples_with_outcome": adv_quiet_samples,
                "losses": adv_quiet_losses,
                "loss_rate": rate(adv_quiet_losses, adv_quiet_samples),
            },
        },
        "execution_judge": {
            "ready": {
                "samples_with_outcome": judge_ready_samples,
                "losses": judge_ready_losses,
                "loss_rate": rate(judge_ready_losses, judge_ready_samples),
            },
            "not_ready": {
                "samples_with_outcome": judge_not_ready_samples,
                "losses": judge_not_ready_losses,
                "loss_rate": rate(judge_not_ready_losses, judge_not_ready_samples),
            },
        },
    }


def _aggregate_by_lane_seat_doctrine(rows: list) -> dict:
    """Build the canonical (lane, seat, doctrine_version) → metrics map.

    Each slice gets:
        - samples_with_outcome
        - win_rate
        - loss_rate
        - by_quality.{A,B,C,REJECT}.samples + wins + losses
        - branch metrics: governor.{block,modulate}, adversary.{chal,quiet},
          execution_judge.{ready,not_ready} (whichever applies to the seat)
    """
    out: dict = defaultdict(lambda: {
        "samples_with_outcome": 0, "wins": 0, "losses": 0,
        "by_quality": defaultdict(lambda: {"samples": 0, "wins": 0, "losses": 0}),
        "branches": defaultdict(lambda: {
            "samples_with_outcome": 0, "losses": 0,
        }),
    })

    seats = ("strategist", "adversary", "governor", "execution_judge")

    for r in rows:
        lane = r.get("lane") or "unknown"
        dv = r.get("doctrine_version") or "unknown"
        quality = r.get("quality") or "unknown"
        oj = r.get("outcome_join") or {}
        label = (oj.get("outcome_label") or "").lower()
        loss = _is_loss(label)
        win = _is_win(label)

        ga = _governor_action(r)
        ac = _adversary_challenge_required(r)
        jr = _execution_judge_ready(r)

        for seat in seats:
            key = f"{lane}/{seat}/{dv}"
            bucket = out[key]
            bucket["samples_with_outcome"] += 1
            if win:
                bucket["wins"] += 1
            if loss:
                bucket["losses"] += 1
            qb = bucket["by_quality"][quality]
            qb["samples"] += 1
            if win:
                qb["wins"] += 1
            if loss:
                qb["losses"] += 1

            # branch metrics — only on the seat whose action it is
            if seat == "governor" and ga in ("block", "modulate"):
                bb = bucket["branches"][ga]
                bb["samples_with_outcome"] += 1
                if loss:
                    bb["losses"] += 1
            elif seat == "adversary" and isinstance(ac, bool):
                branch = "challenge_required" if ac else "quiet"
                bb = bucket["branches"][branch]
                bb["samples_with_outcome"] += 1
                if loss:
                    bb["losses"] += 1
            elif seat == "execution_judge" and isinstance(jr, bool):
                branch = "ready" if jr else "not_ready"
                bb = bucket["branches"][branch]
                bb["samples_with_outcome"] += 1
                if loss:
                    bb["losses"] += 1

    # finalize rates and convert defaultdicts to plain dicts
    final: dict = {}
    for key, b in out.items():
        n = b["samples_with_outcome"]
        decided = b["wins"] + b["losses"]
        by_q: dict = {}
        for q, qb in b["by_quality"].items():
            d = qb["wins"] + qb["losses"]
            by_q[q] = {
                "samples": qb["samples"],
                "wins": qb["wins"],
                "losses": qb["losses"],
                "win_rate": round(qb["wins"] / d, 4) if d > 0 else None,
            }
        branches: dict = {}
        for br, bb in b["branches"].items():
            bn = bb["samples_with_outcome"]
            branches[br] = {
                "samples_with_outcome": bn,
                "losses": bb["losses"],
                "loss_rate": round(bb["losses"] / bn, 4) if bn > 0 else None,
            }
        lane, seat, dv = key.split("/", 2)
        final[key] = {
            "lane": lane,
            "seat": seat,
            "doctrine_version": dv,
            "samples_with_outcome": n,
            "wins": b["wins"],
            "losses": b["losses"],
            "win_rate": round(b["wins"] / decided, 4) if decided > 0 else None,
            "loss_rate": round(b["losses"] / decided, 4) if decided > 0 else None,
            "by_quality": by_q,
            "branches": branches,
        }
    return final


def _seat_occupancy_metadata(rows: list, only_seat: Optional[str] = None) -> dict:
    """Per (lane, seat) → which holders occupied the seat during the
    sampled window, and how many samples each holder contributed.

    Strictly informational. Never a scoring axis.
    """
    out: dict = defaultdict(lambda: defaultdict(int))
    seat_field = {
        "strategist": "strategist_holder",
        "adversary": "adversary_holder",
        "governor": "governor_holder",
        "execution_judge": "execution_judge_holder",
    }
    seats = (only_seat,) if only_seat else tuple(seat_field)
    for r in rows:
        lane = r.get("lane") or "unknown"
        for seat in seats:
            holder = r.get(seat_field[seat]) or "vacant"
            out[f"{lane}/{seat}"][holder] += 1
    final: dict = {}
    for key, holders in out.items():
        lane, seat = key.split("/", 1)
        final[key] = {
            "lane": lane,
            "seat": seat,
            "holders": dict(holders),
            "total_samples": sum(holders.values()),
        }
    return final


def _promotion_blockers(quality_report: dict, by_seat: dict) -> list:
    blockers: list[str] = []
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
        blockers.append(
            f"governor seat: block loss_rate ({gov_block['loss_rate']}) "
            f"≤ modulate loss_rate ({gov_modulate['loss_rate']}) — "
            f"block heuristic not catching losers"
        )

    adv_chal = by_seat["adversary"]["challenge_required"]
    adv_quiet = by_seat["adversary"]["quiet"]
    if (
        adv_chal["loss_rate"] is not None
        and adv_quiet["loss_rate"] is not None
        and adv_chal["loss_rate"] <= adv_quiet["loss_rate"]
    ):
        blockers.append(
            f"adversary seat: challenge loss_rate ({adv_chal['loss_rate']}) "
            f"≤ quiet loss_rate ({adv_quiet['loss_rate']}) — "
            f"objections don't correlate with losses"
        )

    j_ready = by_seat["execution_judge"]["ready"]
    j_not = by_seat["execution_judge"]["not_ready"]
    if (
        j_ready["loss_rate"] is not None
        and j_not["loss_rate"] is not None
        and j_ready["loss_rate"] >= j_not["loss_rate"]
    ):
        blockers.append(
            f"execution_judge seat: ready loss_rate ({j_ready['loss_rate']}) "
            f"≥ not_ready loss_rate ({j_not['loss_rate']}) — "
            f"ready signal not useful"
        )

    return blockers

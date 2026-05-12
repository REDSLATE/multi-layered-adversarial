"""Public /signals — sanitized position primitive view.

Returns BOTH framings the risedual.ai UI expects, derived from MC's
single seat-policy doctrine:

  * Adversarial block (Bull / Bear / Commander) — maps to
    decider / opponent / executor seats. Bull-case argument vs
    Bear-case argument vs the executor's actual call. This is the
    "AI War Room" framing.

  * Governance block (Strategist / Auditor / Synthesized Signal) —
    maps to decider / governor / executor seats' aggregate output.
    This is the "Adversarial Pipeline V5.2" framing the screenshot
    showed.

Both come from the SAME position document — no risk of disagreement
between the two views.

What we hide from the public:
  * Memory provenance (memory_sources, confidence_origin) — operator-only.
  * Quorum blindness flags — they read as alarming to non-operators.
  * seat_epoch + raw audit log.
  * The brain codenames (alpha/camaro/chevelle/redeye) — replaced with
    generic seat labels so the architecture isn't leaked to free users
    unnecessarily.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from db import db
from namespaces import (
    SHARED_POSITIONS,
    SHARED_POSITION_STANCES,
)
from shared.positions import (
    OPEN_STATES,
    STATE_CONSENSUS_LONG,
    STATE_CONSENSUS_SHORT,
)

from .auth import PublicCaller, public_trust_required


router = APIRouter(tags=["public"])


# Seat → public-facing role label. We deliberately do not leak the
# internal seat names verbatim ("opponent" sounds combative; "governor"
# sounds bureaucratic). The labels map to roles a customer would
# recognize from the risedual.ai screenshots.
SEAT_LABEL = {
    "decider": "Strategist",
    "executor": "Commander",
    "governor": "Auditor",
    "advisor": "Advisor",
    "opponent": "Bear Case",
}


def _direction_from_state(state: str, direction: Optional[str]) -> str:
    if state == STATE_CONSENSUS_LONG:
        return "LONG"
    if state == STATE_CONSENSUS_SHORT:
        return "SHORT"
    if direction == "long":
        return "LONG"
    if direction == "short":
        return "SHORT"
    return "HOLD"


def _adversarial_view(stances_by_seat: dict) -> dict:
    """Map seat-stances into Bull / Bear / Commander."""
    bull = stances_by_seat.get("decider") or stances_by_seat.get("advisor")
    bear = stances_by_seat.get("opponent")
    cmd = stances_by_seat.get("executor")

    def _shape(s: dict | None, seat_label: str) -> dict | None:
        if not s:
            return None
        return {
            "label": seat_label,
            "stance": s["stance"].upper(),
            "confidence": round(float(s.get("confidence", 0.0)) * 100),
            "notes": (s.get("notes") or "")[:280],
        }

    return {
        "bull": _shape(bull, "Bull Case"),
        "bear": _shape(bear, "Bear Case"),
        "commander": _shape(cmd, "Commander"),
    }


def _governance_view(stances_by_seat: dict, doc: dict) -> dict:
    """Map seat-stances into Strategist / Auditor / Synthesized Signal."""
    strategist = stances_by_seat.get("decider")
    auditor = stances_by_seat.get("governor")
    executor = stances_by_seat.get("executor")

    auditor_mode = "NO_THREAT_DETECTED"
    auditor_action = "PASS"
    if auditor:
        if auditor["stance"] == "abstain":
            auditor_mode = "NO_THREAT_DETECTED"
            auditor_action = "PASS"
        elif auditor["stance"] in ("long", "short"):
            # Governor expressing a directional stance against the
            # decider = veto signal in the public framing.
            decider_dir = strategist["stance"] if strategist else None
            if decider_dir and decider_dir != "abstain" and decider_dir != auditor["stance"]:
                auditor_mode = "DISSENT_AGAINST_STRATEGIST"
                auditor_action = "VETO"
            else:
                auditor_mode = "ALIGNED"
                auditor_action = "PASS"
    else:
        auditor_mode = "AWAITING_REVIEW"
        auditor_action = "PASS"  # not yet vetoed

    # Synthesized signal = executor's call (or aggregate intent).
    if executor:
        synth_direction = executor["stance"].upper()
        synth_conf = round(float(executor.get("confidence", 0.0)) * 100)
    elif doc.get("state") in (STATE_CONSENSUS_LONG, STATE_CONSENSUS_SHORT):
        synth_direction = "LONG" if doc["state"] == STATE_CONSENSUS_LONG else "SHORT"
        synth_conf = 60
    else:
        synth_direction = "HOLD"
        synth_conf = 50

    return {
        "strategist": (
            {
                "label": "STRATEGIST_AGENT",
                "proposal": f"PROPOSE {strategist['stance'].upper()}",
                "confidence": round(float(strategist.get("confidence", 0.0)) * 100),
                "detected": (strategist.get("notes") or "")[:200] or "SENTIMENT_READ",
            }
            if strategist
            else {
                "label": "STRATEGIST_AGENT",
                "proposal": "PROPOSE HOLD",
                "confidence": 50,
                "detected": "AWAITING_PROPOSAL",
            }
        ),
        "auditor": {
            "label": "RISK_AUDITOR_AGENT",
            "action": auditor_action,
            "mode": auditor_mode,
            "confidence": (
                round(float(auditor.get("confidence", 0.0)) * 100) if auditor else 0
            ),
        },
        "synthesized": {
            "symbol": doc["symbol"],
            "direction": synth_direction,
            "confidence": synth_conf,
            "label": "SYNTHESIZED SIGNAL",
        },
    }


async def _hydrate_stances(position_id: str) -> dict[str, dict]:
    """Latest stance per seat (the seat the brain held at write time)."""
    rows = await db[SHARED_POSITION_STANCES].find(
        {"position_id": position_id}, {"_id": 0},
    ).sort("posted_at", 1).to_list(64)
    by_seat: dict[str, dict] = {}
    for r in rows:
        seat = r.get("posted_as")
        if seat:
            by_seat[seat] = r  # latest stance per seat wins
    return by_seat


def _stance_counts(by_seat: dict[str, dict]) -> dict:
    counts = {"buy": 0, "sell": 0, "hold": 0}
    for s in by_seat.values():
        if s["stance"] == "long":
            counts["buy"] += 1
        elif s["stance"] == "short":
            counts["sell"] += 1
        else:
            counts["hold"] += 1
    n = max(1, sum(counts.values()))
    return {
        "buy_pct": round(counts["buy"] / n * 100),
        "sell_pct": round(counts["sell"] / n * 100),
        "hold_pct": round(counts["hold"] / n * 100),
        "n": sum(counts.values()),
    }


def _consensus_label(counts: dict) -> str:
    if counts["buy_pct"] >= 60:
        return "BULLISH"
    if counts["sell_pct"] >= 60:
        return "BEARISH"
    if counts["hold_pct"] >= 60:
        return "NEUTRAL"
    return "MIXED"


async def _signal_card(doc: dict) -> dict:
    """Compact card payload, matches risedual.ai "Active Signals" tile."""
    by_seat = await _hydrate_stances(doc["position_id"])
    counts = _stance_counts(by_seat)
    direction = _direction_from_state(doc["state"], doc.get("direction"))

    flagged = False
    auditor = by_seat.get("governor")
    if auditor and auditor["stance"] in ("long", "short"):
        decider = by_seat.get("decider")
        if decider and decider["stance"] != "abstain" and decider["stance"] != auditor["stance"]:
            flagged = True

    return {
        "signal_id": doc["position_id"],
        "symbol": doc["symbol"],
        "direction": direction,
        "state": doc["state"],
        "flagged_by_auditor": flagged,
        "consensus": _consensus_label(counts),
        "consensus_breakdown": counts,
        "thesis": (doc.get("thesis") or "")[:280],
        "updated_at": doc["updated_at"],
        "created_at": doc["created_at"],
    }


@router.get("/public/signals")
async def list_public_signals(
    limit: int = Query(20, ge=1, le=100),
    caller: PublicCaller = Depends(public_trust_required),
):
    """Active signals = open positions, most recently touched first."""
    rows = await db[SHARED_POSITIONS].find(
        {"state": {"$in": list(OPEN_STATES)}}, {"_id": 0},
    ).sort("updated_at", -1).to_list(limit)
    items = [await _signal_card(r) for r in rows]

    # Aggregate consensus across all open signals — drives the
    # "AI Consensus NEUTRAL · 56/0/44" hero panel on the dashboard.
    agg = {"buy": 0, "sell": 0, "hold": 0}
    for it in items:
        c = it["consensus_breakdown"]
        agg["buy"] += c["buy_pct"]
        agg["sell"] += c["sell_pct"]
        agg["hold"] += c["hold_pct"]
    n = max(1, len(items))
    consensus = {
        "buy_pct": round(agg["buy"] / n),
        "sell_pct": round(agg["sell"] / n),
        "hold_pct": round(agg["hold"] / n),
    }
    return {
        "items": items,
        "count": len(items),
        "active_signals": len(items),
        "consensus": {
            **consensus,
            "label": _consensus_label({
                "buy_pct": consensus["buy_pct"],
                "sell_pct": consensus["sell_pct"],
                "hold_pct": consensus["hold_pct"],
            }),
        },
        "caller": caller.as_dict(),
    }


@router.get("/public/signals/{signal_id}")
async def get_public_signal(
    signal_id: str,
    caller: PublicCaller = Depends(public_trust_required),
):
    """Single-signal detail. Returns BOTH framings:
      * adversarial: bull / bear / commander
      * governance: strategist / auditor / synthesized

    risedual.ai's frontend renders whichever block(s) it has UI for.
    """
    doc = await db[SHARED_POSITIONS].find_one(
        {"position_id": signal_id}, {"_id": 0},
    )
    if not doc:
        raise HTTPException(status_code=404, detail="signal not found")
    by_seat = await _hydrate_stances(signal_id)
    counts = _stance_counts(by_seat)
    card = await _signal_card(doc)
    return {
        **card,
        "adversarial": _adversarial_view(by_seat),
        "governance": _governance_view(by_seat, doc),
        "consensus_breakdown": counts,
        "caller": caller.as_dict(),
    }

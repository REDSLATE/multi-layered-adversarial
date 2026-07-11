"""MC Seat Arbiter — the arbitration loop.

Collect opinions → compute adjusted_rank via DAWE → pick winner →
compute disagreement + size → emit ONE intent (LIVE only) → record
the decision on the seat doc.

Design freeze: `/app/memory/MC_SEAT_ARBITER.md` §5.

I/O boundaries here:
    * `mc_seats` collection — one doc per seat_key, holds opinions
      + winner + intent_id + arbitrated_at.
    * `brain_runtime_metrics.risedual_stack.brains.<brain>.dawe.<lane>`
      — DAWE state read (see `dawe_store.load` / `save`).
    * `shared.intents._post_intent_impl` — the ONE door to the
      trader. Arbiter emits by constructing an `IntentIn` and
      calling that existing function. Same idempotency, same
      3-clock stamps, same counterfactual signals path.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from db import db
from mc_arbiter.dawe import (
    compute_effective_weight,
    size_multiplier as dawe_size_multiplier,
)
from mc_arbiter.models import (
    DaweState,
    Direction,
    ModelOpinion,
    RuntimeMode,
)
from mc_arbiter.seat_key import parse_seat_key

logger = logging.getLogger("mc_arbiter")

# ── Collections ──────────────────────────────────────────────────
MC_SEATS = "mc_seats"
BRM = "brain_runtime_metrics"
STACK_ID = "risedual_stack"

# ── Config knobs (design freeze §5) ─────────────────────────────
SIZE_MULT_FLOOR = 0.30
SIZE_MULT_CEIL = 2.00
DISAGREEMENT_MIN = 0.55
DISAGREEMENT_MAX = 1.00
DISAGREEMENT_STRENGTH_COEFF = 0.40
KERNEL_MULTIPLIER_DEFAULT = 1.0   # v3's LearningEngine wire-up: Phase 2


# ── DAWE state persistence ───────────────────────────────────────

async def load_dawe(brain: str, lane: str) -> DaweState:
    """Read the DAWE subdoc for (brain, lane). Missing → neutral
    defaults (cold-start)."""
    doc = await db[BRM].find_one(
        {"_id": STACK_ID},
        {f"brains.{brain}.dawe.{lane}": 1},
    )
    subdoc = (
        (((doc or {}).get("brains") or {}).get(brain) or {}).get("dawe", {}).get(lane)
    )
    return DaweState.from_mongo(brain=brain, lane=lane, doc=subdoc)


async def save_dawe(state: DaweState) -> None:
    """Persist DAWE state back to the stack doc. Merges only the
    (brain, lane) subdoc — never overwrites siblings."""
    state.last_updated = _now_iso()
    await db[BRM].update_one(
        {"_id": STACK_ID},
        {
            "$set": {
                f"brains.{state.brain}.dawe.{state.lane}": state.to_mongo(),
                "updated_at": _now_iso(),
            },
            "$setOnInsert": {"_id": STACK_ID, "first_seen_at": _now_iso()},
        },
        upsert=True,
    )


# ── Opinion intake ───────────────────────────────────────────────

async def submit_opinion(opinion: ModelOpinion) -> dict:
    """Store a brain's opinion for a seat. Idempotent per
    (seat_key, brain) — a re-submit within the bucket updates the
    existing row in place.

    Returns a compact receipt: seat_key, brain, direction,
    rank_score, ts_recorded.
    """
    now = _now_iso()
    lane, symbol, bucket = parse_seat_key(opinion.seat_key)
    payload = {
        "brain": opinion.brain,
        "seat_key": opinion.seat_key,
        "lane": lane,
        "symbol": symbol,
        "bucket_iso": bucket,
        "direction": opinion.direction.value,
        "edge": opinion.edge,
        "confidence": opinion.confidence,
        "regime_fit": opinion.regime_fit,
        "urgency": opinion.urgency,
        "rank_score": opinion.rank_score,
        "price_at_signal": opinion.price_at_signal,
        "entry_hint": opinion.entry_hint,
        "stop_hint": opinion.stop_hint,
        "rationale": opinion.rationale,
        "ts": opinion.ts,
        "recorded_at": now,
        # Grader will fill these in later:
        "grade_15m": None,
        "grade_60m": None,
    }
    await db[MC_SEATS].update_one(
        {"seat_key": opinion.seat_key, "brain": opinion.brain},
        {"$set": payload, "$setOnInsert": {"first_recorded_at": now}},
        upsert=True,
    )
    return {
        "ok": True,
        "seat_key": opinion.seat_key,
        "brain": opinion.brain,
        "direction": opinion.direction.value,
        "rank_score": opinion.rank_score,
        "recorded_at": now,
    }


# ── Arbitration ──────────────────────────────────────────────────

async def arbitrate(seat_key: str, runtime_mode: RuntimeMode) -> dict:
    """Resolve a seat. Returns a decision summary regardless of
    runtime mode. In LIVE, ALSO submits ONE intent via
    `shared.intents._post_intent_impl`. In DISARMED, records the
    intended decision but does not emit.

    Doctrine (design freeze §5):
        * All directional opinions ranked by `rank × effective_weight`.
        * Winner = argmax among LONG/SHORT (FLAT never wins).
        * Disagreement multiplier from opposition strength.
        * Size = kernel × disagreement × sqrt(effective_weight),
          clamped [0.30, 2.00].
        * Only mechanical invalidity (via intent path) can still
          block emission.
    """
    lane, symbol, bucket = parse_seat_key(seat_key)
    opinions_docs = await (
        db[MC_SEATS]
        .find({"seat_key": seat_key}, {"_id": 0})
        .max_time_ms(2500)
        .to_list(50)
    )
    if not opinions_docs:
        return _no_decision(seat_key, reason="no_opinions", runtime_mode=runtime_mode)

    # Load DAWE state for every brain that opined (deduped).
    unique_brains = sorted({d["brain"] for d in opinions_docs})
    dawe_by_brain: dict[str, DaweState] = {}
    effective_by_brain: dict[str, float] = {}
    for brain in unique_brains:
        state = await load_dawe(brain, lane)
        dawe_by_brain[brain] = state
        effective_by_brain[brain] = compute_effective_weight(state)

    # Rank every opinion — FLATs get ranked too (for grading) but
    # are excluded from the winner pool below.
    ranked = []
    for op in opinions_docs:
        eff = effective_by_brain[op["brain"]]
        adjusted = float(op["rank_score"]) * eff
        ranked.append({
            "brain": op["brain"],
            "direction": op["direction"],
            "rank_score": op["rank_score"],
            "effective_weight": eff,
            "adjusted_rank": adjusted,
            "opinion": op,
        })

    directional = [r for r in ranked if r["direction"] in {"LONG", "SHORT"}]
    if not directional:
        return _no_decision(
            seat_key, reason="all_flat", runtime_mode=runtime_mode,
            ranked=ranked,
        )

    winner = max(directional, key=lambda r: r["adjusted_rank"])
    winning_dir = winner["direction"]

    # Disagreement: strongest OPPOSING adjusted_rank.
    opposition = [
        r for r in directional
        if r["direction"] != winning_dir
    ]
    opposition_strength = (
        max((r["adjusted_rank"] for r in opposition), default=0.0)
    )
    disagreement_mult = max(
        DISAGREEMENT_MIN,
        min(
            DISAGREEMENT_MAX,
            1.0 - opposition_strength * DISAGREEMENT_STRENGTH_COEFF,
        ),
    )

    # Size stack: kernel × disagreement × sqrt(effective). Final
    # clamp [0.30, 2.00] is the sanity gate — the multiplicative
    # cascade CANNOT nudge size beyond these bounds even if a
    # future kernel multiplier goes wild.
    size_mult_raw = (
        KERNEL_MULTIPLIER_DEFAULT
        * disagreement_mult
        * dawe_size_multiplier(winner["effective_weight"])
    )
    size_mult = max(SIZE_MULT_FLOOR, min(SIZE_MULT_CEIL, size_mult_raw))

    decision = {
        "seat_key": seat_key,
        "lane": lane,
        "symbol": symbol,
        "bucket_iso": bucket,
        "winner_brain": winner["brain"],
        "winner_direction": winning_dir,
        "winner_rank_score": winner["rank_score"],
        "winner_effective_weight": winner["effective_weight"],
        "winner_adjusted_rank": winner["adjusted_rank"],
        "opposition_strength": opposition_strength,
        "disagreement_multiplier": disagreement_mult,
        "size_multiplier_raw": size_mult_raw,
        "size_multiplier": size_mult,
        "kernel_multiplier": KERNEL_MULTIPLIER_DEFAULT,
        "runtime_mode": runtime_mode.value,
        "arbitrated_at": _now_iso(),
        "field": [
            {k: r[k] for k in
             ("brain", "direction", "rank_score",
              "effective_weight", "adjusted_rank")}
            for r in ranked
        ],
    }

    # Emit (LIVE only). Failures don't nuke the decision record —
    # we log the emit outcome onto the seat doc, honest either way.
    intent_id: Optional[str] = None
    emit_error: Optional[str] = None
    if runtime_mode == RuntimeMode.LIVE:
        try:
            intent_id = await _emit_intent(decision, winner["opinion"])
        except Exception as exc:  # noqa: BLE001
            emit_error = f"{type(exc).__name__}: {exc}"
            logger.exception(
                "arbiter emit failed seat=%s winner=%s",
                seat_key, winner["brain"],
            )

    decision["intent_id"] = intent_id
    decision["emit_error"] = emit_error

    # Persist the decision on the seat's row shared by all brains.
    # We stamp it on the winner's opinion row so a compound index
    # (seat_key, brain) find still surfaces the winner cheaply.
    await db[MC_SEATS].update_one(
        {"seat_key": seat_key, "brain": winner["brain"]},
        {"$set": {"decision": decision}},
        upsert=False,
    )
    return decision


async def _emit_intent(decision: dict, winner_opinion: dict) -> Optional[str]:
    """Construct + submit an IntentIn on behalf of the winning
    brain. Reuses the existing intents write path — same
    idempotency, same 3-clock stamps, same counterfactual signals.

    Returns the intent_id on success. Propagates exceptions on
    failure — arbitrate() catches + records.
    """
    from shared.intents import IntentIn, _post_intent_impl  # noqa: WPS433

    direction = decision["winner_direction"]
    # Map DAWE direction → intent action. FLAT never wins so we
    # only handle LONG/SHORT here.
    action_map = {"LONG": "BUY", "SHORT": "SELL"}
    action = action_map[direction]

    body = IntentIn(
        stack=decision["winner_brain"],
        action=action,
        symbol=decision["symbol"],
        lane=decision["lane"],
        confidence=float(winner_opinion.get("confidence", 0.0)),
        rationale=(
            f"mc_arbiter winner · adj_rank={decision['winner_adjusted_rank']:.3f}"
            f" · size_mult={decision['size_multiplier']:.2f}"
            f" · disagreement={decision['disagreement_multiplier']:.2f}"
        ),
        evidence={
            "arbitrated_by": "mc_arbiter",
            "seat_key": decision["seat_key"],
            "effective_weight": decision["winner_effective_weight"],
            "size_multiplier": decision["size_multiplier"],
            "field": decision["field"],
        },
    )
    result = await _post_intent_impl(body)
    return (result or {}).get("intent_id")


# ── Runtime mode (DISARMED ↔ LIVE) ───────────────────────────────

async def get_runtime_mode() -> RuntimeMode:
    """Read the operator's arm/disarm setting. Default DISARMED —
    the safe posture when the doc is missing or malformed."""
    doc = await db[BRM].find_one(
        {"_id": STACK_ID}, {"arbiter.runtime_mode": 1},
    )
    raw = (((doc or {}).get("arbiter") or {}).get("runtime_mode") or "DISARMED")
    try:
        return RuntimeMode(raw)
    except ValueError:
        return RuntimeMode.DISARMED


async def set_runtime_mode(mode: RuntimeMode, actor: str) -> dict:
    """Flip the arm state. `actor` is stamped on the audit trail so
    an operator flip is always attributable."""
    now = _now_iso()
    await db[BRM].update_one(
        {"_id": STACK_ID},
        {
            "$set": {
                "arbiter.runtime_mode": mode.value,
                "arbiter.last_change_ts": now,
                "arbiter.last_change_by": actor,
                "updated_at": now,
            },
            "$setOnInsert": {"_id": STACK_ID, "first_seen_at": now},
        },
        upsert=True,
    )
    logger.warning("mc_arbiter runtime mode set to %s by %s", mode.value, actor)
    return {"ok": True, "runtime_mode": mode.value, "actor": actor, "ts": now}


# ── helpers ──────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _no_decision(
    seat_key: str,
    reason: str,
    runtime_mode: RuntimeMode,
    ranked: Optional[list] = None,
) -> dict:
    return {
        "seat_key": seat_key,
        "winner_brain": None,
        "winner_direction": None,
        "reason": reason,
        "runtime_mode": runtime_mode.value,
        "arbitrated_at": _now_iso(),
        "field": [
            {k: r[k] for k in
             ("brain", "direction", "rank_score",
              "effective_weight", "adjusted_rank")}
            for r in (ranked or [])
        ],
        "intent_id": None,
    }

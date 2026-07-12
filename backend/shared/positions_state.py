"""Position state-machine helpers — extracted from `shared/positions.py`.

2026-02-11 (P6a-finish): moved the three biggest helpers out of
`positions.py` so that file stops being a mixed router+state module.

Functions live here:
    - `_stance_doc` : pure assembler for the stance BSON document.
    - `_current_seat_and_epoch` : roster lookup with graceful fallback.
    - `_maybe_auto_advance` : the auto-consensus gate (executor-seat +
      auto call_mode + directional stance → advance state, with the
      full 2026-07-12 doctrine: no silent returns, v2 fingerprint,
      fresh-input gate, dedup via unique index).
    - `_persist_stance` : the shared insert-stance + hydrate flow used
      by both the operator and runtime stance endpoints.

Cross-module deps:
    `_persist_stance` still needs `_advance_state_if_needed` and
    `_hydrate` from `shared.positions`. Those are imported lazily
    inside the function to avoid the natural circular import
    (positions ↔ positions_state). No behavior change from the
    pre-extraction code path.
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime
from typing import Optional

from fastapi import HTTPException

from db import db
from namespaces import (
    SHARED_POSITION_AUDIT,
    SHARED_POSITION_STANCES,
    SHARED_POSITIONS,
)
from shared.roster import get_roster
from shared.seat_policy import snapshot as seat_snapshot


# Re-export the state constants (imported from positions.py by callers,
# but positions_state.py needs its own view for the auto-advance gate).
from shared.positions_models import (
    CALL_MODE_AUTO,
)


STATE_PROPOSED = "proposed"
STATE_DISCUSSING = "discussing"
STATE_CONSENSUS_LONG = "consensus_long"
STATE_CONSENSUS_SHORT = "consensus_short"
STATE_REJECTED = "rejected"
STATE_STALE = "stale"

OPEN_STATES = frozenset({STATE_PROPOSED, STATE_DISCUSSING})

STANCE_LONG = "long"
STANCE_SHORT = "short"
STANCE_ABSTAIN = "abstain"
VALID_STANCES = frozenset({STANCE_LONG, STANCE_SHORT, STANCE_ABSTAIN})


# ── 2026-07-12 doctrine step 5.b: fresh-input tolerance ──
import os as _os  # noqa: E402
CONSENSUS_FRESH_INPUT_TOLERANCE_SEC = int(
    _os.environ.get("CONSENSUS_FRESH_INPUT_TOLERANCE_SEC", "900")
)


def _now_iso_local() -> str:
    from datetime import timezone
    return datetime.now(timezone.utc).isoformat()


async def _audit(action: str, actor: str, position_id: str, payload: dict) -> None:
    await db[SHARED_POSITION_AUDIT].insert_one({
        "ts": _now_iso_local(),
        "action": action,
        "actor": actor,
        "position_id": position_id,
        "payload": payload,
    })


def _stance_doc(
    *, position_id: str, brain: str, stance: str, confidence: float,
    notes: str, seat: Optional[str], seat_epoch: Optional[int],
    policy: dict, posted_via: str, actor: str, now_iso: str,
    memory_sources: list[str], confidence_origin: dict[str, float],
    source_bar_close_at: Optional[str] = None,
) -> dict:
    """Assemble the stance document with full seat-policy snapshot
    AND memory-provenance fields."""
    return {
        "stance_id": str(uuid.uuid4()),
        "position_id": position_id,
        "brain": brain,
        "stance": stance,
        "confidence": float(confidence),
        "notes": notes,
        # Seat policy snapshot — this is the authority record. If the
        # brain later changes seats, this row STILL reflects what the
        # rules were at write time.
        "posted_as": policy["posted_as"],
        "seat_epoch": seat_epoch,
        "may_decide": policy["may_decide"],
        "may_execute": policy["may_execute"],
        # `may_override` removed from doctrine on 2026-02-19 — see
        # `shared/seat_policy.py` for the 4-seat merge rationale.
        "may_veto": policy["may_veto"],
        # Memory provenance — opt-in by the brain sidecar. Empty arrays
        # are perfectly valid and indicate the brain doesn't (yet)
        # report provenance. Future "memory poisoning" audits will join
        # on these fields.
        "memory_sources": list(memory_sources),
        "confidence_origin": dict(confidence_origin),
        # ── 2026-07-12 doctrine step 5.b: fresh-input provenance ──
        # `source_bar_close_at` is the ISO-8601 close ts of the bar
        # the brain evaluated. None = brain hasn't been retrofitted;
        # consensus writer stays on v1 fingerprint for this position.
        # All-non-None across engaged brains = v2 path unlocks.
        "source_bar_close_at": source_bar_close_at,
        "posted_via": posted_via,
        "posted_at": now_iso,
        "actor": actor,
    }


async def _current_seat_and_epoch(brain: str) -> tuple[Optional[str], Optional[int]]:
    """Resolve which seat the brain currently holds + the live seat_epoch.
    Best-effort: roster lookup failures resolve to (None, None) so callers
    can still ingest with the safest-default policy snapshot."""
    try:
        roster = await get_roster()
    except Exception:  # noqa: BLE001
        return None, None
    seat_epoch = roster.get("seat_epoch")
    for role, occupant in roster["assignments"].items():
        if occupant == brain:
            return role, seat_epoch
    return None, seat_epoch


async def _maybe_auto_advance(
    *, position_id: str, brain: str, stance: str, policy: dict,
    seat_epoch: Optional[int], now_iso: str,
) -> None:
    """If the position is in auto call_mode AND the brain holds the
    executor seat AND the stance is long/short, advance position state.
    Logs `executor_call_auto` so it's distinguishable from operator calls.

    Doctrine 2026-07-12 (Step 7): no silent returns. Every blocked
    branch persists a `consensus_transition_skipped` audit row with a
    machine-readable `reason_code` so the operator can see WHY a stance
    did not advance state, rather than the previous behavior where six
    bare `return`s silently dropped the transition.
    """
    # Lazy import to avoid the positions ↔ positions_state cycle.
    from shared.positions import _stance_summary  # noqa: WPS433

    doc = await db[SHARED_POSITIONS].find_one(
        {"position_id": position_id}, {"_id": 0},
    )
    if not doc:
        # Nothing to audit against — position was deleted between
        # stance write and this call. Genuinely nothing to log.
        return
    if doc.get("call_mode") != CALL_MODE_AUTO:
        await _audit(
            "consensus_transition_skipped", brain, position_id, {
                "reason_code": "CALL_MODE_NOT_AUTO",
                "call_mode": doc.get("call_mode"),
                "brain": brain, "stance": stance,
            },
        )
        return
    if doc["state"] not in OPEN_STATES:
        await _audit(
            "consensus_transition_skipped", brain, position_id, {
                "reason_code": "POSITION_NOT_OPEN",
                "state": doc["state"], "brain": brain, "stance": stance,
            },
        )
        return
    if not policy["may_execute"]:
        await _audit(
            "consensus_transition_skipped", brain, position_id, {
                "reason_code": "BRAIN_MAY_NOT_EXECUTE",
                "brain": brain, "stance": stance,
                "seat": policy.get("posted_as"),
            },
        )
        return
    # 2026-02-19 doctrine: the executor seat ships in TWO flavours —
    # equity `executor` (lane_scope=equity) and `crypto` (lane_scope=
    # crypto). A crypto-seated brain must NOT auto-advance an equity
    # position and vice versa. Pure seat-policy enforcement; the seat
    # owns the lane scope, the brain only fills the seat.
    seat = policy.get("posted_as")
    if seat:
        from shared.seat_policy import seat_may_execute_lane
        from shared.regime_keys import _looks_like_crypto
        position_lane = (
            "crypto" if _looks_like_crypto(doc.get("symbol") or "") else "equity"
        )
        if not seat_may_execute_lane(seat, position_lane):
            await _audit(
                "consensus_transition_skipped", brain, position_id, {
                    "reason_code": "SEAT_LANE_MISMATCH",
                    "brain": brain, "stance": stance,
                    "seat": seat, "position_lane": position_lane,
                },
            )
            return
    if stance not in (STANCE_LONG, STANCE_SHORT):
        await _audit(
            "consensus_transition_skipped", brain, position_id, {
                "reason_code": "STANCE_NOT_DIRECTIONAL",
                "brain": brain, "stance": stance,
            },
        )
        return

    new_state = (
        STATE_CONSENSUS_LONG if stance == STANCE_LONG
        else STATE_CONSENSUS_SHORT
    )
    # ── 2026-07-11 doctrine step 5: consensus dedup ──
    # `consensus_fingerprint` = sha256(symbol | direction |
    # engaged_brains [ | min(source_bar_close_at) for v2 ]).
    # Combined with the sparse unique index on
    # `consensus_fingerprint`, the same {4 brains, symbol, side}
    # combination can produce ONE consensus row across all
    # positions — not four different position_ids all landing
    # at consensus_long on NVDA within the same tick. Duplicate
    # write attempts silently no-op via DuplicateKeyError.
    #
    # ── 2026-07-12 doctrine step 5.b: v2 fingerprint + fresh-input gate ──
    _summary = await _stance_summary(position_id)
    _by_brain = (_summary or {}).get("stances_by_brain", {}) or {}
    _engaged = sorted(_by_brain.keys())
    _bar_closes = [
        (_by_brain.get(b) or {}).get("source_bar_close_at")
        for b in _engaged
    ]
    _all_have_bar_close = all(bc for bc in _bar_closes)

    if _all_have_bar_close and _bar_closes:
        # v2 path: freshness gate + hash includes min(bar_close).
        _min_bar_close = min(_bar_closes)
        _max_bar_close = max(_bar_closes)
        # Compute spread. Any parse failure → drop to v1 (safe).
        _spread_ok = True
        try:
            _dt_min = datetime.fromisoformat(_min_bar_close)
            _dt_max = datetime.fromisoformat(_max_bar_close)
            _spread_s = (_dt_max - _dt_min).total_seconds()
            if _spread_s > CONSENSUS_FRESH_INPUT_TOLERANCE_SEC:
                _spread_ok = False
        except Exception:  # noqa: BLE001
            _all_have_bar_close = False

        if _all_have_bar_close and not _spread_ok:
            await _audit(
                "consensus_rejected_stale_input", brain, position_id, {
                    "reason": "STALE_CONSENSUS_INPUT",
                    "min_bar_close_at": _min_bar_close,
                    "max_bar_close_at": _max_bar_close,
                    "spread_seconds": (
                        datetime.fromisoformat(_max_bar_close)
                        - datetime.fromisoformat(_min_bar_close)
                    ).total_seconds(),
                    "tolerance_seconds": CONSENSUS_FRESH_INPUT_TOLERANCE_SEC,
                    "engaged_brains": _engaged,
                    "would_have_transitioned_to": new_state,
                    "fingerprint_version": "v2",
                },
            )
            return

    if _all_have_bar_close and _bar_closes:
        _fp_input = (
            f"v2|{doc.get('symbol','')}|{stance}|{','.join(_engaged)}"
            f"|{min(_bar_closes)}"
        )
        _fp_version = "v2"
    else:
        _fp_input = (
            f"v1|{doc.get('symbol','')}|{stance}|{','.join(_engaged)}"
        )
        _fp_version = "v1"
    _consensus_fp = hashlib.sha256(_fp_input.encode("utf-8")).hexdigest()
    try:
        await db[SHARED_POSITIONS].update_one(
            {"position_id": position_id},
            {"$set": {
                "state": new_state,
                "direction": stance,
                "executor_call_by": brain,
                "executor_call_at": now_iso,
                "executor_call_notes": f"auto-advanced from executor seat ({brain})",
                "executor_call_recorded_by": "auto",
                "executor_call_seat_epoch": seat_epoch,
                "consensus_fingerprint": _consensus_fp,
                "consensus_fingerprint_version": _fp_version,
                "consensus_engaged_brains": _engaged,
                "consensus_min_bar_close_at": (
                    min(_bar_closes) if _all_have_bar_close and _bar_closes
                    else None
                ),
                "updated_at": now_iso,
            }},
        )
    except Exception as exc:  # noqa: BLE001
        from pymongo.errors import DuplicateKeyError
        if isinstance(exc, DuplicateKeyError):
            await _audit("consensus_dedup_skipped", brain, position_id, {
                "reason": "DUPLICATE_CONSENSUS_FINGERPRINT",
                "consensus_fingerprint": _consensus_fp,
                "engaged_brains": _engaged,
                "would_have_transitioned_to": new_state,
            })
            return
        raise
    await _audit("executor_call_auto", brain, position_id, {
        "executor": brain,
        "direction": stance,
        "before_state": doc["state"],
        "after_state": new_state,
        "trigger": "auto_mode_executor_stance",
        "seat_epoch": seat_epoch,
    })


async def _persist_stance(
    *, position_id: str, brain: str, stance: str,
    confidence: float, notes: str, posted_via: str, actor: str,
    memory_sources: list[str] | None = None,
    confidence_origin: dict[str, float] | None = None,
    source_bar_close_at: Optional[str] = None,
) -> dict:
    if stance not in VALID_STANCES:
        raise HTTPException(
            status_code=422,
            detail=f"stance must be one of {sorted(VALID_STANCES)}",
        )
    # Lazy imports to break the positions ↔ positions_state cycle.
    from shared.positions import _advance_state_if_needed, _hydrate  # noqa: WPS433

    now = _now_iso_local()
    seat, seat_epoch = await _current_seat_and_epoch(brain)
    policy = seat_snapshot(seat)

    await db[SHARED_POSITION_STANCES].insert_one(_stance_doc(
        position_id=position_id, brain=brain, stance=stance,
        confidence=confidence, notes=notes,
        seat=seat, seat_epoch=seat_epoch, policy=policy,
        posted_via=posted_via, actor=actor, now_iso=now,
        memory_sources=memory_sources or [],
        confidence_origin=confidence_origin or {},
        source_bar_close_at=source_bar_close_at,
    ))
    await db[SHARED_POSITIONS].update_one(
        {"position_id": position_id},
        {"$set": {"updated_at": now}},
    )
    await _advance_state_if_needed(position_id)
    await _audit("stance", actor, position_id, {
        "brain": brain, "stance": stance, "confidence": confidence,
        "posted_as": policy["posted_as"],
        "seat_epoch": seat_epoch,
        "may_execute": policy["may_execute"],
        "posted_via": posted_via,
    })
    await _maybe_auto_advance(
        position_id=position_id, brain=brain, stance=stance,
        policy=policy, seat_epoch=seat_epoch, now_iso=now,
    )

    doc = await db[SHARED_POSITIONS].find_one(
        {"position_id": position_id}, {"_id": 0},
    )
    return await _hydrate(doc)

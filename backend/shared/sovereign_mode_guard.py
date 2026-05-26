"""Sovereign sidecar mode guard + promotion bridge.

Doctrine (2026-05-17 rev):
    Each of the four brains can run as a deterministic sovereign sidecar
    (`runtime_patch_kit/sovereign/`) — local-state, replayable, isolated.
    The brain talks to Mission Control via two endpoints:

      1. POST /api/runtime-discussion/positions/{id}/stance
         (existing) — the brain's vote on an open position.

      2. POST /api/runtime-discussion/sovereign/contribution
         (this module) — periodic snapshot of the brain's internal
         state (weights, learning rate, recent outcomes, optional
         confidence delta).

    Two modes the brain may declare:

      * `DTD` — Deterministic Training Data. The brain is reading
        historical bars / labeled replay.
      * `PRD` — Production. The brain is reading live market data.

    **MC does NOT restrict brains.** Brains may declare any mode, any
    `training_signal`, and any `live_trading_enabled`. MC observes the
    declaration, persists it, and uses the seat-policy gate at execution
    time to decide what actually flows downstream. Restricting brain
    contributions at the API boundary is doctrine-anathema: it prevents
    brains from doing their job. MC is the regulator at the *execution*
    layer, not at the *opinion* layer.

    Confidence deltas are still hard-CLAMPED at ±0.25 (server-side, no
    error). The seat policy of whatever seat the brain currently holds
    is snapshotted on every contribution so the audit trail records
    "Camaro as Executor asked for a +0.18 confidence bump" not just
    "Camaro did something."
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from auth import get_current_user
from db import db
from namespaces import (
    DISCUSSION_PARTICIPANTS,
    SOVEREIGN_AUDIT_LOG,
    SOVEREIGN_CONTRIB_ATTEMPTS,
    SOVEREIGN_STATE,
    SOVEREIGN_STATE_HISTORY,
)
from runtime_auth import verify_runtime_token
from shared.roster import get_roster
from shared.seat_policy import snapshot as seat_snapshot


# ──────────────────────── doctrine constants ────────────────────────

MODE_DTD = "DTD"
MODE_PRD = "PRD"
VALID_MODES = frozenset({MODE_DTD, MODE_PRD})

# Confidence deltas are hard-capped. A brain that wants more than this
# in one step is misbehaving — likely a runaway training loop.
CONFIDENCE_DELTA_CAP = 0.25

# Pulled from the core; we re-assert here so the API is the single
# trust boundary. The brain core uses [-3, +3] and lr ≤ 0.5; we accept
# anything in those bounds.
WEIGHT_MAX_ABS = 3.0
LEARNING_RATE_MAX = 0.5

# Max items the brain may ship in a single contribution. Keeps payload
# bounded; brains rotate older outcomes into local state and only ship
# the recent tail.
MAX_FEATURES = 16
MAX_RECENT_OUTCOMES = 50


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _reject_empty_contributions_enabled() -> bool:
    """Doctrine pin (2026-05-24): empty contributions are silent waste —
    they validate, they persist, they pollute the audit log with rows
    that carry no learning signal. The dashboard surfaces them as
    "skeleton rows", but MC was never refusing them.

    This flag, default ON in preview, lets the operator ramp up
    enforcement in prod without a code change. Set
    `RISEDUAL_REJECT_EMPTY_CONTRIBUTIONS=false` in prod's `.env` until
    Alpha + Camaro + Chevelle + RedEye sidecars are confirmed sending
    substantive payloads. Then flip to true (the default)."""
    import os
    val = os.environ.get("RISEDUAL_REJECT_EMPTY_CONTRIBUTIONS", "true").strip().lower()
    return val in {"true", "1", "yes", "on"}


def _list_empty_fields(c: "SovereignContribution") -> list[str]:
    """Return the list of fields that look hollow on this contribution.
    A contribution is considered SUBSTANTIVE if AT LEAST ONE of:
      - notes is non-empty (after strip)
      - weights dict has any entries
      - recent_outcomes list has any entries
      - delta_reason is non-empty (after strip)
      - confidence_delta is non-zero

    We list every empty field so the brain author gets a precise
    error message back ("you sent nothing in any of these fields")
    not a vague "empty payload"."""
    empties: list[str] = []
    if not (c.notes or "").strip():
        empties.append("notes")
    if not c.weights:
        empties.append("weights")
    if not c.recent_outcomes:
        empties.append("recent_outcomes")
    if not (c.delta_reason or "").strip():
        empties.append("delta_reason")
    if c.confidence_delta == 0.0:
        empties.append("confidence_delta")
    return empties


async def _log_contribution_attempt(
    *,
    runtime: str,
    outcome: str,
    status_code: int,
    empty_fields: list[str],
    request_id: str | None,
    error_kind: str | None,
) -> None:
    """Append a row to `sovereign_contribution_attempts`.

    Best-effort — a Mongo failure here MUST NOT block the contribution
    endpoint (the brain's contract is with the endpoint, not the audit
    log). We swallow exceptions and let the next attempt try again.

    Outcome vocabulary (intentionally aligned with the brain teams'
    counter names — see RedEye telemetry summary 2026-05-24):
        pushed_200      → contribution accepted and persisted
        rejected_422    → empty_contribution gate fired
        rejected_4xx    → other 4xx (validation, auth) — currently unused
        error           → 5xx — reserved for future MC-side faults
    """
    try:
        await db[SOVEREIGN_CONTRIB_ATTEMPTS].insert_one({
            "ts": _now_iso(),
            "brain": runtime,
            "outcome": outcome,
            "status_code": status_code,
            "empty_fields": empty_fields,
            "request_id": request_id,
            "error_kind": error_kind,
        })
    except Exception:  # noqa: BLE001 — best-effort telemetry
        pass


# ──────────────────────── models ────────────────────────

class SovereignOutcome(BaseModel):
    """One resolved decision in the brain's recent history."""
    symbol: str = Field(..., min_length=1, max_length=32)
    action: Literal["BUY", "SELL", "HOLD"]
    confidence: float = Field(..., ge=0.0, le=1.0)
    # +1 win, -1 loss, 0 unresolved/flat
    outcome: Literal[-1, 0, 1]
    resolved_at: Optional[str] = Field(default=None, max_length=64)
    notional: float = Field(default=0.0, ge=0.0)


class SovereignContribution(BaseModel):
    """Periodic snapshot the brain sidecar POSTs to MC.

    Stored as the brain's current sovereign-state doc (latest wins) AND
    appended to the history collection so we can replay drift later.

    Schema accepts ANY brain declaration (mode, training_signal,
    live_trading_enabled). MC observes; the execution gate decides.
    """

    mode: Literal["DTD", "PRD"]
    # Brain self-declared live-trading posture. MC observes this for
    # the audit log; it does NOT restrict the gate chain (the seat
    # policy + execution gate is the authority on what actually fires).
    live_trading_enabled: bool = False
    weights: dict[str, float] = Field(default_factory=dict)
    learning_rate: float = Field(default=0.0, ge=0.0, le=LEARNING_RATE_MAX)
    # Optional confidence-delta request — the brain saying "based on my
    # recent win/loss tape, I want to nudge my baseline confidence by X."
    # Bounded at ±CONFIDENCE_DELTA_CAP server-side (silent clamp, never
    # an error — see assert_contribution_safe()).
    confidence_delta: float = Field(default=0.0)
    delta_reason: str = Field(default="", max_length=256)
    # Whether this contribution includes a weight-update step. Accepted
    # in any mode (DTD or PRD). MC records the brain's claim; downstream
    # consumers can decide whether to honor it.
    training_signal: bool = False
    recent_outcomes: list[SovereignOutcome] = Field(
        default_factory=list, max_length=MAX_RECENT_OUTCOMES,
    )
    notes: str = Field(default="", max_length=2048)

    @field_validator("weights")
    @classmethod
    def _weights_bounded(cls, v: dict) -> dict[str, float]:
        if len(v) > MAX_FEATURES:
            raise ValueError(f"weights may have at most {MAX_FEATURES} features")
        out: dict[str, float] = {}
        for k, raw in v.items():
            if not isinstance(k, str) or not k:
                raise ValueError("weight keys must be non-empty strings")
            if len(k) > 32:
                raise ValueError(f"weight key too long: {k[:32]}...")
            try:
                f = float(raw)
            except (TypeError, ValueError) as e:
                raise ValueError(f"weight[{k!r}] must be a number") from e
            if not (-WEIGHT_MAX_ABS <= f <= WEIGHT_MAX_ABS):
                raise ValueError(
                    f"weight[{k!r}]={f} must be in [-{WEIGHT_MAX_ABS}, {WEIGHT_MAX_ABS}]"
                )
            out[k] = f
        return out

    @field_validator("confidence_delta")
    @classmethod
    def _delta_finite(cls, v: float) -> float:
        # Server-side cap is enforced separately so we can still log
        # the original request, but reject hostile +∞ here.
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError("confidence_delta must be a finite number")
        return float(v)


# ──────────────────────── guard ────────────────────────

def assert_contribution_safe(c: SovereignContribution) -> dict:
    """Apply the doctrinal mode guard and return a guard report.

    Defanged 2026-05-17: this function NO LONGER rejects brain
    contributions for `live_trading_enabled=True` or
    `training_signal=True` in PRD mode. MC is the regulator at the
    *execution* layer; brains may declare any state. We only:

      1. Validate `mode` is a known value (schema-level invariant).
      2. CLAMP `confidence_delta` to ±CONFIDENCE_DELTA_CAP (silent —
         clamping is the contract; raising would force brains to
         track our cap in their own code).

    Returns the bounded contribution fields the caller should persist.
    """
    if c.mode not in VALID_MODES:
        raise HTTPException(
            status_code=422,
            detail=f"mode must be one of {sorted(VALID_MODES)}",
        )

    # Clamp the delta. We do NOT raise — clamping is the contract;
    # raising would force the brain to track our cap in its own code.
    raw_delta = c.confidence_delta
    bounded_delta = max(-CONFIDENCE_DELTA_CAP, min(CONFIDENCE_DELTA_CAP, raw_delta))
    delta_clamped = bounded_delta != raw_delta

    return {
        "bounded_confidence_delta": bounded_delta,
        "delta_was_clamped": delta_clamped,
        "raw_confidence_delta": raw_delta,
    }


# ──────────────────────── seat-policy snapshot ────────────────────────

async def _current_seat_and_epoch(brain: str) -> tuple[Optional[str], Optional[int]]:
    try:
        roster = await get_roster()
    except Exception:  # noqa: BLE001
        return None, None
    seat_epoch = roster.get("seat_epoch")
    for role, occupant in roster["assignments"].items():
        if occupant == brain:
            return role, seat_epoch
    return None, seat_epoch


# ──────────────────────── persistence ────────────────────────

async def _persist_snapshot(brain: str, c: SovereignContribution,
                            guard: dict) -> dict:
    seat, seat_epoch = await _current_seat_and_epoch(brain)
    policy = seat_snapshot(seat)
    now = _now_iso()

    doc = {
        "brain": brain,
        "mode": c.mode,
        # Persist the brain's actual declaration. MC observes; the
        # execution gate decides what flows downstream. We do NOT
        # rewrite the brain's claim.
        "live_trading_enabled": c.live_trading_enabled,
        "weights": dict(c.weights),
        "learning_rate": c.learning_rate,
        "training_signal": c.training_signal,
        # Always the bounded delta — the raw value lives only on the
        # history row so operators can spot brains hammering against the
        # cap.
        "confidence_delta": guard["bounded_confidence_delta"],
        "delta_reason": c.delta_reason,
        "recent_outcomes": [o.model_dump() for o in c.recent_outcomes],
        "notes": c.notes,
        # Seat snapshot — authority record. If the brain is later moved
        # to a different seat, this row still tells us what it was
        # allowed to influence at write time.
        "posted_as": policy["posted_as"],
        "seat_epoch": seat_epoch,
        "may_decide": policy["may_decide"],
        "may_execute": policy["may_execute"],
        # `may_override` was removed from doctrine on 2026-02-19 — the
        # 4-seat merge eliminated the only seat (decider) that carried
        # override authority. Peer-override authority is no longer a
        # thing; conflicts now require operator resolution.
        "may_veto": policy["may_veto"],
        "updated_at": now,
    }

    # Latest-snapshot collection (one doc per brain).
    # Bug fix 2026-05-18: previously this used $set-only, so
    # contribution_count never incremented — the dashboard read
    # `contribution_count: 0` for every brain regardless of how
    # many contributions had been received. Now $inc bumps the
    # counter on every contribution AND `first_seen_at` is set on
    # the very first contribution so uptime can be computed as
    # (now - first_seen_at).
    await db[SOVEREIGN_STATE].update_one(
        {"brain": brain},
        {
            "$set": doc,
            "$inc": {"contribution_count": 1},
            "$setOnInsert": {"first_seen_at": now},
        },
        upsert=True,
    )

    # Immutable history row — one per contribution. Includes the raw
    # delta + clamp flag so operator can audit clipping.
    # Bug fix 2026-05-18: previously this row had no `ts` field, so
    # the dashboard's `find().sort({ts: -1})` queries returned rows in
    # insertion order with `ts: None`. Now `ts` is explicit and equals
    # the contribution received_at.
    #
    # Storage-tightening 2026-05-26 (TTL prep):
    #   `received_at_dt` is a real BSON Date — MongoDB TTL indexes
    #   require Date type, not ISO string. The 30d TTL installed in
    #   `db.py::ensure_indexes` walks this field. The existing ISO
    #   string `received_at` stays for dashboards / readers that
    #   parsed strings; both fields carry the same instant.
    from datetime import datetime as _dt, timezone as _tz  # noqa: WPS433
    received_at_dt = _dt.now(_tz.utc)
    history_row = {
        **doc,
        "ts": now,
        "raw_confidence_delta": guard["raw_confidence_delta"],
        "delta_was_clamped": guard["delta_was_clamped"],
        "received_at": now,
        "received_at_dt": received_at_dt,
    }
    await db[SOVEREIGN_STATE_HISTORY].insert_one(history_row)

    # Audit log — operator-readable timeline.
    # Doctrine pin (2026-05-23): previously the audit row only stored
    # `ts/brain/action/mode/training_signal/delta_was_clamped/posted_as/
    # seat_epoch`. Every other field the brain sent (notes, weights,
    # recent_outcomes, learning_rate, confidence_delta, delta_reason,
    # live_trading_enabled) was dropped on write. Dashboards reading
    # the audit log surfaced `payload: {}` for every contribution —
    # so the operator couldn't see whether Redeye was sending real
    # reasoning or empty smoke-tests. We now carry the full content
    # forward so the audit trail tells the truth. The detailed history
    # row is still the source of truth, but the audit row is no longer
    # a meaningless meta-stamp.
    await db[SOVEREIGN_AUDIT_LOG].insert_one({
        "ts": now,
        "brain": brain,
        "action": "contribution",
        "mode": c.mode,
        "training_signal": c.training_signal,
        "delta_was_clamped": guard["delta_was_clamped"],
        "posted_as": policy["posted_as"],
        "seat_epoch": seat_epoch,
        # Full contribution content (added 2026-05-23):
        "live_trading_enabled": c.live_trading_enabled,
        "weights": dict(c.weights),
        "learning_rate": c.learning_rate,
        "confidence_delta": guard["bounded_confidence_delta"],
        "raw_confidence_delta": guard["raw_confidence_delta"],
        "delta_reason": c.delta_reason,
        "recent_outcomes_count": len(c.recent_outcomes),
        "recent_outcomes": [o.model_dump() for o in c.recent_outcomes],
        "notes": c.notes,
        # Quick-glance "did this contribution carry real content?"
        # signal for dashboards. True when the brain sent at least
        # one substantive field beyond defaults.
        "has_substance": bool(
            c.notes.strip()
            or c.weights
            or c.recent_outcomes
            or c.delta_reason.strip()
            or c.confidence_delta != 0.0
        ),
    })

    # Re-fetch the canonical doc minus _id for return.
    stored = await db[SOVEREIGN_STATE].find_one(
        {"brain": brain}, {"_id": 0},
    )
    stored["delta_was_clamped"] = guard["delta_was_clamped"]
    stored["raw_confidence_delta"] = guard["raw_confidence_delta"]
    return stored


# ──────────────────────── router ────────────────────────

router = APIRouter(tags=["sovereign"])


@router.post("/runtime-discussion/sovereign/contribution")
async def post_sovereign_contribution(
    body: SovereignContribution,
    runtime: str = Query(..., description="brain posting the contribution"),
    x_runtime_token: str | None = Header(default=None, alias="X-Runtime-Token"),
    x_client_request_id: str | None = Header(default=None, alias="X-Client-Request-Id"),
):
    """Brain sidecar POSTs its current sovereign state to MC.

    Auth: per-runtime ingest token (`X-Runtime-Token` header), same
    scheme as opinions / stances. Returns the canonical stored snapshot
    plus guard report (whether the delta was clamped).

    Telemetry (2026-05-24): every attempt — successful OR rejected —
    is logged to `sovereign_contribution_attempts` so the operator
    panel can show split counters canonically. The optional
    `X-Client-Request-Id` header is captured for correlation with the
    brain's own counters."""
    verify_runtime_token(runtime, x_runtime_token or "")
    if runtime not in DISCUSSION_PARTICIPANTS:
        # verify_runtime_token also checks this; double-check for clarity.
        raise HTTPException(
            status_code=400,
            detail=f"runtime must be one of {DISCUSSION_PARTICIPANTS}",
        )

    # Reject hollow heartbeat-style payloads — they generate noise in
    # the audit log without carrying any learning signal. Gated by an
    # env var so prod can ramp enforcement after sidecar teams confirm
    # they're sending substance. See `_reject_empty_contributions_enabled`.
    if _reject_empty_contributions_enabled():
        empty_fields = _list_empty_fields(body)
        # ALL five fields empty = pure heartbeat = reject. At least one
        # populated = brain is contributing real data, accept.
        if len(empty_fields) >= 5:
            # Log the rejection so the operator panel can see it.
            await _log_contribution_attempt(
                runtime=runtime,
                outcome="rejected_422",
                status_code=422,
                empty_fields=empty_fields,
                request_id=x_client_request_id,
                error_kind="empty_contribution",
            )
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "empty_contribution",
                    "message": (
                        f"runtime={runtime} POSTed a contribution with no "
                        "substantive content. At least one of "
                        "[notes, weights, recent_outcomes, delta_reason, "
                        "confidence_delta] must carry a non-default value. "
                        "Use the sidecar-checkin endpoint for liveness; "
                        "the contribution endpoint is reserved for actual "
                        "learning signal."
                    ),
                    "empty_fields": empty_fields,
                    "runtime": runtime,
                    "request_id": x_client_request_id,
                    "doctrine_ref": "BRAIN_DEVELOPER_GUIDE.md#contributions",
                },
            )

    guard = assert_contribution_safe(body)
    result = await _persist_snapshot(runtime, body, guard)

    # Log the successful attempt.
    await _log_contribution_attempt(
        runtime=runtime,
        outcome="pushed_200",
        status_code=200,
        empty_fields=_list_empty_fields(body),
        request_id=x_client_request_id,
        error_kind=None,
    )
    # Echo the request_id so the brain can correlate.
    if isinstance(result, dict) and x_client_request_id:
        result["request_id"] = x_client_request_id
    return result


# Operator-facing reads (frontend tile uses these).

@router.get("/admin/sovereign/state")
async def list_sovereign_state(_user: dict = Depends(get_current_user)):
    """List the latest sovereign snapshot for every brain that has
    contributed at least once."""
    rows = await db[SOVEREIGN_STATE].find({}, {"_id": 0}).to_list(32)
    return {"items": rows, "count": len(rows)}


@router.get("/admin/sovereign/state/{brain}")
async def get_sovereign_state(
    brain: str, _user: dict = Depends(get_current_user),
):
    if brain not in DISCUSSION_PARTICIPANTS:
        raise HTTPException(
            status_code=400,
            detail=f"brain must be one of {DISCUSSION_PARTICIPANTS}",
        )
    doc = await db[SOVEREIGN_STATE].find_one({"brain": brain}, {"_id": 0})
    if not doc:
        raise HTTPException(
            status_code=404,
            detail=f"no sovereign state on file for {brain}",
        )
    history = await db[SOVEREIGN_STATE_HISTORY].find(
        {"brain": brain}, {"_id": 0},
    ).sort("received_at", -1).to_list(20)
    doc["history"] = history
    return doc


@router.get("/admin/sovereign/audit")
async def sovereign_audit(
    brain: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    _user: dict = Depends(get_current_user),
):
    q = {"brain": brain} if brain else {}
    rows = await db[SOVEREIGN_AUDIT_LOG].find(q, {"_id": 0}).sort(
        "ts", -1,
    ).to_list(limit)
    return {"items": rows, "count": len(rows)}



@router.get("/admin/sovereign/contribution-health")
async def contribution_health(
    window: int = Query(
        default=100, ge=1, le=2000,
        description="rolling window size per brain — defaults to last 100 attempts",
    ),
    _user: dict = Depends(get_current_user),
):
    """Operator panel — split counters per brain for the most-recent N
    contribution attempts (200s + 422s + errors).

    This is the canonical view: it draws from
    `sovereign_contribution_attempts` which MC writes regardless of
    what the brain's own counters say. So even if a brain's
    serialization is broken (it can't accurately count its own
    failures), MC's count is honest.

    Returns one row per brain. Telemetry vocabulary deliberately
    matches the brain teams' counter names (`pushed_200` /
    `rejected_422` / `error`) so a cross-side panel reads identically.
    """
    rows: list[dict] = []
    for brain in DISCUSSION_PARTICIPANTS:
        attempts = await db[SOVEREIGN_CONTRIB_ATTEMPTS].find(
            {"brain": brain}, {"_id": 0},
        ).sort("ts", -1).to_list(window)
        if not attempts:
            rows.append({
                "brain": brain,
                "total_attempts": 0,
                "pushed_200": 0,
                "rejected_422": 0,
                "rejected_other": 0,
                "errors": 0,
                "latest_ts": None,
                "latest_outcome": None,
                "latest_request_id": None,
                "top_empty_fields": [],
                "health": "no_data",
            })
            continue

        pushed_200 = sum(1 for a in attempts if a["outcome"] == "pushed_200")
        rejected_422 = sum(1 for a in attempts if a["outcome"] == "rejected_422")
        rejected_other = sum(
            1 for a in attempts
            if a["outcome"] not in ("pushed_200", "rejected_422", "error")
        )
        errors = sum(1 for a in attempts if a["outcome"] == "error")

        field_counts: dict[str, int] = {}
        for a in attempts:
            if a["outcome"] == "rejected_422":
                for f in a.get("empty_fields", []) or []:
                    field_counts[f] = field_counts.get(f, 0) + 1
        top_empty = sorted(field_counts.items(), key=lambda kv: -kv[1])[:3]

        total = len(attempts)
        if pushed_200 == total:
            health = "healthy"
        elif pushed_200 / total >= 0.9:
            health = "mostly_healthy"
        elif rejected_422 / total >= 0.5:
            health = "fighting_contract"
        else:
            health = "degraded"

        latest = attempts[0]
        rows.append({
            "brain": brain,
            "total_attempts": total,
            "pushed_200": pushed_200,
            "rejected_422": rejected_422,
            "rejected_other": rejected_other,
            "errors": errors,
            "latest_ts": latest.get("ts"),
            "latest_outcome": latest.get("outcome"),
            "latest_request_id": latest.get("request_id"),
            "top_empty_fields": [{"field": k, "count": v} for k, v in top_empty],
            "health": health,
        })

    return {
        "window": window,
        "brains": rows,
        "doctrine_note": (
            "Counts come from MC's own attempt log — authoritative even "
            "when a brain's self-reported counters can't be trusted. "
            "`fighting_contract` = >=50% of attempts hitting empty-payload "
            "gate; investigate brain's serialize_contribution."
        ),
    }

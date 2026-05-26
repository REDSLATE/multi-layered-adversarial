"""Decision Machine — intent envelope ingest.

Brains emit *intents*, not orders. Every intent is a candidate that lives
or dies based on whether the gate chain passes. This module accepts the
envelope from a brain, MC-stamps it (seat_at_post_time, intent_id, ts),
schema-pins the safety invariants (`may_execute=false`,
`requires_gate_pass=true`), and stores it in `shared_intents`.

The schema is deliberately strict: anything a brain could mutate that
would change execution authority is overridden by MC at ingest.

Endpoints:
    POST /api/intents              brain → MC, one intent per call
    GET  /api/intents              operator/brain read (filterable)
    POST /api/execution/dry_run    operator → MC, runs gate chain against
                                   an intent_id and returns verdict only
                                   (no broker call). Day 1 of the
                                   paper-trading sprint uses this.

Doctrine:
  * `may_execute` is schema-pinned to False. The brain CANNOT request
    execution authority via this envelope.
  * `requires_gate_pass` is schema-pinned to True. Cannot be bypassed.
  * `seat_at_post_time` is MC-stamped from live seat policy. The brain
    can declare its `stack` but cannot self-grant a role.
  * The Executor seat is registered separately (see executor_seat.py
    once Day 1 lands). Until then, every intent records
    `seat_at_post_time` as the brain's static `roster` role.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Header, Query
from pydantic import BaseModel, Field, field_validator

from auth import get_current_user
from db import db
from namespaces import (
    RUNTIMES,
    SHARED_INTENTS,
)
from runtime_auth import verify_runtime_token
from shared.regime_keys import (  # canonical regime/crypto primitives
    REGIME_FP_KEYS,
    _looks_like_crypto,
    _regime_fingerprint,
)


router = APIRouter(tags=["intents"])

# Strict action vocabulary — extend deliberately.
ACTIONS = ("BUY", "SELL", "SHORT", "COVER", "HOLD")


# ─────────────────────────────── schema ───────────────────────────────

class IntentIn(BaseModel):
    """Brain → MC. Subset of fields. MC fills the rest."""

    stack: Literal["alpha", "camaro", "chevelle", "redeye"]
    # 2026-05-24: Extended verbs.
    #   OPEN  — opens a new position; requires `direction` field
    #           (`long`→BUY, `short`→SHORT). Rejected if `direction`
    #           is absent.
    #   CLOSE — closes an existing position; MC discovers side
    #           (long→SELL, short→COVER) and qty from the broker.
    #           `direction` ignored.
    # OPEN/CLOSE are rewritten to canonical BUY/SELL/SHORT/COVER
    # immediately on intake by `_normalize_action`; the rest of the
    # 12-gate chain only ever sees the canonical actions, preserving
    # all existing logic.
    action: Literal["BUY", "SELL", "SHORT", "COVER", "HOLD", "OPEN", "CLOSE"]
    # Direction for OPEN; ignored otherwise. Optional so legacy
    # BUY/SHORT/SELL/COVER intents remain unchanged.
    direction: Optional[Literal["long", "short"]] = None
    symbol: str = Field(min_length=1, max_length=24)
    # Lane is the brain's declared asset class for this intent. MC uses
    # it to compose the canonical asset key and pick the broker. Missing
    # lane = NO_TRADE (fail-closed at the resolver).
    lane: Optional[Literal["equity", "crypto"]] = Field(default=None)
    confidence: float = Field(ge=0.0, le=1.0)
    risk_multiplier: float = Field(ge=0.0, le=1.0, default=0.0)
    rationale: str = Field(min_length=1, max_length=4000)

    # ─── Memory modulator hook (2026-05-24) ───
    # Numeric per-bar signal vector. When present on a BUY/SHORT intent,
    # MC's memory_modulator computes cosine similarity vs the brain's
    # prior memories for the same symbol and nudges `confidence` by
    # ∈ [-0.25, +0.10]. When absent or for HOLD/non-directional intents,
    # no modulation runs. Optional for backward-compat.
    features: Optional[dict[str, float]] = None

    # ─── Honesty telemetry (2026-05-14 doctrine) ───
    # Brains MUST tell MC the truth about their own thinking. Specifically:
    # separate MARKET JUDGMENT from EXECUTION JUDGMENT so a blocked trade
    # is never silently recorded as "HOLD". Every field below is optional
    # for backward-compat; brains that don't send them keep working, but
    # they forfeit honesty and disable the auditor's blocked-trade view.

    # Raw model output BEFORE any council/gate/penalty interference.
    raw_action: Optional[Literal["BUY", "SELL", "SHORT", "COVER", "HOLD"]] = None
    raw_confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)

    # What the brain WOULD have done if execution gates didn't intervene.
    market_decision: Optional[Literal["BUY", "SELL", "SHORT", "COVER", "HOLD"]] = None
    # The execution-side verdict (separate from market judgment).
    execution_decision: Optional[Literal["ALLOW", "BLOCK", "SIZE_DOWN", "OBSERVE_ONLY"]] = None
    # The action the brain chose to DISPLAY (what shows in the UI). Often
    # equals `action` above, but separating it lets us audit the case
    # where display_action diverges from market_decision.
    display_action: Optional[Literal["BUY", "SELL", "SHORT", "COVER", "HOLD"]] = None
    # If display_action == HOLD but market_decision was directional, this
    # explains WHY the brain held: COUNCIL_DISAGREEMENT_CONFIDENCE_CLAMP,
    # MIN_CONFIDENCE_TO_TRADE, FEATURE_HEALTH_CLAMP, PDT_BLOCK, etc.
    hold_reason: Optional[str] = Field(default=None, max_length=200)
    blocked_by: Optional[list[str]] = None  # array of gate names that fired
    would_have_traded_without_gates: Optional[bool] = None

    # Per-decision weighting telemetry — lets the operator see exactly
    # how the confidence got from `raw_confidence` to the final
    # `confidence` value above. Shape mirrors the brain's WeightState.
    pre_weight_confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    post_weight_confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    council_penalty: Optional[float] = None  # negative number, e.g. -0.08
    strategist_weight: Optional[float] = None
    auditor_weight: Optional[float] = None
    commander_weight: Optional[float] = None
    regime_weight: Optional[float] = None
    memory_weight: Optional[float] = None

    # Brain-supplied context. Bounded.
    evidence: dict = Field(default_factory=dict)
    decision_id: Optional[str] = Field(default=None, max_length=64)
    regime: Optional[str] = Field(default=None, max_length=48)

    # ─── Doctrine sidecar input (2026-02-17, equity-only) ───
    # Optional snapshot of market facts that drives the small-account
    # doctrine labeler (`shared.doctrine.base_labels`). When provided
    # and the intent is EQUITY, MC will run all four brain interpreters
    # and attach the resulting packet to the intent doc as a READ-ONLY
    # ATTACHMENT — it never influences direction, confidence, or any
    # gate decision. Crypto intents ignore this field (the small-account
    # doctrine is equity-flavored). Missing field → empty snapshot (just
    # the symbol) and the packet will mostly produce REJECT/no-data
    # labels, which is itself informative for Shelly's learning loop.
    #
    # Optional `strategy` key inside the snapshot (2026-02-17, rev2):
    # "gap_and_go" or "micro_pullback" — dispatches to a strategy-
    # specific doctrine_version (`gap_and_go_v1` / `micro_pullback_v1`).
    # Anything else (or absent) falls back to the generic
    # `small_account_sidecar_v1`. Patent J grades each independently.
    doctrine_snapshot: Optional[dict] = Field(default=None)

    # SAFETY INVARIANTS — schema-pinned. Cannot be overridden by the brain.
    may_execute: bool = Field(default=False)
    requires_gate_pass: bool = Field(default=True)

    @field_validator("may_execute")
    @classmethod
    def _pin_may_execute(cls, v: bool) -> bool:
        if v is True:
            raise ValueError("may_execute must be False in an intent envelope")
        return False

    @field_validator("requires_gate_pass")
    @classmethod
    def _pin_requires_gate_pass(cls, v: bool) -> bool:
        if v is False:
            raise ValueError("requires_gate_pass must be True in an intent envelope")
        return True

    @field_validator("symbol")
    @classmethod
    def _symbol_clean(cls, v: str) -> str:
        return v.strip().upper()

    @field_validator("doctrine_snapshot")
    @classmethod
    def _doctrine_snapshot_size_cap(cls, v):
        if v is None:
            return None
        import json
        if not isinstance(v, dict):
            raise ValueError("doctrine_snapshot must be an object")
        if len(json.dumps(v, default=str)) > 4 * 1024:
            raise ValueError("doctrine_snapshot must be ≤4 KB serialized")
        return v

    @field_validator("evidence")
    @classmethod
    def _evidence_size_cap(cls, v: dict) -> dict:
        # Mirror the opinions evidence cap — 16 KB max serialized.
        import json
        if len(json.dumps(v, default=str)) > 16 * 1024:
            raise ValueError("evidence must be ≤16 KB serialized")
        # Regime fingerprint shape check (2026-02-16). If the brain
        # supplied a `regime_fp`, every key must be one of the canonical
        # 6 — unknown keys would silently poison memory recall. Missing
        # keys are tolerated (the server back-fills from indicators at
        # ingest time; see `_enrich_regime_fp` in intents.py).
        rfp = v.get("regime_fp")
        if rfp is not None:
            if not isinstance(rfp, dict):
                raise ValueError("evidence.regime_fp must be an object")
            extra = set(rfp.keys()) - set(REGIME_FP_KEYS)
            if extra:
                raise ValueError(
                    f"evidence.regime_fp has unknown keys: {sorted(extra)}. "
                    f"Allowed: {sorted(REGIME_FP_KEYS)}"
                )
        return v


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _audit_lane_policy_rejection(
    *,
    stack: str,
    lane: Optional[str],
    symbol: str,
    action: str,
    confidence: float,
    rationale: str,
    ingest_method: str,
    admin_email: Optional[str] = None,
) -> None:
    """Record a brain-lane-policy rejection so the operator can see
    that a brain TRIED to emit and was muted at ingest.

    Two writes:
      1. mc_shelly — keeps the rejection in the training-data substrate
         alongside successful emissions. Same `event_type` prefix as
         normal intent ingest so feeds pick it up automatically.
      2. shared_intents — write a 'rejected_at_ingest' row so the
         existing Intents UI surfaces it without any new view. The row
         carries `gate_state='rejected_ingest'`, `executed=False`,
         `may_execute=False`. This is FAILS-CLOSED: the row exists for
         audit only, the gate chain will refuse to execute it.
    """
    import uuid as _uuid  # noqa: WPS433
    rejection_id = f"rejected-{_uuid.uuid4().hex}"
    now = _now_iso()
    doc = {
        "intent_id": rejection_id,
        "stack": stack,
        "action": action,
        "symbol": symbol,
        "lane": lane,
        "lane_source": "brain",
        "confidence": float(confidence),
        "rationale": rationale,
        "evidence": {},
        # ── gate-state: rejected before any gate ran ──
        "gate_state": "rejected_at_ingest",
        "rejected_reason": "brain_lane_policy",
        "rejected_policy": "brain_lane_policy",
        "may_execute": False,
        "requires_gate_pass": False,
        "executed": False,
        "executed_at": None,
        "execution_receipt_id": None,
        # ── audit ──
        "ingest_ts": now,
        "ingest_method": ingest_method,
        "ingest_admin_email": admin_email,
        "audit_only": True,
    }
    try:
        await db[SHARED_INTENTS].insert_one(doc.copy())
    except Exception:  # noqa: BLE001
        pass
    try:
        from shared.mc_shelly import record_async  # noqa: WPS433
        record_async(
            event_type="intent_rejected_at_ingest",
            brain=stack,
            symbol=symbol,
            action=action,
            confidence=float(confidence),
            outcome="rejected",
            rationale=rationale,
            ref_id=rejection_id,
            extra={
                "reason": "brain_lane_policy",
                "lane": lane,
                "ingest_method": ingest_method,
            },
        )
    except Exception:  # noqa: BLE001
        pass


def _doctrine_failure_packet(symbol: str, lane: Optional[str], reason: str) -> dict:
    """Minimal envelope when doctrine build fails for non-doctrinal
    reasons (import error, runtime crash). Intent ingest still
    proceeds — doctrine is advisory only."""
    return {
        "error": f"doctrine_packet_failed: {reason}"[:300],
        "doctrine_version": "router_failure_v1",
        "event_type": "BRAIN_DOCTRINE_SIDECAR_PACKET",
        "lane": lane or None,
        "symbol": symbol,
        "seats": {},
        "base_labels": {"score": 0.0, "quality": None, "labels": [], "reasons": []},
    }


async def _build_and_persist_doctrine_packet(
    *,
    intent_id: str,
    stack: str,
    lane: Optional[str],
    symbol: str,
    action: str,
    confidence: float,
    snapshot: Optional[dict],
    ingest_method: str,
    admin_email: Optional[str] = None,
) -> Optional[dict]:
    """Attach a `BRAIN_DOCTRINE_SIDECAR_PACKET` to the intent.

    Doctrine (2026-02-17):
        READ-ONLY ATTACHMENT — never modifies direction, confidence,
        or any gate state.

        Twin lanes get twin doctrine:
          • equity → `shared.doctrine.brain_sidecars` (gap/RVOL/float)
          • crypto → `shared.crypto.doctrine.crypto_brain_sidecars`
                     (24h volume / spread / funding / OI / liquidations)
          • else   → UNKNOWN_LANE_REJECT packet so absence isn't silent

        Routed via `shared.doctrine.lane_doctrine_router` so this
        function never imports the equity or crypto modules directly
        — keeps lane isolation regression-clean.

        Failure-isolated — if the labeler throws (bad snapshot field
        types, etc.), we log an error packet and the intent ingest
        still proceeds.

    Side effects when the packet is built:
        1. Returned for inclusion in the intent doc as `doctrine_packet`.
        2. Append-only audit row in `doctrine_sidecars` joined to
           `intent_id`. This is Shelly's training substrate.
        3. Shelly event `BRAIN_DOCTRINE_SIDECAR_PACKET` so the
           memory layer indexes it alongside the ingest row.
    """
    lane_norm = (lane or "").lower()

    # Build the snapshot the labeler consumes. The brain may have
    # supplied facts; we always inject lane + symbol + existing_intent
    # so the lane router and crypto Camaro readiness check have what
    # they need without forcing every brain to remember them.
    merged = dict(snapshot or {})
    merged.setdefault("symbol", symbol)
    merged["lane"] = lane_norm or merged.get("lane")
    # Camaro readiness depends on whether MC already has a directional
    # intent. The intent we're ingesting RIGHT NOW counts iff its
    # action is directional (BUY/SELL/SHORT/COVER) — HOLD does not.
    merged.setdefault(
        "existing_intent",
        (action or "").upper() in {"BUY", "SELL", "SHORT", "COVER"},
    )

    try:
        from shared.doctrine.lane_doctrine_router import (  # noqa: WPS433
            build_lane_doctrine_packet,
            fetch_seat_holders,
            hoist_packet_audit_fields,
        )
    except Exception as e:  # noqa: BLE001
        return _doctrine_failure_packet(symbol, lane_norm, f"router_import: {e!r}")

    # Resolve the four doctrine-relevant seat holders from the live
    # roster so the packet records "who was sitting in this seat at
    # packet build". Doctrine survives seat rotations untouched — only
    # `holder` shifts. Non-fatal: if the roster read fails (e.g., DB
    # offline during ingest), the packet still attaches with all four
    # holders as None.
    try:
        seat_holders = await fetch_seat_holders(lane_norm)
    except Exception:  # noqa: BLE001
        seat_holders = {}

    try:
        packet = build_lane_doctrine_packet(merged, seat_holders)
        hoisted = hoist_packet_audit_fields(packet)
    except Exception as e:  # noqa: BLE001
        return _doctrine_failure_packet(symbol, lane_norm, f"build: {e!r}")

    # Audit-row write — fire-and-forget shape, but await so we have a
    # row whether the rest of the ingest succeeds or not.
    #
    # Doctrine pin (2026-02-17, seat-doctrinal canonicalization):
    #   Audit fields are keyed by SEAT, never by brain identity.
    #   Holders are surfaced as METADATA only. Metrics computed off
    #   this row must NEVER imply "brain X underperformed" — only that
    #   "(lane, seat, doctrine_version) underperformed while X happened
    #   to occupy the seat." `stack` is preserved as metadata for
    #   per-brain context, NOT as a primary scoring axis.
    audit_row = {
        "intent_id": intent_id,
        "stack": stack,                           # METADATA: ingest brain
        "lane": lane_norm or None,
        "symbol": symbol,
        "action": action,
        "ingest_confidence": float(confidence),
        "ingest_method": ingest_method,
        "ingest_admin_email": admin_email,
        "snapshot": merged,
        "packet": packet,
        # ── seat-doctrinal canonical keys ────────────────────────────
        "quality": hoisted["quality"],
        "score": hoisted["score"],
        "doctrine_version": hoisted["doctrine_version"] or packet.get("doctrine_version"),
        "strategist_conviction_delta": hoisted["strategist_conviction_delta"],
        "strategist_holder": hoisted["strategist_holder"],
        "adversary_challenge_required": hoisted["adversary_challenge_required"],
        "adversary_challenge_strength": hoisted["adversary_challenge_strength"],
        "adversary_objection_count": hoisted["adversary_objection_count"],
        "adversary_holder": hoisted["adversary_holder"],
        "governor_action": hoisted["governor_action"],
        "governor_risk_multiplier": hoisted["governor_risk_multiplier"],
        "governor_block_reason_count": hoisted["governor_block_reason_count"],
        "governor_holder": hoisted["governor_holder"],
        "execution_judge_ready": hoisted["execution_judge_ready"],
        "execution_judge_holder": hoisted["execution_judge_holder"],
        # ── legacy brain-named aliases (DEPRECATED) ─────────────────
        # Kept for one deprecation cycle. New consumers MUST use the
        # seat-keyed names above. Will be removed in a future pass.
        "redeye_challenge_required": hoisted["redeye_challenge_required"],
        "chevelle_governor_action": hoisted["chevelle_governor_action"],
        "camaro_execution_ready": hoisted["camaro_execution_ready"],
        "ts": _now_iso(),
    }
    try:
        from namespaces import DOCTRINE_SIDECARS  # noqa: WPS433
        await db[DOCTRINE_SIDECARS].insert_one(audit_row.copy())
    except Exception:  # noqa: BLE001
        pass

    # Shelly memory — index by intent so the memory layer can recall
    # "what was the doctrine packet attached to this intent?" later.
    try:
        from shared.mc_shelly import record_async  # noqa: WPS433
        record_async(
            event_type="BRAIN_DOCTRINE_SIDECAR_PACKET",
            brain=stack,
            symbol=symbol,
            action=action,
            confidence=float(confidence),
            outcome="advisory",
            rationale=f"quality={hoisted['quality']}, score={hoisted['score']}",
            ref_id=intent_id,
            extra={
                "lane": lane_norm or None,
                "quality": hoisted["quality"],
                "score": hoisted["score"],
                "doctrine_version": packet.get("doctrine_version"),
            },
        )
    except Exception:  # noqa: BLE001
        pass

    return packet


async def _seat_at_post_time(brain: str) -> Optional[str]:
    """Stamp the brain's current role at the moment of ingest.

    Until the dedicated Executor seat exists, we use the brain's static
    roster role as recorded by the roles manifest. This will be replaced
    by a live lookup once the executor seat registry lands.
    """
    try:
        from shared.roster import get_role_of  # noqa: WPS433
        return await get_role_of(brain)
    except Exception:  # noqa: BLE001
        return None


async def _enrich_regime_fp(symbol: str, supplied_fp: Optional[dict]) -> dict:
    """Server-side regime_fp back-fill. If the brain supplied a fingerprint,
    we accept it as-is (validator already screened the key set). If it
    didn't, OR if it sent fewer than 6 keys, we top up from the latest
    `shared_indicator_snapshots` row for `symbol`. Keys the brain set win
    over server-derived keys — we trust the brain's view of its own
    setup, only filling gaps. Returns the merged 0-6 key dict.
    """
    supplied = dict(supplied_fp or {})
    # Skip the DB hit if the brain already sent the full set.
    if set(supplied.keys()) >= set(REGIME_FP_KEYS):
        return supplied
    try:
        from namespaces import SHARED_INDICATOR_SNAPSHOTS  # noqa: WPS433
        snap = await db[SHARED_INDICATOR_SNAPSHOTS].find_one(
            {"symbol": symbol}, {"_id": 0, "indicators": 1},
            sort=[("captured_at", -1)],
        )
    except Exception:  # noqa: BLE001
        snap = None
    derived = _regime_fingerprint((snap or {}).get("indicators") or {})
    # Brain-supplied keys win over derived ones; fill missing only.
    for k, v in derived.items():
        supplied.setdefault(k, v)
    return supplied


def _compose_canonical(symbol: str, lane: Optional[str]) -> tuple[Optional[str], Optional[str], bool]:
    """Compose `(effective_lane, canonical, inferred)`.

    `inferred` is True when MC filled in the lane the brain failed to
    send. Visible on the persisted intent as `inferred_lane=True` so
    the audit trail shows MC's defensive fill-in (vs. an explicit
    brain-side tag).

    Inference precedence:
      1. Explicit lane on the envelope — always wins.
      2. Symbol looks unambiguously crypto (BTC/USD, ETH-USDT, …)
         → infer `lane="crypto"`.
      3. Plain alphanumeric symbol (AAPL, NVDA, GOOGL) → infer
         `lane="equity"` for backward-compat with today's Camaro flow.
      4. Otherwise → leave lane None and let the broker router fail
         closed downstream (never silently routes the wrong asset).
    """
    effective_lane = lane
    inferred = False
    if effective_lane is None:
        if _looks_like_crypto(symbol):
            effective_lane = "crypto"
            inferred = True
        elif symbol.isalnum():
            effective_lane = "equity"
            inferred = True
    canonical: Optional[str] = None
    if effective_lane:
        try:
            from shared.broker_symbol_resolver import compose as _compose  # noqa: WPS433
            canonical = _compose(symbol, effective_lane).canonical
        except Exception:  # noqa: BLE001
            canonical = None
    return effective_lane, canonical, inferred


# ─────────────────────────────── routes ───────────────────────────────

@router.post("/intents")
async def post_intent(
    body: IntentIn,
    x_runtime_token: Optional[str] = Header(default=None, alias="X-Runtime-Token"),
):
    """Brain emits an intent envelope. MC stamps the safety fields.

    Auth: `X-Runtime-Token` of the brain that matches `body.stack`. A
    brain cannot post an intent as another brain. Operators use the
    `/admin/intents` proxy below for that.
    """
    if not x_runtime_token:
        raise HTTPException(status_code=401, detail="X-Runtime-Token required")
    verify_runtime_token(body.stack, x_runtime_token)

    # ─── OPEN / CLOSE verb translation (2026-05-24) ──────────────────
    # Brains may post `action="OPEN"` or `"CLOSE"` for symmetry with
    # the lifecycle vocabulary. We rewrite to canonical BUY/SHORT/SELL/
    # COVER here so the rest of the gate chain only sees the legacy
    # actions. OPEN requires `direction`. CLOSE delegates to the
    # close_position helper to discover side+qty from the broker.
    if body.action == "OPEN":
        if body.direction not in {"long", "short"}:
            raise HTTPException(
                status_code=422,
                detail=(
                    "action=OPEN requires `direction` to be 'long' or 'short'. "
                    "Send action=BUY/SHORT directly if you prefer to skip the "
                    "lifecycle vocabulary."
                ),
            )
        body.action = "BUY" if body.direction == "long" else "SHORT"
        # Keep raw_action / display_action consistent if the brain set them.
        if body.raw_action == "OPEN":
            body.raw_action = body.action
        if body.display_action == "OPEN":
            body.display_action = body.action
    elif body.action == "CLOSE":
        # Hand off to the position-close flow which discovers side+qty
        # from the broker, builds the inverse-side intent, and routes
        # it through the SAME gate chain via this same function.
        from routes.runtime_position_close import (  # noqa: WPS433
            CloseIn, close_position,
        )
        if body.lane not in {"equity", "crypto"}:
            raise HTTPException(
                status_code=422,
                detail="action=CLOSE requires lane ∈ {equity, crypto}",
            )
        return await close_position(
            body=CloseIn(
                symbol=body.symbol,
                lane=body.lane,
                fraction=1.0,
                rationale=body.rationale,
                confidence=body.confidence,
            ),
            x_runtime_token=x_runtime_token,
        )

    # ─── Memory modulator (2026-05-24, doctrine-locked) ───
    # PRE-GATE step. Nudges confidence based on similarity to prior
    # memories for this (brain, symbol, action). Doctrine: may not
    # promote HOLD, may not create direction, may not bypass any gate.
    # Output is stamped on the intent's audit row for full provenance.
    memory_modulator_info: Optional[dict] = None
    if body.action in {"BUY", "SHORT"} and body.features:
        try:
            from shared.memory_modulator import (  # noqa: WPS433
                apply_to_confidence, compute_memory_modulator,
            )
            memory_modulator_info = await compute_memory_modulator(
                brain=body.stack,
                symbol=body.symbol,
                action=body.action,
                features=body.features,
            )
            mod_value = memory_modulator_info["modulator"]
            if mod_value != 0.0:
                new_conf = apply_to_confidence(body.confidence, mod_value)
                memory_modulator_info["original_confidence"] = body.confidence
                memory_modulator_info["modulated_confidence"] = new_conf
                body.confidence = new_conf
        except Exception as e:  # noqa: BLE001
            # Fail-OPEN: a modulator error must NEVER block an intent.
            # Log + record skip; downstream gates run on unmodulated confidence.
            logger.warning("memory_modulator: error %r — skipping nudge", e)
            memory_modulator_info = {
                "modulator": 0.0, "skipped": True,
                "reason": f"modulator error: {e!r}",
            }

    # Per-brain × lane emission policy (2026-02-16). Reject at ingest
    # before we burn a uuid or write anything. Camaro→crypto is muted
    # by default (see brain_lane_policy.seed_default_policy).
    effective_lane, canonical, inferred_lane = _compose_canonical(body.symbol, body.lane)
    from shared.brain_lane_policy import is_brain_lane_allowed  # noqa: WPS433
    if not await is_brain_lane_allowed(body.stack, effective_lane):
        # Audit-write the rejection BEFORE the 403 so crypto rejections
        # are visible in the decisions feed / mc_shelly. Otherwise the
        # mute is invisible — operators can't tell whether a brain
        # tried and was blocked vs. never emitted at all.
        await _audit_lane_policy_rejection(
            stack=body.stack, lane=effective_lane, symbol=body.symbol,
            action=body.action, confidence=float(body.confidence),
            rationale=body.rationale, ingest_method="runtime_token",
        )
        raise HTTPException(
            status_code=403,
            detail=(
                f"{body.stack} is not authorized to emit {effective_lane} intents "
                f"(per /api/admin/brain-lane-policy)."
            ),
        )

    seat = await _seat_at_post_time(body.stack)

    # Lane-aware execute-seat snapshot at ingest. For a crypto intent
    # we record who holds the CRYPTO seat at this moment, not the
    # equity executor — they're independent doctrines. Equity intents
    # still resolve to the equity executor (legacy semantic preserved
    # via seats_with_execute("equity") returning ["executor"]).
    #
    # 2026-02-16: the equity executor seat is no longer consulted for
    # crypto intents at any layer (ingest stamp, gate chain message,
    # audit feed). REDEYE crypto and Alpha equity are physically
    # independent execution paths from this point forward.
    from shared.executor_seat import (  # noqa: WPS433
        get_seat_holder,
        seats_with_execute,
    )
    eligible_seats_at_post = seats_with_execute(effective_lane)
    holds_executor = False
    matched_seat_at_post = None
    executor_at_post = None        # holder of the lane's execute-seat at post time
    for _seat_name in eligible_seats_at_post:
        _h = await get_seat_holder(_seat_name)
        if _h and executor_at_post is None:
            executor_at_post = _h
        if _h == body.stack:
            holds_executor = True
            matched_seat_at_post = _seat_name
            # Don't break — we want to record the *first* eligible seat
            # holder for the lane (audit), but if that's also us, perfect.

    intent_id = str(uuid.uuid4())

    # Server-side regime_fp back-fill (2026-02-16). Brains may ship a
    # partial fingerprint or none at all; MC tops up missing keys from
    # the latest indicator snapshot so memory recall has a stable 6-key
    # target. Brain-supplied keys are not overwritten.
    evidence = dict(body.evidence or {})
    evidence["regime_fp"] = await _enrich_regime_fp(body.symbol, evidence.get("regime_fp"))

    # Brain doctrine sidecar packet — READ-ONLY ATTACHMENT (2026-02-17).
    # Equity-only by doctrine. Never influences direction or any gate.
    doctrine_packet = await _build_and_persist_doctrine_packet(
        intent_id=intent_id,
        stack=body.stack,
        lane=effective_lane,
        symbol=body.symbol,
        action=body.action,
        confidence=float(body.confidence),
        snapshot=body.doctrine_snapshot,
        ingest_method="runtime_token",
    )

    doc = {
        "intent_id": intent_id,
        "stack": body.stack,
        "action": body.action,
        "symbol": body.symbol,
        "lane": effective_lane,
        "lane_source": "brain" if (body.lane is not None) else ("inferred" if effective_lane else "unset"),
        "inferred_lane": inferred_lane,
        "canonical": canonical,
        "confidence": float(body.confidence),
        "risk_multiplier": float(body.risk_multiplier),
        "rationale": body.rationale,
        "evidence": evidence,
        "doctrine_packet": doctrine_packet,
        "decision_id": body.decision_id,
        "regime": body.regime,
        # ─── Honesty telemetry — brain-side ground truth ───
        # Captures the SEPARATION between market judgment and execution
        # judgment so a blocked trade never silently becomes "HOLD".
        # All optional — missing fields stay None.
        "raw_action": body.raw_action,
        "raw_confidence": body.raw_confidence,
        "market_decision": body.market_decision,
        "execution_decision": body.execution_decision,
        "display_action": body.display_action,
        "hold_reason": body.hold_reason,
        "blocked_by": body.blocked_by or [],
        "would_have_traded_without_gates": body.would_have_traded_without_gates,
        "pre_weight_confidence": body.pre_weight_confidence,
        "post_weight_confidence": body.post_weight_confidence,
        "council_penalty": body.council_penalty,
        "weights": {
            "strategist": body.strategist_weight,
            "auditor": body.auditor_weight,
            "commander": body.commander_weight,
            "regime": body.regime_weight,
            "memory": body.memory_weight,
        } if any(w is not None for w in (
            body.strategist_weight, body.auditor_weight, body.commander_weight,
            body.regime_weight, body.memory_weight,
        )) else None,
        # SAFETY (MC-stamped, schema-pinned)
        "may_execute": False,
        "requires_gate_pass": True,
        # AUTHORITY (MC-stamped, not brain-controlled)
        "seat_at_post_time": seat,
        "executor_holder_at_post": executor_at_post,
        "holds_executor_seat": holds_executor,
        "matched_seat_at_post": matched_seat_at_post,
        # AUDIT (MC-stamped)
        "ingest_ts": _now_iso(),
        "ingest_method": "runtime_token",
        # MARKET SNAPSHOT — persisted so gates that need ground-truth
        # market structure (RoadGuard reads `spread_bps`, future gates
        # may read more) can find it on the intent doc. Field
        # explicitly mirrors what `_build_and_persist_doctrine_packet`
        # consumes — the brain's `doctrine_snapshot` becomes the
        # intent's `snapshot`. Pre-2026-02-18 this was silently dropped,
        # which caused EVERY intent to fail `roadguard_spread_floor`
        # at gate 7 with ROADGUARD_MISSING_SPREAD_BPS even when the
        # brain dutifully sent the field.
        "snapshot": dict(body.doctrine_snapshot or {}),
        # LIFECYCLE
        "gate_state": "pending",   # pending | passed | blocked | dry_run_passed | dry_run_blocked
        "executed": False,
        "executed_at": None,
        "execution_receipt_id": None,
    }
    if memory_modulator_info is not None:
        doc["memory_modulator"] = memory_modulator_info
    await db[SHARED_INTENTS].insert_one(doc)
    # so the brain never waits on bookkeeping.
    from shared.mc_shelly import record_async  # noqa: WPS433
    record_async(
        event_type="intent_ingested",
        brain=body.stack,
        symbol=body.symbol,
        action=body.action,
        confidence=float(body.confidence),
        outcome="pending",
        regime_fp=evidence.get("regime_fp"),
        rationale=body.rationale,
        ref_id=intent_id,
    )

    return {
        "ok": True,
        "intent_id": intent_id,
        "stack": body.stack,
        "seat_at_post_time": seat,
        "gate_state": "pending",
        "ingest_ts": doc["ingest_ts"],
        # Surface the attached doctrine packet so the brain (or the
        # operator using the dry-run flow) can see the quality band
        # MC assigned. Read-only — does not affect routing.
        "doctrine_packet": doctrine_packet,
    }


# ───── per-lane intent endpoints (2026-02-16) ─────
#
# Doctrine: equity and crypto each have their own execute seat. They
# now each have their own intent emission endpoint too — parity with
# the lane-isolated risk guards and seats.
#
# Generic `/api/intents` and `/api/admin/intents` are preserved for
# back-compat (existing brain sidecars still work), but new emitters
# should target the per-lane endpoint matching their seat. The per-lane
# endpoint enforces that the intent's lane matches the path and rejects
# 400 on mismatch — so `POST /api/intents/crypto` with `symbol=AAPL`
# can never silently route through.


def _lane_pin(body: IntentIn, expected_lane: Literal["equity", "crypto"]) -> None:
    """Validate that an intent destined for a per-lane endpoint either
    omits `lane` (we'll force-set it) or matches the path's lane."""
    submitted = (body.lane or "").lower().strip() if body.lane else None
    if submitted and submitted != expected_lane:
        raise HTTPException(
            status_code=400,
            detail=(
                f"This endpoint accepts {expected_lane!r} intents only; "
                f"got lane={submitted!r}. Use /api/intents/{submitted} instead."
            ),
        )
    # Force-pin lane so the inference layer / downstream gates can't
    # silently rewrite it.
    body.lane = expected_lane


@router.post("/intents/crypto")
async def post_intent_crypto(
    body: IntentIn,
    x_runtime_token: Optional[str] = Header(default=None, alias="X-Runtime-Token"),
):
    """Crypto-lane intent emission. Dedicated path for REDEYE's crypto
    executor seat. Doctrine: parity with the per-lane risk guards.

    Refuses non-crypto symbols (400) — no silent cross-lane routing.
    Still subject to the `brain_lane_policy` ingest filter, gate chain,
    and lane-isolation regression test.
    """
    _lane_pin(body, "crypto")
    return await post_intent(body, x_runtime_token=x_runtime_token)


@router.post("/intents/equity")
async def post_intent_equity(
    body: IntentIn,
    x_runtime_token: Optional[str] = Header(default=None, alias="X-Runtime-Token"),
):
    """Equity-lane intent emission. Dedicated path for the equity
    executor seat (today: Alpha). Mirror of `/intents/crypto`."""
    _lane_pin(body, "equity")
    return await post_intent(body, x_runtime_token=x_runtime_token)


@router.get("/intents")
async def list_intents(
    stack: Optional[str] = Query(default=None),
    symbol: Optional[str] = Query(default=None),
    lane: Optional[Literal["equity", "crypto"]] = Query(default=None),
    gate_state: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    x_runtime_token: Optional[str] = Header(default=None, alias="X-Runtime-Token"),
):
    """Read recent intents. Accepts either an operator JWT (via the admin
    proxy below) or any runtime token (brains can read each other's
    intents for council-context purposes — same doctrine as opinions).
    """
    if not x_runtime_token:
        raise HTTPException(status_code=401, detail="X-Runtime-Token required")
    # Token must match SOMEONE in the four-brain roster.
    matched = False
    for rt in RUNTIMES:
        try:
            verify_runtime_token(rt, x_runtime_token)
            matched = True
            break
        except HTTPException:
            continue
    if not matched:
        raise HTTPException(status_code=401, detail="invalid runtime ingest token")

    q: dict = {}
    if stack:
        q["stack"] = stack
    if symbol:
        q["symbol"] = symbol.strip().upper()
    if lane:
        q["lane"] = lane
    if gate_state:
        q["gate_state"] = gate_state

    rows = await db[SHARED_INTENTS].find(q, {"_id": 0}).sort("ingest_ts", -1).to_list(limit)
    return {"items": rows, "count": len(rows)}


# ──────────────────── operator: honesty audit ────────────────────

@router.get("/admin/intents/honesty")
async def honesty_audit(
    stack: Optional[str] = Query(default=None),
    hours: int = Query(default=24, ge=1, le=168),
    limit: int = Query(default=100, ge=1, le=500),
    user: dict = Depends(get_current_user),  # noqa: B008
):
    """Surface every intent where the brain's market_decision was
    directional (BUY/SELL/SHORT/COVER) but display_action ended up
    HOLD — the silent-block failure mode.

    Returns the count of "would_have_traded_without_gates=True" intents,
    grouped by stack and by reason, so the operator can see at a glance
    whether the brains are being mathematically flattened by gates.
    """
    from datetime import timedelta  # noqa: WPS433
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()

    q: dict = {
        "ingest_ts": {"$gte": since},
        "would_have_traded_without_gates": True,
    }
    if stack:
        q["stack"] = stack

    rows = await db[SHARED_INTENTS].find(q, {"_id": 0}).sort("ingest_ts", -1).to_list(limit)

    # Reason tallies.
    by_stack: dict = {}
    by_reason: dict = {}
    for r in rows:
        s = r.get("stack", "?")
        by_stack[s] = by_stack.get(s, 0) + 1
        reason = r.get("hold_reason") or "unspecified"
        by_reason[reason] = by_reason.get(reason, 0) + 1

    # How many intents the brains submitted at all in this window — for
    # the "X out of Y would have traded but didn't" framing.
    total_q: dict = {"ingest_ts": {"$gte": since}}
    if stack:
        total_q["stack"] = stack
    total = await db[SHARED_INTENTS].count_documents(total_q)

    return {
        "since": since,
        "hours": hours,
        "stack_filter": stack,
        "total_intents_in_window": total,
        "blocked_directional": len(rows),
        "blocked_pct_of_total": round((len(rows) / total) * 100, 2) if total else 0,
        "by_stack": by_stack,
        "by_reason": by_reason,
        "items": rows,
        "as_of": _now_iso(),
        "by": user.get("email"),
    }


# ──────────────────── admin proxy + dry-run gate chain ────────────────────

@router.post("/admin/intents")
async def admin_post_intent(
    body: IntentIn,
    user: dict = Depends(get_current_user),  # noqa: B008
):
    """Operator-authed intent emission on behalf of any brain.

    Useful for: stress-testing the gate chain, replaying historical
    decisions, or filling a missing intent during sidecar downtime.
    """
    effective_lane, canonical, inferred_lane = _compose_canonical(body.symbol, body.lane)

    # Per-brain × lane policy applies to the admin proxy too — operators
    # can override by toggling the policy first via
    # /api/admin/brain-lane-policy. We do not silently bypass.
    from shared.brain_lane_policy import is_brain_lane_allowed  # noqa: WPS433
    if not await is_brain_lane_allowed(body.stack, effective_lane):
        await _audit_lane_policy_rejection(
            stack=body.stack, lane=effective_lane, symbol=body.symbol,
            action=body.action, confidence=float(body.confidence),
            rationale=body.rationale, ingest_method="admin_proxy",
            admin_email=user.get("email"),
        )
        raise HTTPException(
            status_code=403,
            detail=(
                f"{body.stack} is not authorized to emit {effective_lane} intents "
                f"(per /api/admin/brain-lane-policy). Toggle the policy first."
            ),
        )

    seat = await _seat_at_post_time(body.stack)

    # Lane-aware execute-seat snapshot — same doctrine as the engine
    # ingest path. For crypto intents the equity executor seat is
    # never consulted.
    from shared.executor_seat import (  # noqa: WPS433
        get_seat_holder,
        seats_with_execute,
    )
    eligible_seats_at_post = seats_with_execute(effective_lane)
    holds_executor = False
    matched_seat_at_post = None
    executor_at_post = None
    for _seat_name in eligible_seats_at_post:
        _h = await get_seat_holder(_seat_name)
        if _h and executor_at_post is None:
            executor_at_post = _h
        if _h == body.stack:
            holds_executor = True
            matched_seat_at_post = _seat_name

    intent_id = str(uuid.uuid4())

    # Server-side regime_fp back-fill — same doctrine as POST /api/intents.
    evidence = dict(body.evidence or {})
    evidence["regime_fp"] = await _enrich_regime_fp(body.symbol, evidence.get("regime_fp"))

    # Brain doctrine sidecar packet — READ-ONLY ATTACHMENT (2026-02-17).
    # Equity-only by doctrine. Never influences direction or any gate.
    doctrine_packet = await _build_and_persist_doctrine_packet(
        intent_id=intent_id,
        stack=body.stack,
        lane=effective_lane,
        symbol=body.symbol,
        action=body.action,
        confidence=float(body.confidence),
        snapshot=body.doctrine_snapshot,
        ingest_method="admin_proxy",
        admin_email=user.get("email"),
    )

    doc = {
        "intent_id": intent_id,
        "stack": body.stack,
        "action": body.action,
        "symbol": body.symbol,
        "lane": effective_lane,
        "lane_source": "brain" if (body.lane is not None) else ("inferred" if effective_lane else "unset"),
        "inferred_lane": inferred_lane,
        "canonical": canonical,
        "confidence": float(body.confidence),
        "risk_multiplier": float(body.risk_multiplier),
        "rationale": body.rationale,
        "evidence": evidence,
        "doctrine_packet": doctrine_packet,
        "decision_id": body.decision_id,
        "regime": body.regime,
        "may_execute": False,
        "requires_gate_pass": True,
        "seat_at_post_time": seat,
        "executor_holder_at_post": executor_at_post,
        "holds_executor_seat": holds_executor,
        "matched_seat_at_post": matched_seat_at_post,
        "ingest_ts": _now_iso(),
        "ingest_method": "admin_proxy",
        "ingest_admin_email": user.get("email"),
        # See doctrine note on the runtime-token ingest path: the
        # brain's `doctrine_snapshot` is persisted under `snapshot` so
        # the gate chain can read market-structure facts. Without this,
        # `roadguard_spread_floor` fails closed on every intent.
        "snapshot": dict(body.doctrine_snapshot or {}),
        "gate_state": "pending",
        "executed": False,
        "executed_at": None,
        "execution_receipt_id": None,
    }
    await db[SHARED_INTENTS].insert_one(doc)

    # MC Shelly — record this admin-proxied ingest too. Tagged with the
    # operator email under extra so we can distinguish brain pushes from
    # operator-injected ones during training.
    from shared.mc_shelly import record_async  # noqa: WPS433
    record_async(
        event_type="intent_ingested",
        brain=body.stack,
        symbol=body.symbol,
        action=body.action,
        confidence=float(body.confidence),
        outcome="pending",
        regime_fp=evidence.get("regime_fp"),
        rationale=body.rationale,
        ref_id=intent_id,
        extra={"ingest_method": "admin_proxy", "by": user.get("email")},
    )

    return {
        "ok": True,
        "intent_id": intent_id,
        "stack": body.stack,
        "seat_at_post_time": seat,
        "gate_state": "pending",
        "ingest_via": "admin_proxy",
        "doctrine_packet": doctrine_packet,
    }


# ───── per-lane admin intent endpoints (2026-02-16) ─────


@router.post("/admin/intents/crypto")
async def admin_post_intent_crypto(
    body: IntentIn,
    user: dict = Depends(get_current_user),  # noqa: B008
):
    """Operator-authed crypto-lane intent emission. Mirror of
    `/admin/intents`, lane-pinned to `crypto`."""
    _lane_pin(body, "crypto")
    return await admin_post_intent(body, user=user)


@router.post("/admin/intents/equity")
async def admin_post_intent_equity(
    body: IntentIn,
    user: dict = Depends(get_current_user),  # noqa: B008
):
    """Operator-authed equity-lane intent emission. Mirror of
    `/admin/intents`, lane-pinned to `equity`."""
    _lane_pin(body, "equity")
    return await admin_post_intent(body, user=user)


# NOTE: `/execution/dry_run` and `/execution/submit` live in
# `shared/execution.py` — they consume the full gate chain including
# real exposure caps and the live Alpaca paper adapter.


# ───── doctrine sidecar audit log read (2026-02-17) ─────


@router.get("/admin/intents/doctrine-sidecars")
async def list_doctrine_sidecars(
    stack: Optional[str] = Query(default=None),
    symbol: Optional[str] = Query(default=None),
    quality: Optional[Literal["A_QUALITY", "B_QUALITY", "C_QUALITY", "REJECT"]] = Query(default=None),
    chevelle_governor_action: Optional[Literal["block", "modulate"]] = Query(default=None),
    camaro_execution_ready: Optional[bool] = Query(default=None),
    redeye_challenge_required: Optional[bool] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    """Read the append-only `doctrine_sidecars` audit log.

    Read-only training substrate for Shelly + operator review. Filters
    let the operator pull slices like "all A_QUALITY intents Camaro
    flagged execution_ready", or "all REJECT intents Chevelle blocked".

    Doctrine pin: this surface NEVER decides anything. It exists to
    join brain doctrine output to eventual trade outcomes for later
    verified-reinforcement learning.
    """
    from namespaces import DOCTRINE_SIDECARS  # noqa: WPS433
    q: dict = {}
    if stack:
        q["stack"] = stack
    if symbol:
        q["symbol"] = symbol.strip().upper()
    if quality:
        q["quality"] = quality
    if chevelle_governor_action:
        q["chevelle_governor_action"] = chevelle_governor_action
    if camaro_execution_ready is not None:
        q["camaro_execution_ready"] = bool(camaro_execution_ready)
    if redeye_challenge_required is not None:
        q["redeye_challenge_required"] = bool(redeye_challenge_required)

    rows = await db[DOCTRINE_SIDECARS].find(q, {"_id": 0}).sort("ts", -1).to_list(limit)
    # Quality histogram so the operator can see the distribution
    # without re-aggregating client-side.
    histogram: dict = {"A_QUALITY": 0, "B_QUALITY": 0, "C_QUALITY": 0, "REJECT": 0, "unknown": 0}
    for r in rows:
        key = r.get("quality") or "unknown"
        histogram[key] = histogram.get(key, 0) + 1
    return {
        "items": rows,
        "count": len(rows),
        "quality_histogram": histogram,
        "as_of": _now_iso(),
    }


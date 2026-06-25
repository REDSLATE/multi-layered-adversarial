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

import logging
import uuid
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Header, Query, Request
from pydantic import BaseModel, Field, field_validator

from auth import get_current_user
from db import db
from namespaces import (
    RUNTIMES,
    SHARED_GATE_RESULTS,
    SHARED_INTENTS,
)
from runtime_auth import verify_runtime_token
from shared.brain_legend import canonicalize_stack  # 2026-02-23 dual-field migration
from shared.intent_envelope_v3 import (  # 2026-02 Paradox v3 schema (Step 1)
    ExecutionBlock,
    PlanBlock,
)
from shared.regime_keys import (  # canonical regime/crypto primitives
    REGIME_FP_KEYS,
    _looks_like_crypto,
    _regime_fingerprint,
)


router = APIRouter(tags=["intents"])

logger = logging.getLogger(__name__)

# Strict action vocabulary — extend deliberately.
ACTIONS = ("BUY", "SELL", "SHORT", "COVER", "HOLD")


# ─── Auto-dry-run-on-ingest hook (2026-05-27) ───────────────────────
# Doctrine: intents must never sit at `gate_state=pending` indefinitely.
# Before this hook, brain emissions piled up at `pending` because the
# gate chain was only evaluated when an operator manually called
# `/execution/dry_run`. Prod accumulated 100+ Camaro pending intents;
# preview accumulated 6,000+. This hook fires `_evaluate_gates`
# immediately after a successful insert so every new intent transitions
# to `dry_run_passed` / `dry_run_blocked` within milliseconds.
#
# Env-gated. Default ON. Operator can flip OFF on prod via
# `AUTO_DRY_RUN_ON_INGEST=false` while load-tuning. Off does NOT break
# anything — intents revert to the old behavior (sit at `pending`
# until manual dry-run).
import os as _os  # noqa: WPS433
import asyncio as _asyncio  # noqa: WPS433


def _auto_dry_run_enabled() -> bool:
    val = _os.environ.get("AUTO_DRY_RUN_ON_INGEST", "true").strip().lower()
    return val in {"true", "1", "yes", "on"}


async def _fire_and_forget_dry_run(intent_id: str, actor: str) -> None:
    """Best-effort dry-run launcher. Always swallows exceptions so the
    brain's POST never blocks on bookkeeping. We schedule the actual
    gate evaluation as a background task; the response to the brain
    returns immediately with `gate_state=pending` (the dry-run flip
    happens asynchronously, typically within ~50ms)."""
    if not _auto_dry_run_enabled():
        return
    try:
        # Chain dry-run → auto-submit (Phase 1 throughput unlock).
        # `_run_dry_run_then_auto_submit` runs the existing dry-run
        # and then calls the auto-submit policy. Policy is OFF by
        # default — operator opts in via the admin endpoint.
        _asyncio.create_task(_run_safely(
            _run_dry_run_then_auto_submit(intent_id, actor=actor)
        ))
    except Exception as e:  # noqa: BLE001
        logger.warning("auto_dry_run schedule failed for %s: %s", intent_id, e)


async def _run_safely(coro) -> None:
    """Swallow exceptions in the background dry-run task. The intent
    just stays at `pending` if anything goes wrong — operator can
    manually re-run via the existing endpoint."""
    try:
        await coro
    except Exception as e:  # noqa: BLE001
        logger.warning("auto_dry_run background task failed: %s", e)


async def _run_dry_run_then_auto_submit(intent_id: str, actor: str) -> None:
    """Chain auto-submit (Phase 1) onto the dry-run finalizer.

    Doctrine: the brain emits → dry-run completes ~50ms later → if
    tier-1 policy matches we call the SAME `execution_submit` path
    the operator's SUBMIT button uses. Every gate still runs. The
    receipt's `executed_by` field marks the intent as machine-
    advanced (`auto_submit_tier_1@risedual.io`) so the audit feed
    shows operator-click vs. policy-advanced trades distinctly.

    Failure here is silently swallowed — auto-submit is a throughput
    optimization, not a correctness gate. If it fails, the intent
    sits at dry_run_passed and the operator can click SUBMIT
    manually. The post-mortem panel will surface the failure pattern.
    """
    from shared.execution import run_dry_run_for_intent  # noqa: WPS433
    from shared.auto_submit_policy import maybe_auto_submit  # noqa: WPS433
    # 2026-02-20: track whether maybe_auto_submit was actually
    # entered. If the chain raises *before* maybe_auto_submit writes
    # its own audit row, we'd previously lose the intent into the
    # "Never submitted (no audit row)" black hole. Now we write a
    # catch-all `auto_submit_failed` row with the exception so the
    # post-mortem panel surfaces the leak with diagnostic detail.
    submit_attempted = False
    try:
        await run_dry_run_for_intent(intent_id, 10.0, actor=actor)
        submit_attempted = True
        await maybe_auto_submit(intent_id)
    except Exception as e:  # noqa: BLE001
        # 2026-02-20: capture STRUCTURED receipt instead of just the
        # `repr(e)` blob. Operator can now group the 61 `internal_error`
        # rows by `exception_type` and see which Python error is
        # actually killing trades.
        from shared.auto_submit_receipt import build_receipt
        receipt = build_receipt(
            intent_id,
            stage=("post_dry_run" if submit_attempted else "in_dry_run"),
            exc=e,
        )
        # 2026-06-22 (P0): bump the chain-failure log line to
        # `exception()` so the FULL traceback lands in the supervisor
        # log alongside the structured Mongo receipt. Prod has been
        # bleeding `TypeError: cannot unpack non-iterable coroutine
        # object` for days without a single stack frame to anchor the
        # fix — the audit row carried it but operators only see the
        # rolled-up label in the post-mortem panel. With this, the
        # very next occurrence drops a full traceback into
        # `/var/log/supervisor/backend.err.log` for instant triage.
        logger.exception(
            "auto_submit chain failed intent=%s stage=%s type=%s msg=%s",
            intent_id, receipt.stage, receipt.exception_type, receipt.exception_message,
        )
        # Only write the catch-all when the failure happened *outside*
        # maybe_auto_submit's own audit envelope. If submit_attempted
        # is True the failure was inside maybe_auto_submit, which has
        # its own internal try/except writing `auto_submit_failed`.
        # But if it raised after maybe_auto_submit had a chance to
        # bubble its own pre-audit exception, we still want a row —
        # `kind=auto_submit_failed` is idempotent in spirit (one
        # latest row per intent wins in the post-mortem aggregator).
        try:
            from db import db as _db  # noqa: WPS433
            from namespaces import SHARED_GATE_RESULTS  # noqa: WPS433
            await _db[SHARED_GATE_RESULTS].insert_one(
                receipt.to_row(
                    kind="auto_submit_failed",
                    skip_category="internal_error",
                    actor="auto_submit_tier_1",
                ) | {"phase": receipt.stage}
            )
        except Exception:  # noqa: BLE001
            # last-ditch — if even the audit write fails, we accept
            # the loss; logs still carry the exception.
            pass



# ─────────────────────────────── schema ───────────────────────────────

class IntentIn(BaseModel):
    """Brain → MC. Subset of fields. MC fills the rest."""

    stack: Literal["camino", "barracuda", "hellcat", "gto"]
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

    # ─── Risk/Reward fields (2026-05-27, Phase A — equity-only) ───
    # When provided on a BUY/SHORT equity intent, MC's `rr_gate`
    # enforces a 3:1 reward-to-risk floor. Phase A is fail-SOFT when
    # either field is missing (intent passes with a typed warning so
    # brain teams have a rollout window). Phase B will flip the
    # missing-fields case to fail-CLOSED via the `RR_REQUIRE_FIELDS_HARD`
    # env. The 3:1 ratio enforcement itself is HARD from day one.
    #
    #   BUY (long):   target_price MUST be > entry; stop_price MUST be < entry
    #   SHORT:        target_price MUST be < entry; stop_price MUST be > entry
    # Incoherent prices (target on the wrong side of entry) are a
    # HARD REJECT even in Phase A — that's a broken intent, not a
    # configuration gap.
    target_price: Optional[float] = Field(default=None, gt=0)
    stop_price: Optional[float] = Field(default=None, gt=0)

    # ─── Memory modulator hook (2026-05-24) ───
    # Numeric per-bar signal vector. When present on a BUY/SHORT intent,
    # MC's memory_modulator computes cosine similarity vs the brain's
    # prior memories for the same symbol and nudges `confidence` by
    # ∈ [-0.25, +0.10]. When absent or for HOLD/non-directional intents,
    # no modulation runs. Optional for backward-compat.
    features: Optional[dict[str, float]] = None

    # ─── Brain-supplied modulator receipt (2026-05-25, doctrine bound) ───
    # Brain-side modulator (per the operator-locked spec sent to all
    # four brains) computes a confidence nudge locally and ships the
    # receipt here. MC's invariants:
    #   1. `value` (alias `modulator`) MUST be ∈ [-0.25, +0.10].
    #      Any out-of-bound value is a HARD REJECT (422). MC does NOT
    #      silently clamp — a brain that ships out-of-bound is buggy
    #      and the operator must see the violation.
    #   2. When the brain ships a receipt, MC TRUSTS the brain's
    #      already-modulated `confidence` field and does NOT recompute
    #      server-side (no double-application).
    #   3. MC stamps the receipt onto the intent's audit row verbatim
    #      so the operator can replay why confidence moved.
    memory_modulator: Optional[dict] = Field(default=None)

    @field_validator("memory_modulator")
    @classmethod
    def _memory_modulator_bounded(cls, v):
        """Doctrine: brain-supplied modulator `value` must be in
        [-0.25, +0.10] — the spec the operator pinned for all four
        brains. Out-of-bound = hard 422. Both `value` and the legacy
        `modulator` alias are honored."""
        if v is None:
            return None
        if not isinstance(v, dict):
            raise ValueError("memory_modulator must be an object")
        if "value" not in v and "modulator" not in v:
            raise ValueError(
                "memory_modulator must include a numeric `value` "
                "(or legacy alias `modulator`)"
            )
        raw = v.get("value", v.get("modulator"))
        try:
            num = float(raw)
        except (TypeError, ValueError):
            raise ValueError(
                f"memory_modulator.value must be numeric, got {raw!r}"
            )
        if num < -0.25 or num > 0.10:
            raise ValueError(
                f"memory_modulator.value={num} out of doctrine bounds "
                f"[-0.25, +0.10]"
            )
        # Cap size: the receipt is for audit, not a smuggling channel.
        import json
        if len(json.dumps(v, default=str)) > 4 * 1024:
            raise ValueError("memory_modulator must be ≤4 KB serialized")
        # Normalize: always carry the canonical `value` key.
        v.setdefault("value", num)
        return v

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

    # ─── Broker route override (2026-06-10, operator-pinned) ───
    # Optional. When set to a member of `ROUTE_OVERRIDE_BROKERS`
    # (currently just "webull"), the broker_router redirects THIS
    # single intent through the named broker instead of the lane
    # default (Public.com for equity, Kraken for crypto). Any other
    # value is rejected at the pydantic boundary — the override exists
    # to opt INTO a parallel broker, not to redirect to the lane
    # defaults arbitrarily. See `broker_router.py` and
    # `shared/broker/webull_caps.py` for the gate chain.
    broker_override: Optional[Literal["webull"]] = Field(default=None)

    # ─── Paradox v3 envelope (2026-02, Step 1 of rollout — ADDITIVE) ──
    # Operator-approved PRD §3 schema. v3 brains emit `intent_version
    # = "v3"` plus a `plan{}` block (planning artefact, decoupled from
    # the order ticket) and an optional `execution{}` block (derived
    # from the plan when trigger conditions fire). v2 emitters leave
    # all three fields null — backward-compat is guaranteed and the
    # read-side lifter (`shared.intent_envelope_v3.normalize_intent`)
    # synthesizes the same shape from v2 docs on read.
    #
    # No brain emits v3 yet. This is rail-only until Step 3 lands
    # `trigger_watcher.py` and Step 4 flips the first brain to v3.
    intent_version: Optional[Literal["v2", "v3"]] = Field(default=None)
    plan: Optional[PlanBlock] = Field(default=None)
    execution: Optional[ExecutionBlock] = Field(default=None)


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

    Storage-tightening 2026-05-26:
      The rejection used to write a full intent doc (rationale up to
      4 KB, evidence, regime_fp, etc.) into `shared_intents`. With
      muted brains generating tens of thousands of rejections per week
      this dominated storage growth. We now write a SLIM rejection row
      (intent_id + identifiers + gate_state + timestamps — ~250 B),
      and rely on mc_shelly for the rich provenance.

    Two writes (unchanged surface, leaner payload):
      1. mc_shelly — keeps the rejection in the training-data substrate
         alongside successful emissions. Same `event_type` prefix as
         normal intent ingest so feeds pick it up automatically.
      2. shared_intents — slim 'rejected_at_ingest' row so the existing
         Intents UI / confidence-floor sweep / diagnostic counters keep
         working. Carries `gate_state='rejected_at_ingest'`,
         `executed=False`, `may_execute=False`. FAILS-CLOSED: row
         exists for audit only, gate chain refuses to execute it.
    """
    import uuid as _uuid  # noqa: WPS433
    rejection_id = f"rejected-{_uuid.uuid4().hex}"
    now = _now_iso()
    # Slim row: only the fields downstream readers actually consume
    # (confidence_floor_sweep, brain_emission_diagnose, intent_inspect,
    # operator UI badge). Rationale is truncated to 240 chars — full
    # rationale lives on mc_shelly below.
    rationale_stub = (rationale or "")[:240]
    slim_doc = {
        "intent_id": rejection_id,
        "stack": stack,
        "stack_canonical": canonicalize_stack(stack),  # 2026-02-23 dual-field
        "action": action,
        "symbol": symbol,
        "lane": lane,
        "lane_source": "brain",
        "confidence": float(confidence),
        # ── gate-state: rejected before any gate ran ──
        "gate_state": "rejected_at_ingest",
        "rejected_reason": "brain_lane_policy",
        "rejected_policy": "brain_lane_policy",
        "may_execute": False,
        "requires_gate_pass": False,
        "executed": False,
        # ── audit ──
        "ingest_ts": now,
        "ingest_method": ingest_method,
        "ingest_admin_email": admin_email,
        "audit_only": True,
        "rationale_stub": rationale_stub,
        "slim_v": 2,  # 2026-05-26 storage-tightening marker
    }
    try:
        await db[SHARED_INTENTS].insert_one(slim_doc)
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


def _fractional_default_for_lane(
    lane: Optional[str], symbol: Optional[str],
) -> bool:
    """Deterministic default for `snapshot["fractional_supported"]`.

    Doctrine pin (operator, 2026-02-20):
        Removing operator-dependent failure modes is more important
        than getting the flag "right" for some edge symbol. We hard-
        code the broker-capability table here so a brain runtime
        that forgets to set the flag doesn't silently lose:
          * +0.05 FRACTIONAL_SUPPORTED label
          * baseline-only toehold path
          * large-caps going quiet again

    Rule table:
        equity     → True (Webull's fractional-eligible US equity/ETF
                     universe; specific ineligible tickers are
                     enforced at the seat layer via the
                     `WEBULL_FRACTIONAL_INELIGIBLE_SYMBOLS` blacklist
                     env var — not here, to keep doctrine layer
                     broker-agnostic).
        crypto     → True (Kraken supports fractional natively for
                     every USD pair).
        else       → False (unknown lane; conservative default).

    Operator override: `RISEDUAL_DISABLE_FRACTIONAL_AUTOFILL=true`
    forces every snapshot to False. Use during a broker outage or
    when the operator wants to force whole-share fallback testing.
    """
    import os  # local import keeps module-load lean
    if (os.environ.get("RISEDUAL_DISABLE_FRACTIONAL_AUTOFILL") or "").strip().lower() in {
        "true", "1", "yes", "on",
    }:
        return False
    lane_norm = (lane or "").lower()
    if lane_norm == "equity":
        return True
    if lane_norm == "crypto":
        return True
    return False


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
    intent_version: Optional[str] = None,
    plan_execution_style: Optional[str] = None,
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
    # 2026-02-20: fractional-trading capability is a DETERMINISTIC
    # property of the broker + lane combination, not something a
    # brain should have to remember to set on every snapshot. We
    # auto-fill here so the doctrine layer's `FRACTIONAL_SUPPORTED`
    # label + BASELINE_ONLY_TOEHOLD path never silently de-activate
    # because someone forgot to plumb the flag. Operator can flip
    # OFF via env if a broker outage forces whole-share fallback.
    if "fractional_supported" not in merged:
        merged["fractional_supported"] = _fractional_default_for_lane(
            merged.get("lane"), merged.get("symbol"),
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
        "stack": stack,                           # METADATA: ingest brain (raw)
        "stack_canonical": canonicalize_stack(stack),  # 2026-02-23 dual-field
        "lane": lane_norm or None,
        "symbol": symbol,
        "action": action,
        "ingest_confidence": float(confidence),
        "ingest_method": ingest_method,
        "ingest_admin_email": admin_email,
        "snapshot": merged,
        "packet": packet,
        # ── Paradox v3 envelope hints (2026-02-22, Step 7) ───────────
        # Stamped so `_v3_patient_candidates` in auto_retire can
        # filter to v3 PATIENT plans only. v2 rows leave these null;
        # the slicer naturally excludes them.
        "intent_version": intent_version,
        "plan_execution_style": plan_execution_style,
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
        "execution_judge_failed_checks": hoisted.get("execution_judge_failed_checks") or [],
        "execution_judge_not_ready_reason": hoisted.get("execution_judge_not_ready_reason"),
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
    """HTTP route: verifies the per-brain runtime token then delegates
    to `_post_intent_impl`. External callers (including any future
    sidecar) MUST authenticate. The in-process brain runner uses
    `submit_intent_in_process` below to skip auth entirely.
    """
    if not x_runtime_token:
        raise HTTPException(status_code=401, detail="X-Runtime-Token required")
    verify_runtime_token(body.stack, x_runtime_token)
    return await _post_intent_impl(body, x_runtime_token=x_runtime_token)


async def submit_intent_in_process(body: IntentIn):
    """In-process direct-call entrypoint. The brain runner uses this
    so it doesn't have to authenticate to itself over loopback.

    2026-02-20 — part of the Option C refactor that removes the HTTP
    + token roundtrip for same-process callers. Token verification
    still gates the HTTP route for external integrators (none today,
    but the surface stays defended).
    """
    return await _post_intent_impl(body, x_runtime_token=None)


async def _post_intent_impl(
    body: IntentIn,
    x_runtime_token: Optional[str] = None,
):
    """The actual intent-submit logic. Reached two ways: via the
    HTTP route (after auth) or via `submit_intent_in_process`.

    `x_runtime_token` is threaded through because the legacy
    `close_position` flow re-uses it; for in-process callers it
    will be None, and `close_position` handles that.
    """
    # ─── Symbol normalization (2026-02-19) ──────────────────────────
    # Strip any over-prefixed canonical form ("EQ:AAPL",
    # "CRYPTO:BTC-USD") down to the bare ticker BEFORE any downstream
    # code reads `body.symbol`. The system stores the bare form on
    # the intent row and stamps the prefixed form on `canonical` —
    # mixing the two shapes silently breaks the
    # `symbol_in_universe` gate and any per-symbol cap lookup.
    from shared.broker_symbol_resolver import _strip_canonical_prefix  # noqa: WPS433
    if body.symbol:
        body.symbol = _strip_canonical_prefix(body.symbol)

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
        # `CloseIn` lives in shared/position_close_models.py to keep
        # the import boundary clean; `close_position` itself is the
        # route handler so it's late-imported (the runtime delegation
        # is intentional and mutual — see that module's docstring).
        from routes.runtime_position_close import close_position  # noqa: WPS433
        from shared.position_close_models import CloseIn  # noqa: WPS433
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
    #
    # Schema-tightening 2026-05-25:
    #   If the brain shipped a `memory_modulator` receipt on the
    #   envelope, MC trusts the brain's already-modulated `confidence`
    #   (the IntentIn validator already enforced `value ∈
    #   [-0.25, +0.10]`). MC stamps the receipt verbatim and skips its
    #   own compute — single source of modulation, no double-apply.
    memory_modulator_info: Optional[dict] = None
    if body.memory_modulator is not None:
        # Brain-supplied path: bounds already validated by Pydantic.
        # Stamp `source="brain"` so the operator can tell brain-side
        # modulation from MC-side at a glance.
        memory_modulator_info = dict(body.memory_modulator)
        memory_modulator_info.setdefault("source", "brain")
        memory_modulator_info["mc_validated"] = True
        memory_modulator_info["mc_bounds"] = {"min": -0.25, "max": 0.10}
    elif body.action in {"BUY", "SHORT"} and body.features:
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
            memory_modulator_info["source"] = "mc"
        except Exception as e:  # noqa: BLE001
            # Fail-OPEN: a modulator error must NEVER block an intent.
            # Log + record skip; downstream gates run on unmodulated confidence.
            logger.warning("memory_modulator: error %r — skipping nudge", e)
            memory_modulator_info = {
                "modulator": 0.0, "skipped": True,
                "reason": f"modulator error: {e!r}",
                "source": "mc",
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
        intent_version=body.intent_version,
        plan_execution_style=(body.plan.execution_style if body.plan else None),
    )

    # ─── Spread-bps enrichment (2026-05-26) ───
    # Doctrine: brains SHOULD ship `spread_bps` in `doctrine_snapshot`.
    # When they don't (Camaro, historically), MC walks a fallback
    # ladder: brain → derive(bid,ask) → indicator cache → kraken
    # public (crypto, opt-in) → sentinel. Result is stamped on the
    # persisted `snapshot` field so RoadGuard reads a value rather
    # than failing with ROADGUARD_MISSING_SPREAD_BPS.
    from shared.market_data import enrich_snapshot_spread  # noqa: WPS433
    enriched_snapshot, spread_diag = await enrich_snapshot_spread(
        dict(body.doctrine_snapshot or {}),
        symbol=body.symbol, lane=effective_lane,
    )

    doc = {
        "intent_id": intent_id,
        "stack": body.stack,
        "stack_canonical": canonicalize_stack(body.stack),  # 2026-02-23 dual-field
        "action": body.action,
        "symbol": body.symbol,
        "lane": effective_lane,
        "lane_source": "brain" if (body.lane is not None) else ("inferred" if effective_lane else "unset"),
        "inferred_lane": inferred_lane,
        "canonical": canonical,
        "confidence": float(body.confidence),
        "risk_multiplier": float(body.risk_multiplier),
        "rationale": body.rationale,
        # Phase A R:R fields — used by `shared.rr_gate.evaluate_rr` in
        # the gate chain. Optional today; brains rolling out can ship
        # `target_price` + `stop_price` to engage the 3:1 floor. Phase B
        # will require both on equity entries.
        "target_price": body.target_price,
        "stop_price": body.stop_price,
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
        # may read more) can find it on the intent doc. The brain's
        # `doctrine_snapshot` has been MC-enriched (2026-05-26) so
        # `spread_bps` is always populated — either by the brain, by
        # MC derivation from bid/ask, MC's indicator cache, the
        # optional Kraken public ticker (crypto), or the explicit
        # sentinel `SPREAD_BPS_UNKNOWN=9999.0`. The provenance lives
        # in `snapshot.spread_source` and `spread_enrichment_diagnostics`.
        "snapshot": enriched_snapshot,
        "spread_enrichment_diagnostics": spread_diag,
        # LIFECYCLE
        "gate_state": "pending",   # pending | passed | blocked | dry_run_passed | dry_run_blocked
        "executed": False,
        "executed_at": None,
        "execution_receipt_id": None,
        # Broker route override (None → lane-default broker; "webull"
        # → opts INTO the Webull parallel route, capped at $3-$10
        # per ticker by `evaluate_webull_order` BEFORE submission).
        "broker_override": body.broker_override,
        # ─── Paradox v3 envelope (Step 1 — stamps if brain sent v3) ───
        # When the brain emits v3, the discriminator + the two blocks
        # ride alongside the legacy fields. When the brain still emits
        # v2, `intent_version` lands as "v2" (so the lifter doesn't
        # have to infer from missing keys) and the two blocks remain
        # null. The lifter synthesises them on read for legacy rows.
        "intent_version": (body.intent_version or "v2"),
        "plan": (body.plan.model_dump() if body.plan is not None else None),
        "execution": (body.execution.model_dump() if body.execution is not None else None),
    }
    if memory_modulator_info is not None:
        doc["memory_modulator"] = memory_modulator_info
    # 2026-02-20: stamp Research Layer evidence onto the canonical
    # runtime ingest. Best-effort (helper internally try/excepts the
    # bar source) so production brain emissions never block on a
    # research outage. This is the doctrine-enforcement point that
    # makes admin-bridge intents and runtime-token intents carry the
    # same `evidence.research_signals` shape — comparable apples to
    # apples in the post-mortem.
    try:
        from shared.research.intent_evidence import attach_research_evidence
        await attach_research_evidence(doc)
    except Exception as _research_err:  # noqa: BLE001
        logger.warning(
            "_post_intent_impl: research evidence attach failed intent_id=%s err=%s",
            intent_id, _research_err,
        )
    # 2026-02-20: setup memory feedback loop. Reads `(brain, setup_id)`
    # report-card history and adjusts `confidence` accordingly. Kill
    # switch on `runtime_flags.setup_memory_enabled` — default OFF
    # so operator can review the report cards before letting them
    # actually pull brain confidence. Stamps the audit trail on
    # `evidence.setup_memory` either way.
    try:
        from shared.setup_memory import apply_setup_memory
        await apply_setup_memory(doc)
    except Exception as _sm_err:  # noqa: BLE001
        logger.warning(
            "_post_intent_impl: setup_memory apply failed intent_id=%s err=%s",
            intent_id, _sm_err,
        )
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

    # Auto-dry-run hook (2026-05-27). Fire-and-forget so the brain's
    # POST returns immediately; the gate verdict lands ~50ms later.
    # Env-gated via AUTO_DRY_RUN_ON_INGEST (default ON).
    await _fire_and_forget_dry_run(intent_id, actor="auto_dry_run:runtime_token")

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
    request: Request,
    stack: Optional[str] = Query(default=None),
    symbol: Optional[str] = Query(default=None),
    lane: Optional[Literal["equity", "crypto"]] = Query(default=None),
    gate_state: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    # 2026-02-23 — operator queue improvements
    sort: Literal[
        "conviction", "execution_priority", "newest", "symbol",
    ] = Query(default="conviction"),
    include_disabled_lanes: bool = Query(default=False),
    x_runtime_token: Optional[str] = Header(default=None, alias="X-Runtime-Token"),
):
    """Read recent intents. Accepts either:
      * an operator JWT (Authorization: Bearer <token> or cookie), or
      * any runtime token (brains can read each other's intents for
        council-context purposes — same doctrine as opinions).

    2026-02-21: The frontend now uses admin JWT only — the legacy
    `X-Runtime-Token: alpha-ingest-...` header was a leftover from the
    sidecar HTTP architecture (since deleted). We accept it for any
    brain still calling this endpoint, but operator JWT is the canonical
    path now.

    2026-02-23: Two operator-queue improvements.

      `sort` (default 'conviction')
        * conviction          — highest confidence first (operator's
                                strongest ideas surface first)
        * execution_priority  — BUY/SELL passed → BUY/SELL blocked
                                → HOLD/WATCH (closest-to-execution first)
        * newest              — historical ingest_ts DESC behavior
        * symbol              — alphabetical (escape hatch for ticker
                                lookup, NOT recommended as default)

      `include_disabled_lanes` (default False)
        When a lane has execution toggled OFF, the brains may still
        emit observation/advisor intents on that lane. Hiding them
        from the default queue keeps the operator's actionable view
        clean while the lane is paused. Set to True to inspect
        them for QA/forensics.
    """
    # Try operator JWT first (cookie or bearer); fall back to runtime token.
    try:
        await get_current_user(request)
        authed = True
    except HTTPException:
        authed = False

    if not authed:
        if not x_runtime_token:
            raise HTTPException(
                status_code=401,
                detail="operator JWT or X-Runtime-Token required",
            )
        for rt in RUNTIMES:
            try:
                verify_runtime_token(rt, x_runtime_token)
                authed = True
                break
            except HTTPException:
                continue
        if not authed:
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

    # 2026-02-23 disabled-lane filter. When `include_disabled_lanes`
    # is False (the operator-friendly default), restrict the result
    # to intents whose lane has execution currently ON. Crypto stays
    # observable to the brains but hidden from the actionable queue
    # while the operator has crypto paused.
    enabled_lanes: list[str] = []
    if not include_disabled_lanes:
        # Lazy import to avoid lifespan-time circular reference.
        from shared.lane_execution import is_lane_execution_enabled  # noqa: WPS433
        for L in ("equity", "crypto"):
            if await is_lane_execution_enabled(L):
                enabled_lanes.append(L)
        if enabled_lanes:
            # Restrict to enabled lanes (intersect with any caller-
            # supplied `lane` filter — if the caller asked for a
            # disabled lane explicitly, return nothing rather than
            # silently widening their query).
            if "lane" in q:
                if q["lane"] not in enabled_lanes:
                    return {
                        "items": [], "count": 0,
                        "sort": sort,
                        "enabled_lanes": enabled_lanes,
                        "include_disabled_lanes": False,
                        "note": (
                            f"requested lane={q['lane']!r} has execution "
                            f"disabled — returning empty. Pass "
                            f"include_disabled_lanes=true to inspect."
                        ),
                    }
            else:
                q["lane"] = {"$in": enabled_lanes}
        else:
            # All lanes disabled — return empty rather than show
            # everything as if nothing changed.
            return {
                "items": [], "count": 0,
                "sort": sort,
                "enabled_lanes": [],
                "include_disabled_lanes": False,
                "note": "All lanes disabled — pass include_disabled_lanes=true to view observation intents.",
            }

    # ── Sort selection ─────────────────────────────────────────────
    # Note on Mongo sort tuples: `("confidence", -1)` is descending.
    # The `execution_priority` rank below is computed via a small
    # `$addFields` so we can sort on a derived field cleanly.
    sort_spec: list[tuple[str, int]]
    if sort == "newest":
        sort_spec = [("ingest_ts", -1)]
    elif sort == "symbol":
        sort_spec = [("symbol", 1), ("ingest_ts", -1)]
    elif sort == "conviction":
        # Highest confidence first; tie-break by recency so two equally-
        # confident intents on the same symbol show the freshest one.
        sort_spec = [("confidence", -1), ("ingest_ts", -1)]
    else:  # execution_priority
        # We need a derived rank. Run as a tiny aggregation pipeline
        # so the priority field doesn't pollute the response shape.
        pipeline = [
            {"$match": q},
            {"$addFields": {
                "_exec_rank": {
                    "$switch": {
                        "branches": [
                            # 0 = passed/executable directional → top
                            {"case": {"$and": [
                                {"$in": ["$action", ["BUY", "SELL"]]},
                                {"$eq": ["$gate_state", "dry_run_passed"]},
                            ]}, "then": 0},
                            {"case": {"$and": [
                                {"$in": ["$action", ["BUY", "SELL"]]},
                                {"$eq": ["$gate_state", "passed"]},
                            ]}, "then": 1},
                            # 2 = directional but blocked → fix-it bucket
                            {"case": {"$and": [
                                {"$in": ["$action", ["BUY", "SELL"]]},
                                {"$in": ["$gate_state", ["dry_run_blocked", "blocked"]]},
                            ]}, "then": 2},
                            # 3 = directional pending dry-run
                            {"case": {"$in": ["$action", ["BUY", "SELL"]]}, "then": 3},
                            # 4 = HOLD/WATCH (informational) at the bottom
                        ],
                        "default": 4,
                    },
                },
            }},
            {"$sort": {"_exec_rank": 1, "confidence": -1, "ingest_ts": -1}},
            {"$limit": limit},
            {"$project": {"_id": 0, "_exec_rank": 0}},
        ]
        rows = await db[SHARED_INTENTS].aggregate(pipeline).to_list(None)
        return {
            "items": rows, "count": len(rows),
            "sort": sort,
            "enabled_lanes": enabled_lanes,
            "include_disabled_lanes": bool(include_disabled_lanes),
        }

    rows = await db[SHARED_INTENTS].find(q, {"_id": 0}).sort(sort_spec).to_list(limit)
    return {
        "items": rows, "count": len(rows),
        "sort": sort,
        "enabled_lanes": enabled_lanes,
        "include_disabled_lanes": bool(include_disabled_lanes),
    }


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
    # Symbol normalization — same contract as POST /intents. Operator
    # injections frequently carry the already-canonical form
    # ("EQ:AAPL", "CRYPTO:BTC-USD"); strip it so the persisted intent
    # row carries the bare ticker and downstream gates match.
    from shared.broker_symbol_resolver import _strip_canonical_prefix  # noqa: WPS433
    if body.symbol:
        body.symbol = _strip_canonical_prefix(body.symbol)

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
        intent_version=body.intent_version,
        plan_execution_style=(body.plan.execution_style if body.plan else None),
    )

    # Spread-bps enrichment ladder — same doctrine as runtime path.
    from shared.market_data import enrich_snapshot_spread  # noqa: WPS433
    enriched_snapshot, spread_diag = await enrich_snapshot_spread(
        dict(body.doctrine_snapshot or {}),
        symbol=body.symbol, lane=effective_lane,
    )

    doc = {
        "intent_id": intent_id,
        "stack": body.stack,
        "stack_canonical": canonicalize_stack(body.stack),  # 2026-02-23 dual-field
        "action": body.action,
        "symbol": body.symbol,
        "lane": effective_lane,
        "lane_source": "brain" if (body.lane is not None) else ("inferred" if effective_lane else "unset"),
        "inferred_lane": inferred_lane,
        "canonical": canonical,
        "confidence": float(body.confidence),
        "risk_multiplier": float(body.risk_multiplier),
        "rationale": body.rationale,
        # Phase A R:R fields — used by `shared.rr_gate.evaluate_rr` in
        # the gate chain. Optional today; brains rolling out can ship
        # `target_price` + `stop_price` to engage the 3:1 floor. Phase B
        # will require both on equity entries.
        "target_price": body.target_price,
        "stop_price": body.stop_price,
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
        # brain's `doctrine_snapshot` is enriched with `spread_bps`
        # via MC's fallback ladder so RoadGuard reads a value rather
        # than failing with ROADGUARD_MISSING_SPREAD_BPS.
        "snapshot": enriched_snapshot,
        "spread_enrichment_diagnostics": spread_diag,
        "gate_state": "pending",
        "executed": False,
        "executed_at": None,
        "execution_receipt_id": None,
        # Broker route override — same semantics as the runtime path.
        "broker_override": body.broker_override,
        # ─── Paradox v3 envelope (Step 1 — admin proxy mirror) ───
        # Mirrors the runtime path so admin-replayed intents carry the
        # same v3 shape as runtime-emitted intents. The lifter doesn't
        # know which ingest channel produced a doc; consistency here
        # is what keeps the post-mortem comparing apples to apples.
        "intent_version": (body.intent_version or "v2"),
        "plan": (body.plan.model_dump() if body.plan is not None else None),
        "execution": (body.execution.model_dump() if body.execution is not None else None),
    }
    # Brain-supplied modulator receipt (bounds already validated by
    # IntentIn). Persisted on the admin path the same as the runtime
    # path so admin replays carry the same provenance trail.
    if body.memory_modulator is not None:
        receipt = dict(body.memory_modulator)
        receipt.setdefault("source", "brain")
        receipt["mc_validated"] = True
        receipt["mc_bounds"] = {"min": -0.25, "max": 0.10}
        doc["memory_modulator"] = receipt
    # 2026-02-20: same research evidence hook as the runtime path
    # (`_post_intent_impl`). Admin-proxied intents go through this
    # branch; keep them shape-compatible with runtime intents so the
    # post-mortem and the bridge-health tile compare apples to apples.
    try:
        from shared.research.intent_evidence import attach_research_evidence
        await attach_research_evidence(doc)
    except Exception as _research_err:  # noqa: BLE001
        logger.warning(
            "admin_post_intent: research evidence attach failed intent_id=%s err=%s",
            doc.get("intent_id"), _research_err,
        )
    # 2026-02-20: setup memory feedback loop. Same hook as the runtime
    # path. Kill switch on `runtime_flags.setup_memory_enabled`.
    try:
        from shared.setup_memory import apply_setup_memory
        await apply_setup_memory(doc)
    except Exception as _sm_err:  # noqa: BLE001
        logger.warning(
            "admin_post_intent: setup_memory apply failed intent_id=%s err=%s",
            doc.get("intent_id"), _sm_err,
        )
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

    # Auto-dry-run hook (2026-05-27). Fire-and-forget so the admin
    # response returns immediately; verdict lands shortly after.
    await _fire_and_forget_dry_run(intent_id, actor=f"auto_dry_run:admin_proxy:{user.get('email','operator')}")

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



# ─── Position-model resurrection (2026-05-31) ─────────────────────────
# Operator-only one-shot to clean up the audit damage from the OLD
# brain-coupled sweep. The pre-fix auto-router would terminally block
# every intent where the poster did not hold the executor seat at
# post-time, even when a different brain now holds the seat (which is
# perfectly valid under the position-model gate).
#
# This endpoint finds those intents — `gate_state=blocked` AND only
# failing gate is `executor_seat_check` with the legacy brain-coupled
# reason — and flips them back to `gate_state=pending` so the next
# auto-router tick can re-evaluate them under the new doctrine.
#
# Idempotent: if the intent has a NEW failing gate row (different
# `kind` than the legacy sweep), it stays blocked.

@router.post("/admin/intents/resurrect-position-model-victims")
async def resurrect_position_model_victims(
    dry_run: bool = Query(default=True, description="set false to actually mutate"),
    limit: int = Query(default=500, ge=1, le=5000),
    _user: dict = Depends(get_current_user),
):
    """Operator one-shot. Restores intents wrongly terminated by the
    pre-position-model auto-router sweep so they can be re-evaluated.

    Returns counts only; rows are not echoed (could be hundreds).
    """
    # Match: blocked, has at least one auto-router-sweep gate row with
    # the legacy reason. The legacy text includes "swept by auto_router
    # seat-mismatch cleanup" — that's our distinguishing marker.
    legacy_marker = "swept by auto_router seat-mismatch cleanup"

    # Find candidate intents via the gate-results audit row.
    legacy_rows = (
        await db[SHARED_GATE_RESULTS]
        .find(
            {
                "kind": "auto_router_blocked",
                "gates.reason": {"$regex": legacy_marker},
            },
            {"_id": 0, "intent_id": 1},
        )
        .to_list(limit)
    )
    candidate_ids = list({r["intent_id"] for r in legacy_rows if r.get("intent_id")})

    resurrected = 0
    skipped_still_blocked = 0
    not_blocked_anymore = 0
    for iid in candidate_ids:
        intent = await db[SHARED_INTENTS].find_one(
            {"intent_id": iid},
            {"_id": 0, "intent_id": 1, "gate_state": 1, "executed": 1},
        )
        if not intent or intent.get("executed"):
            continue
        if intent.get("gate_state") != "blocked":
            not_blocked_anymore += 1
            continue
        # Confirm the LATEST gate-result row for this intent is the legacy
        # one (not a fresh block with a different reason that would still
        # apply). If it has a newer non-legacy block, leave it alone.
        latest = await db[SHARED_GATE_RESULTS].find_one(
            {"intent_id": iid},
            {"_id": 0, "gates": 1, "kind": 1, "ts": 1},
            sort=[("ts", -1)],
        )
        if not latest:
            continue
        gates = latest.get("gates") or []
        is_legacy = any(
            (g.get("reason") or "").find(legacy_marker) >= 0 and g.get("passed") is False
            for g in gates
        )
        if not is_legacy:
            skipped_still_blocked += 1
            continue
        if not dry_run:
            await db[SHARED_INTENTS].update_one(
                {"intent_id": iid},
                {
                    "$set": {
                        "gate_state": "pending",
                        "resurrected_by": "position_model_cleanup",
                        "resurrected_at": _now_iso(),
                    },
                    "$unset": {"last_block_reason": ""},
                },
            )
        resurrected += 1

    return {
        "dry_run": dry_run,
        "candidates_found": len(candidate_ids),
        "resurrected": resurrected,
        "skipped_blocked_by_other_reason": skipped_still_blocked,
        "no_longer_blocked": not_blocked_anymore,
        "next_step": (
            "Set dry_run=false to actually flip the intents back to pending. "
            "The next auto-router tick will re-run the gate chain under the "
            "current position-model doctrine."
        ),
    }

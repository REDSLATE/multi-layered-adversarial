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


router = APIRouter(tags=["intents"])

# Strict action vocabulary — extend deliberately.
ACTIONS = ("BUY", "SELL", "SHORT", "COVER", "HOLD")


# ─────────────────────────────── schema ───────────────────────────────

class IntentIn(BaseModel):
    """Brain → MC. Subset of fields. MC fills the rest."""

    stack: Literal["alpha", "camaro", "chevelle", "redeye"]
    action: Literal["BUY", "SELL", "SHORT", "COVER", "HOLD"]
    symbol: str = Field(min_length=1, max_length=24)
    # Lane is the brain's declared asset class for this intent. MC uses
    # it to compose the canonical asset key and pick the broker. Missing
    # lane = NO_TRADE (fail-closed at the resolver).
    lane: Optional[Literal["equity", "crypto"]] = Field(default=None)
    confidence: float = Field(ge=0.0, le=1.0)
    risk_multiplier: float = Field(ge=0.0, le=1.0, default=0.0)
    rationale: str = Field(min_length=1, max_length=4000)

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
            from shared.hypothesis import REGIME_FP_KEYS  # noqa: WPS433
            extra = set(rfp.keys()) - set(REGIME_FP_KEYS)
            if extra:
                raise ValueError(
                    f"evidence.regime_fp has unknown keys: {sorted(extra)}. "
                    f"Allowed: {sorted(REGIME_FP_KEYS)}"
                )
        return v


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    from shared.hypothesis import REGIME_FP_KEYS, _regime_fingerprint  # noqa: WPS433
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


def _looks_like_crypto(symbol: str) -> bool:
    """Heuristic: does `symbol` unambiguously look like a crypto pair?

    Matches the common shapes Kraken / Camaro emit:
      - BTC/USD, ETH/USDT, SOL/USD, BNB-USD, BTC-USDT
      - XBTUSD (Kraken's BTC alias), pairs with USD/USDT/USDC suffixes
    We deliberately do NOT match bare 3-letter tickers like "BTC" or "ETH"
    — too easy to collide with equity symbols, and a real ambiguity
    case the operator should resolve.
    """
    if not symbol or not isinstance(symbol, str):
        return False
    s = symbol.upper().strip()
    # Hard rule: must contain a separator OR be a known fused pair shape.
    if "/" in s or "-" in s:
        # Probably a pair like BTC/USD or BTC-USDT. Trust it as crypto if
        # the quote side looks like a fiat / stablecoin.
        sep = "/" if "/" in s else "-"
        parts = s.split(sep)
        if len(parts) != 2:
            return False
        _, quote = parts
        return quote in {"USD", "USDT", "USDC", "EUR", "GBP", "JPY", "BTC", "ETH"}
    # Kraken-style fused pairs (XBTUSD, BTCUSDT, ETHUSD). Require >=6 chars
    # and a known suffix to avoid colliding with NYSE 4-5 letter tickers.
    for suffix in ("USDT", "USDC", "USD"):
        if len(s) >= len(suffix) + 3 and s.endswith(suffix):
            base = s[: -len(suffix)]
            # Common crypto base symbols. Anything else stays ambiguous.
            if base in {
                "BTC", "XBT", "ETH", "SOL", "BNB", "DOGE", "ADA", "AVAX",
                "MATIC", "DOT", "LTC", "LINK", "UNI", "ATOM", "TRX", "XRP",
                "XLM", "ETC", "FIL", "NEAR", "ARB", "OP", "INJ", "TIA",
            }:
                return True
    return False


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

    # Per-brain × lane emission policy (2026-02-16). Reject at ingest
    # before we burn a uuid or write anything. Camaro→crypto is muted
    # by default (see brain_lane_policy.seed_default_policy).
    effective_lane, canonical, inferred_lane = _compose_canonical(body.symbol, body.lane)
    from shared.brain_lane_policy import is_brain_lane_allowed  # noqa: WPS433
    if not await is_brain_lane_allowed(body.stack, effective_lane):
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
        # LIFECYCLE
        "gate_state": "pending",   # pending | passed | blocked | dry_run_passed | dry_run_blocked
        "executed": False,
        "executed_at": None,
        "execution_receipt_id": None,
    }
    await db[SHARED_INTENTS].insert_one(doc)

    # MC Shelly — record this ingest in MC's memory, tagged with the
    # full position snapshot at the moment of ingest. Fire-and-forget
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
    }


@router.get("/intents")
async def list_intents(
    stack: Optional[str] = Query(default=None),
    symbol: Optional[str] = Query(default=None),
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
    }


# NOTE: `/execution/dry_run` and `/execution/submit` live in
# `shared/execution.py` — they consume the full gate chain including
# real exposure caps and the live Alpaca paper adapter.

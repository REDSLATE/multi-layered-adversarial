"""Paradox v3 Intent Envelope — schema + read-side lifter.

Doctrine pin (operator, 2026-02 PRD approval, decisions §11 + §8):
  * v2 intents (`action: BUY|SELL|SHORT|COVER|HOLD|OPEN|CLOSE`) are
    EXISTING IP. They stay valid. Old emitters continue to work.
  * v3 emits add `intent_version="v3"` plus `plan{}` and `execution{}`
    blocks. `action` becomes execution-layer only.
  * `normalize_intent(doc)` is the SINGLE place that knows about the
    version difference on read. Every read-path consumer (funnel,
    post-mortem, verifier, frontend) calls it and gets a uniform
    v3-shaped dict back.

Step 1 of the rollout sequence (PRD §7). This module ships the rails
ONLY. No brain emits v3 yet. No new pipeline behaviour. No new
storage. Pure additive.

Locked operator decisions baked in here:
  - 1A: `plan.target_prices` is OPTIONAL with NO doctrine penalty
        when omitted on an ENTER intent.
  - 2B: `plan.setup` is enum; a `plan.setup_custom_tag` free-string
        fallback exists for brain-specific labels that don't fit.
  - 3B: NO inner `plan_version` discriminator (YAGNI — top-level
        `intent_version` is enough until future plan-shape evolution
        demands it).
  - 4A: When `plan.ttl_seconds` is null, the verifier derives the
        plan's effective expiry from `plan.horizon` using
        `HORIZON_TTL_DEFAULTS`. Stored on the row as-null; the lifter
        does NOT eagerly back-fill (lets future verifier strategies
        change without re-migrating data).
  - 5C: WAIT plans bucket UNDER seat-blocked in the funnel — no new
        column. This module returns the normalised shape; the funnel
        route's existing "no receipt + executed=false" path puts
        WAIT-state docs at Stage 1 (Emitted only) naturally.
  - 6B: Hot-Brain Router scores EVERYTHING. We do NOT special-case
        `plan.intent IN [WAIT_*, WATCH, ABSTAIN, NO_EDGE, DEFER]`
        out of any aggregation here.
"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, Field, field_validator, model_validator


# ── Enums (kept as tuples so Literal[] can splat them) ─────────────
STANCE_VALUES: Tuple[str, ...] = (
    "BULLISH", "LONG_BIAS", "NEUTRAL",
    "SHORT_BIAS", "BEARISH", "UNCERTAIN",
)
SETUP_VALUES: Tuple[str, ...] = (
    "bull_flag", "bear_flag", "breakout", "breakdown",
    "mean_revert", "gap_fill", "range_play",
    "trend_continuation", "trend_exhaustion",
    "news_driven", "other",
)
PLAN_INTENT_VALUES: Tuple[str, ...] = (
    "ENTER", "EXIT", "SCALE_IN", "SCALE_OUT", "HEDGE",
    "WAIT_FOR_TRIGGER", "WAIT_CONFIRMATION", "DEFER",
    "WATCH", "ABSTAIN", "NO_EDGE",
)
EXECUTION_STYLE_VALUES: Tuple[str, ...] = (
    "MARKET_NOW", "LIMIT", "STOP",
    "TRIGGERED_LIMIT", "PATIENT", "SCALED",
)
SIZE_POSTURE_VALUES: Tuple[str, ...] = ("STANDARD", "REDUCED", "ELEVATED")
PORTFOLIO_POSTURE_VALUES: Tuple[str, ...] = ("RISK_ON", "NEUTRAL", "RISK_OFF")
HORIZON_VALUES: Tuple[str, ...] = ("INTRADAY", "SWING", "POSITION", "UNKNOWN")
EXECUTION_ACTION_VALUES: Tuple[str, ...] = (
    "BUY", "SELL", "SHORT", "COVER", "OPEN", "CLOSE",
)

# Operator decision 4A — TTL defaults when plan.ttl_seconds is null.
HORIZON_TTL_DEFAULTS: Dict[str, Optional[int]] = {
    "INTRADAY": 23_400,      # ~6h30m, next session close
    "SWING":    432_000,     # 5 trading days
    "POSITION": 1_728_000,   # 20 trading days
    "UNKNOWN":  None,
}


# ── Pydantic models ────────────────────────────────────────────────
class PlanBlock(BaseModel):
    """The brain's planning artifact. Decoupled from execution side."""

    stance: Literal[STANCE_VALUES] = Field(..., description="Directional read")  # type: ignore[valid-type]
    setup: Literal[SETUP_VALUES] = Field(..., description="Setup category")  # type: ignore[valid-type]
    setup_custom_tag: Optional[str] = Field(
        default=None, max_length=64,
        description="Free-string label when `setup=other` (operator 2B)",
    )
    intent: Literal[PLAN_INTENT_VALUES] = Field(..., description="What the brain wants done")  # type: ignore[valid-type]
    execution_style: Literal[EXECUTION_STYLE_VALUES] = Field(...)  # type: ignore[valid-type]
    size_posture: Literal[SIZE_POSTURE_VALUES] = Field(default="STANDARD")  # type: ignore[valid-type]
    portfolio_posture: Literal[PORTFOLIO_POSTURE_VALUES] = Field(default="NEUTRAL")  # type: ignore[valid-type]
    hedge_against_symbol: Optional[str] = Field(default=None, max_length=24)
    trigger_price: Optional[float] = Field(default=None, gt=0)
    invalidation_price: Optional[float] = Field(default=None, gt=0)
    # Operator decision 1A: optional, no doctrine penalty when absent on ENTER.
    target_prices: Optional[List[float]] = Field(default=None, max_length=8)
    confidence: float = Field(ge=0.0, le=1.0)
    thesis: str = Field(default="", max_length=4_000)
    horizon: Literal[HORIZON_VALUES] = Field(default="UNKNOWN")  # type: ignore[valid-type]
    ttl_seconds: Optional[int] = Field(default=None, ge=1, le=10_000_000)

    @field_validator("target_prices")
    @classmethod
    def _target_prices_positive(cls, v: Optional[List[float]]) -> Optional[List[float]]:
        if v is None:
            return None
        if any((t is None) or (float(t) <= 0) for t in v):
            raise ValueError("plan.target_prices must all be > 0")
        return v

    @model_validator(mode="after")
    def _hedge_requires_symbol(self):
        if self.intent == "HEDGE" and not self.hedge_against_symbol:
            raise ValueError(
                "plan.hedge_against_symbol is required when plan.intent == 'HEDGE'"
            )
        return self


class ExecutionBlock(BaseModel):
    """Derived from PlanBlock. Optional at emit time — populated when
    (and only when) the trigger conditions are met."""

    action: Optional[Literal[EXECUTION_ACTION_VALUES]] = Field(default=None)  # type: ignore[valid-type]
    notional_usd: Optional[float] = Field(default=None, gt=0)
    limit_price: Optional[float] = Field(default=None, gt=0)
    broker_hint: Optional[Literal["webull", "kraken"]] = Field(default=None)
    derived_from_plan: bool = Field(default=True)
    derived_at: Optional[str] = Field(default=None, max_length=40)


# ── v2 → v3 mapping tables (PRD §6.2) ──────────────────────────────
_V2_INTENT_FOR_ACTION: Dict[str, str] = {
    "BUY":   "ENTER",
    "SELL":  "EXIT",
    "SHORT": "ENTER",
    "COVER": "EXIT",
    "HOLD":  "WATCH",   # the critical mapping — operator §11 locked
    "OPEN":  "ENTER",
    "CLOSE": "EXIT",
}

_V2_STANCE_FOR_ACTION: Dict[str, str] = {
    "BUY":   "BULLISH",
    "SELL":  "BEARISH",
    "SHORT": "BEARISH",
    "COVER": "BULLISH",
    "HOLD":  "NEUTRAL",
    "OPEN":  "NEUTRAL",
    "CLOSE": "NEUTRAL",
}


def _build_v2_lift(doc: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Synthesize `plan` + `execution` blocks for a v2 doc per §6.2.

    The lift is shape-only. It DOES NOT mutate any v2 field. Downstream
    consumers reading `doc["action"]` see the same value before and
    after. New consumers can read `doc["execution"]["action"]` and
    `doc["plan"]["intent"]` and get a uniform shape across versions.
    """
    action = (doc.get("action") or "").upper()
    is_hold_or_unknown = action in {"", "HOLD"}

    plan: Dict[str, Any] = {
        "stance": _V2_STANCE_FOR_ACTION.get(action, "NEUTRAL"),
        "setup": "other",
        "setup_custom_tag": None,
        "intent": _V2_INTENT_FOR_ACTION.get(action, "WATCH"),
        # v2 emitted action implies "do it now" (the brain ran the
        # gate chain before getting here). MARKET_NOW reflects that.
        "execution_style": "MARKET_NOW",
        "size_posture": "STANDARD",
        "portfolio_posture": "NEUTRAL",
        "hedge_against_symbol": None,
        "trigger_price": None,
        # Natural mappings off existing v2 RR fields.
        "invalidation_price": doc.get("stop_price"),
        "target_prices": (
            [float(doc["target_price"])] if doc.get("target_price") else None
        ),
        "confidence": float(doc.get("confidence") or 0.0),
        "thesis": (doc.get("rationale") or "")[:4_000],
        "horizon": "UNKNOWN",
        "ttl_seconds": None,
    }

    execution: Dict[str, Any] = {
        # PRD §6.2: HOLD lifts to execution.action=null. Everything
        # else preserves the canonical v2 action.
        "action": None if is_hold_or_unknown else action,
        "notional_usd": None,
        "limit_price": None,
        "broker_hint": None,
        # PRD §3.2: v2 emits are flagged false (legacy fast-path).
        "derived_from_plan": False,
        "derived_at": None,
    }
    return plan, execution


def _fill_v3_defaults(plan: Dict[str, Any], execution: Dict[str, Any], doc: Dict[str, Any]) -> None:
    """Ensure every consumer sees the same keys regardless of which
    optional fields the emitter omitted. In-place fill — caller owns
    the dicts.
    """
    plan.setdefault("stance", "NEUTRAL")
    plan.setdefault("setup", "other")
    plan.setdefault("setup_custom_tag", None)
    plan.setdefault("intent", "WATCH")
    plan.setdefault("execution_style", "MARKET_NOW")
    plan.setdefault("size_posture", "STANDARD")
    plan.setdefault("portfolio_posture", "NEUTRAL")
    plan.setdefault("hedge_against_symbol", None)
    plan.setdefault("trigger_price", None)
    plan.setdefault("invalidation_price", None)
    plan.setdefault("target_prices", None)
    plan.setdefault("confidence", float(doc.get("confidence") or 0.0))
    plan.setdefault("thesis", "")
    plan.setdefault("horizon", "UNKNOWN")
    plan.setdefault("ttl_seconds", None)

    execution.setdefault("action", None)
    execution.setdefault("notional_usd", None)
    execution.setdefault("limit_price", None)
    execution.setdefault("broker_hint", None)
    execution.setdefault("derived_from_plan", True)
    execution.setdefault("derived_at", None)


def normalize_intent(doc: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Lift v2 docs into v3 shape on-read. v3 docs pass through with
    optional inner keys defaulted. v2 fields are PRESERVED so existing
    consumers reading `doc["action"]` keep working.

    Returns:
        A *new* dict (the input is never mutated). Top-level
        `intent_version` is always populated ("v2" or "v3"). `plan`
        and `execution` blocks are always present with the full
        canonical key set.

    Behaviour matrix:
        | Input shape                                  | intent_version | plan         | execution.action |
        |----------------------------------------------|----------------|--------------|------------------|
        | v2 doc, action="BUY"                         | "v2"           | ENTER/BULLISH| "BUY"            |
        | v2 doc, action="HOLD"                        | "v2"           | WATCH/NEUTRAL| None             |
        | v3 doc, full plan{} + execution{}            | "v3"           | passes through| passes through  |
        | v3 doc, partial plan{}, no execution{}       | "v3"           | defaults filled| None           |
        | empty doc                                    | unchanged      | unchanged    | unchanged        |
    """
    if not doc:
        return doc

    out = dict(doc)
    version = out.get("intent_version") or "v2"
    out["intent_version"] = version

    if version == "v3":
        plan = dict(out.get("plan") or {})
        execution = dict(out.get("execution") or {})
        _fill_v3_defaults(plan, execution, out)
        out["plan"] = plan
        out["execution"] = execution
        return out

    # v2 lift.
    plan, execution = _build_v2_lift(out)
    out["plan"] = plan
    out["execution"] = execution
    return out


def normalize_intents(docs):
    """Convenience: apply `normalize_intent` over an iterable. Returns
    a list (callers usually want one)."""
    return [normalize_intent(d) for d in (docs or [])]


# ── Write-side: v3 envelope synthesizer (Step 4) ───────────────────
def synthesize_v3_envelope(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Upgrade a v2 emit-payload to a v3 envelope.

    Inverse of `normalize_intent` for the write path: takes the v2
    payload a brain runner already built (action / confidence /
    rationale / target_price / stop_price / lane / symbol / ...) and
    returns a SHALLOW COPY enriched with `intent_version="v3"`,
    `plan{}`, and `execution{}` blocks synthesised from the action.

    Doctrine:
      * The synthesised v3 envelope round-trips losslessly through
        `normalize_intent` — the lifter would map it back to the same
        action/intent/stance/execution_style combination. This is the
        property that makes Step 4 safe: turning a brain v3-on doesn't
        change what gets persisted in any way the downstream
        post-mortem can detect, until the brain actually starts
        emitting NEW v3-only intents (WAIT_FOR_TRIGGER, HEDGE, etc.).
      * The top-level `action` field is preserved on the payload so
        legacy v2 consumers (legacy gate chain, broker router) keep
        seeing the canonical action they always have.
      * `execution.derived_from_plan=True` (NOT False) because this
        IS a planning-aware emit, not a v2-legacy fast-path row. The
        lifter sets `False` for legacy v2 rows; the synthesiser sets
        `True` for v3-aware emits even when synthesised from a v2
        payload (per PRD §3.2 — `derived_from_plan` distinguishes
        v3-aware emits from legacy fast-path).
    """
    if not payload:
        return payload
    action = (payload.get("action") or "").upper()
    is_hold_or_blank = action in {"", "HOLD"}

    plan: Dict[str, Any] = {
        "stance": _V2_STANCE_FOR_ACTION.get(action, "NEUTRAL"),
        "setup": "other",
        "setup_custom_tag": None,
        "intent": _V2_INTENT_FOR_ACTION.get(action, "WATCH"),
        "execution_style": "MARKET_NOW",
        "size_posture": "STANDARD",
        "portfolio_posture": "NEUTRAL",
        "hedge_against_symbol": None,
        "trigger_price": None,
        "invalidation_price": payload.get("stop_price"),
        "target_prices": (
            [float(payload["target_price"])] if payload.get("target_price") else None
        ),
        "confidence": float(payload.get("confidence") or 0.0),
        "thesis": (payload.get("rationale") or "")[:4_000],
        "horizon": "UNKNOWN",
        "ttl_seconds": None,
    }
    execution: Dict[str, Any] = {
        "action": None if is_hold_or_blank else action,
        "notional_usd": None,
        "limit_price": None,
        "broker_hint": None,
        # IMPORTANT: True (not False) — this IS a planning-aware emit,
        # not a v2-legacy fast-path. See docstring above.
        "derived_from_plan": True,
        "derived_at": None,
    }

    new_payload = dict(payload)
    new_payload["intent_version"] = "v3"
    new_payload["plan"] = plan
    new_payload["execution"] = execution
    return new_payload


def v3_brain_enabled(brain_id: str, env_var: str = "PARADOX_V3_BRAINS") -> bool:
    """Step 4 feature flag — DB-backed with env-var fallback.

    Source of truth, in order:
      1. `system_flags.paradox_v3_brains` (DB-backed, flippable
         from the admin UI without restart — 2026-02-23 operator fix)
      2. `PARADOX_V3_BRAINS` env var (legacy / bootstrap fallback)

    Returns True iff `brain_id` is in the effective list. Default
    False on both empty — safe-OFF by design so v3 emits never roll
    out by accident.

    Examples (env behaviour preserved for tests):
        PARADOX_V3_BRAINS=                  → all brains stay on v2
        PARADOX_V3_BRAINS=camino            → only camino emits v3
        PARADOX_V3_BRAINS=camino,barracuda  → camino + barracuda

    DB override examples (Mongo `system_flags.current` doc):
        paradox_v3_brains: null              → fall back to env above
        paradox_v3_brains: []                → explicitly no brains
                                               (DB wins even if env set)
        paradox_v3_brains: ["camino"]        → only camino
    """
    target = (brain_id or "").strip().lower()
    if not target:
        return False
    # DB-first.
    try:
        from shared.system_flags import get_system_flags
        snap = get_system_flags()
        if snap.paradox_v3_brains is not None:
            return target in {b.lower() for b in snap.paradox_v3_brains}
    except Exception:  # noqa: BLE001
        # system_flags import / cache failure — fall through to env.
        pass
    # Env fallback (legacy path; preserved for tests + bootstrap).
    import os
    val = os.environ.get(env_var, "").strip().lower()
    if not val:
        return False
    brains = {b.strip() for b in val.split(",") if b.strip()}
    return target in brains


__all__ = (
    "STANCE_VALUES",
    "SETUP_VALUES",
    "PLAN_INTENT_VALUES",
    "EXECUTION_STYLE_VALUES",
    "SIZE_POSTURE_VALUES",
    "PORTFOLIO_POSTURE_VALUES",
    "HORIZON_VALUES",
    "EXECUTION_ACTION_VALUES",
    "HORIZON_TTL_DEFAULTS",
    "PlanBlock",
    "ExecutionBlock",
    "normalize_intent",
    "normalize_intents",
    "synthesize_v3_envelope",
    "v3_brain_enabled",
)

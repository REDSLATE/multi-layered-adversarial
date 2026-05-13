"""Decision Machine — brain-side intent envelope helper.

Drop this file into your sidecar at:
    services/decision_machine.py

Then import wherever your decision loop runs.

The doctrine: brains emit INTENTS, not orders. Every intent is a
candidate. MC's gate chain decides if it lives. `may_execute` and
`requires_gate_pass` are schema-pinned on the MC side — the brain
cannot lie about them. This module is a thin builder + poster.

Feature-flag controlled. Set DECISION_MACHINE_ENABLED=true to turn on.
Leave unset/false to keep the brain on the existing opinions-only path.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Optional

import httpx


log = logging.getLogger("risedual.decision_machine")


# ──────────────────────────── feature flag ────────────────────────────

def is_enabled() -> bool:
    """Read DECISION_MACHINE_ENABLED at call time. Easy to toggle without restart."""
    return os.environ.get("DECISION_MACHINE_ENABLED", "false").strip().lower() in (
        "1", "true", "yes", "on",
    )


# ──────────────────────────── envelope ────────────────────────────

# Mirror the action vocabulary in MC's shared/intents.py.
ACTIONS = ("BUY", "SELL", "SHORT", "COVER", "HOLD")


@dataclass(frozen=True)
class Intent:
    """Brain-side intent envelope. Mirrors the MC `IntentIn` schema.

    Fields the brain CONTROLS:
        stack            — your runtime name ("alpha"/"camaro"/...)
        action           — one of ACTIONS
        symbol           — e.g. "TSLA", "BTC/USD"
        confidence       — 0.0..1.0
        risk_multiplier  — 0.0..1.0 (0 = veto-equivalent, 1 = full size)
        rationale        — required, ≤4000 chars
        evidence         — bounded dict, ≤16 KB serialized
        decision_id      — optional, your internal id (for cross-reference)
        regime           — optional, e.g. "trend", "risk_on"

    Fields MC SETS (not on this dataclass — server-stamped):
        intent_id, seat_at_post_time, ingest_ts, gate_state, executed.

    Fields the brain CANNOT SET (schema-pinned to False/True on MC):
        may_execute=False, requires_gate_pass=True.
    """
    stack: str
    action: str
    symbol: str
    confidence: float
    risk_multiplier: float
    rationale: str
    evidence: dict[str, Any]
    decision_id: Optional[str] = None
    regime: Optional[str] = None

    def to_payload(self) -> dict[str, Any]:
        """Body for POST /api/intents."""
        payload: dict[str, Any] = {
            "stack": self.stack,
            "action": self.action,
            "symbol": self.symbol.strip().upper(),
            "confidence": float(self.confidence),
            "risk_multiplier": float(self.risk_multiplier),
            "rationale": self.rationale,
            "evidence": self.evidence or {},
            # Schema-pinned on MC; sending them is harmless but explicit
            # so any code review reading the payload sees the doctrine.
            "may_execute": False,
            "requires_gate_pass": True,
        }
        if self.decision_id:
            payload["decision_id"] = self.decision_id
        if self.regime:
            payload["regime"] = self.regime
        return payload


# ──────────────────────────── builder ────────────────────────────

def build_intent_from_council(
    *,
    stack: str,
    symbol: str,
    governed: dict[str, Any],
    action: Optional[str] = None,
    rationale: Optional[str] = None,
    decision_id: Optional[str] = None,
) -> Intent:
    """Helper: collapse a council `governed` snapshot into an Intent.

    Reads the standard governance fields your sidecar already builds
    (`council_binding_voice`, `size_multiplier`, `envelope_approved`,
    `binding_rule`, etc) and produces a canonical Intent envelope.

    The mapping mirrors what your sidecar currently does for opinions:
      * council_binding_voice=BULL  → BUY
      * council_binding_voice=BEAR  → SELL  (or SHORT if you prefer)
      * council_binding_voice=CMD   → HOLD
      * SAFETY_OVERRIDE or envelope_approved=False → HOLD (confidence 1.0)
    """
    voice = governed.get("council_binding_voice")
    size_mult = float(governed.get("size_multiplier") or 0.0)
    margin = float(governed.get("council_margin") or 0.0)
    envelope_ok = bool(governed.get("envelope_approved"))
    rule = governed.get("binding_rule")

    # Action mapping (override allowed).
    if action is None:
        if not envelope_ok or rule == "SAFETY_OVERRIDE":
            action = "HOLD"
        elif voice == "BULL":
            action = "BUY"
        elif voice == "BEAR":
            action = "SELL"
        else:
            action = "HOLD"
    if action not in ACTIONS:
        raise ValueError(f"action must be one of {ACTIONS}, got {action!r}")

    # Confidence mapping — same shape as opinions for symmetry.
    if action == "HOLD":
        # Margin-based observation confidence (0..1, threshold ~0.35).
        confidence = max(0.0, min(1.0, margin / 0.35))
        risk_multiplier = 0.0
    else:
        confidence = max(0.0, min(1.0, size_mult))
        risk_multiplier = max(0.0, min(1.0, size_mult))

    if rationale is None:
        rationale = (
            f"{stack}/decision_machine: voice={voice} margin={margin:.3f} "
            f"size_mult={size_mult:.3f} rule={rule} envelope_ok={envelope_ok}"
        )

    return Intent(
        stack=stack,
        action=action,
        symbol=symbol,
        confidence=confidence,
        risk_multiplier=risk_multiplier,
        rationale=rationale,
        evidence={
            "council_margin": margin,
            "size_multiplier": size_mult,
            "envelope_approved": envelope_ok,
            "binding_rule": rule,
            "binding_voice": voice,
            "strategist_signal": governed.get("strategist_signal"),
            "auditor_score": governed.get("auditor_score"),
            "bull_score": governed.get("bull_score"),
            "bear_score": governed.get("bear_score"),
        },
        decision_id=decision_id
            or (governed.get("decision") or {}).get("decision_id"),
        regime=governed.get("regime"),
    )


# ──────────────────────────── poster ────────────────────────────

async def post_intent(intent: Intent) -> dict[str, Any]:
    """POST the intent to MC. Returns the parsed response.

    On any failure logs a warning and returns `{"ok": False, "error": "..."}`
    so the calling sidecar loop never crashes on transport hiccups.

    Reads MC base URL + token from env at call time:
        MONOREPO_BASE_URL         — e.g. https://multi-brain-backbone.preview.emergentagent.com
        MONOREPO_INGEST_TOKEN     — this brain's runtime ingest token
    """
    if not is_enabled():
        log.debug("decision_machine disabled (DECISION_MACHINE_ENABLED unset); skipping")
        return {"ok": False, "skipped": True, "reason": "flag_disabled"}

    base = os.environ.get("MONOREPO_BASE_URL", "").rstrip("/")
    token = os.environ.get("MONOREPO_INGEST_TOKEN", "")
    if not base or not token:
        return {"ok": False, "error": "missing MONOREPO_BASE_URL or MONOREPO_INGEST_TOKEN"}

    payload = intent.to_payload()
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.post(
                f"{base}/api/intents",
                json=payload,
                headers={"X-Runtime-Token": token},
            )
            r.raise_for_status()
            return r.json()
    except httpx.HTTPStatusError as e:
        log.warning("intent post rejected by MC: status=%s body=%s",
                    e.response.status_code, e.response.text[:300])
        return {
            "ok": False,
            "error": f"HTTP {e.response.status_code}",
            "detail": e.response.text[:300],
        }
    except Exception as e:  # noqa: BLE001
        log.warning("intent post failed: %s", e)
        return {"ok": False, "error": str(e)}


async def read_intents(
    *, stack: Optional[str] = None,
    symbol: Optional[str] = None,
    gate_state: Optional[str] = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Read recent intents (yours or peers'). Same auth as post_intent."""
    if not is_enabled():
        return {"items": [], "count": 0, "skipped": True}
    base = os.environ.get("MONOREPO_BASE_URL", "").rstrip("/")
    token = os.environ.get("MONOREPO_INGEST_TOKEN", "")
    if not base or not token:
        return {"items": [], "count": 0, "error": "missing env"}

    params: dict[str, str] = {"limit": str(int(limit))}
    if stack: params["stack"] = stack
    if symbol: params["symbol"] = symbol.strip().upper()
    if gate_state: params["gate_state"] = gate_state

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(
                f"{base}/api/intents",
                params=params,
                headers={"X-Runtime-Token": token},
            )
            r.raise_for_status()
            return r.json()
    except Exception as e:  # noqa: BLE001
        log.warning("intent read failed: %s", e)
        return {"items": [], "count": 0, "error": str(e)}

"""RISEDUAL — Council Module.

Extracted 2026-02-15 from `shared/execution.py` (which had grown to
1355 lines). Contains the full lane-aware graduated council:
  * Lane policy table (equity / crypto)
  * Schema-tolerant brain & symbol matchers
  * Seat-bound lookups for governor & opponent (lane-aware)
  * `_governance_verdict` — pure function: graduated verdict matrix
  * `_evaluate_council` — orchestrator: composes governor + opponent +
    quantum-inspired regime overlay, writes the governance ledger row

This module exports the helpers `_evaluate_council` and the
diagnostic-endpoint helpers consume. See the module-level docstring of
each function for doctrine details.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from db import db
from namespaces import (
    SHARED_GOVERNANCE_DECISIONS,
    SHARED_RECEIPTS,
    SOVEREIGN_AUDIT_LOG,
)
from shared.mc_shelly import record_async
from shared.quantum_state import (
    BrainOpinion as _QSBrainOpinion,
    build_quantum_inspired_state as _build_quantum_state,
)
from shared.stack_personalities import (
    enrich_response as _stamp_personality,
    personality_of as _personality_of,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ───────────────────────────── council ─────────────────────────────
# Doctrine (rev4, 2026-02-15): LANE-AWARE GRADUATED COUNCIL.
#
# The council is bound to SEATS, not brain identities. The Governor seat
# holder must record a stance, but its DISSENT is now a smooth multiplier
# — not a binary kill switch. Hard veto stays available for true safety
# stops. Crypto runs a more permissive variant: it punishes hesitation
# more than equities, so governance damping is reduced and momentum
# weighting is lifted.
#
# Verdict outputs:
#   * HARD_VETO          → block (veto bit + governor conf ≥ hard_veto)
#   * NO_STANCE          → SOFT downweight (governor uncertain on symbol
#                          but alive) — NO LONGER a hard block
#   * GOVERNOR_OFFLINE   → block (no calls anywhere in 30m)
#   * SOFT_DISSENT       → executor fires at conf × dissent_conf_mult,
#                          size × dissent_size_mult. Below MIN_EXECUTOR_
#                          CONF_FLOOR after suppression → block.
#   * NO_DISSENT         → full size, lane base multiplier.
#
# Clamps prevent any single agent from collapsing or amplifying the
# action space beyond MAX_DOWNWEIGHT / MAX_UPWEIGHT.

# How fresh a council signal must be to count.
_COUNCIL_FRESHNESS_SECONDS = 600  # 10 minutes
# How long the governor seat can be silent before we consider it offline.
_GOVERNOR_OFFLINE_THRESHOLD_SECONDS = 1800  # 30 minutes

# Lane-aware policy. Each lane's policy lives in its own subpackage:
#   shared/equity/council_policy.py — consensus-first / governance-heavy
#   shared/crypto/council_policy.py — momentum-biased / governance-light
#
# This file is the dispatcher. A lane-only change should require
# editing ONLY the policy file in that lane's subpackage — never this
# one. (2026-02-16 reorg.)
from shared.crypto.council_policy import CRYPTO_POLICY
from shared.equity.council_policy import EQUITY_POLICY

COUNCIL_POLICY: dict[str, dict] = {
    "equity": EQUITY_POLICY,
    "crypto": CRYPTO_POLICY,
}


def _policy_for_lane(lane: Optional[str]) -> dict:
    """Pick the council policy for an intent's lane. Equity is the safe
    default for legacy / lane-untagged intents."""
    if (lane or "").lower() == "crypto":
        return COUNCIL_POLICY["crypto"]
    return COUNCIL_POLICY["equity"]


def _clamp_size(size: float, policy: dict) -> float:
    """Bound size adjustments by lane-policy floor/ceiling."""
    return max(policy["MAX_DOWNWEIGHT"], min(policy["MAX_UPWEIGHT"], size))


def _clamp_agent_delta(base: float, adjusted: float, policy: dict) -> float:
    """No single agent may move size by more than ±MAX_SINGLE_AGENT_INFLUENCE
    from the base. Prevents Chevelle freeze spirals AND Camaro dominance spirals."""
    cap = policy["MAX_SINGLE_AGENT_INFLUENCE"]
    delta = adjusted - base
    delta = max(-cap, min(cap, delta))
    return base + delta


# Brain-identity fields a receipt might use. Engines vary; we accept all.
_BRAIN_FIELDS = ("runtime", "brain", "stack", "source", "from")

# Candidate symbol paths inside an authority_call doc.
_SYMBOL_PATHS = (
    "intent.symbol",
    "symbol",
    "payload.symbol",
    "data.symbol",
    "call.symbol",
)

# Candidate action/kind fields (and the value we accept).
_ACTION_FIELDS = ("action", "kind", "type", "event")
_AUTHORITY_CALL_VALUES = ("authority_call", "AUTHORITY_CALL", "authoritycall")
_CONTRIBUTION_VALUES = ("contribution", "Contribution", "CONTRIBUTION")


def _brain_id_variants(name: str) -> list[str]:
    """All case variants we accept for a brain identity."""
    if not name:
        return []
    return list({name, name.lower(), name.upper(), name.capitalize()})


def _brain_match_clause(brain: str) -> dict:
    """Match any of the known identity fields against any case variant
    of `brain`. Returns an `$or` Mongo clause."""
    variants = _brain_id_variants(brain)
    return {"$or": [
        {field: {"$in": variants}} for field in _BRAIN_FIELDS
    ]}


def _authority_call_clause() -> dict:
    return {"$or": [
        {field: {"$in": list(_AUTHORITY_CALL_VALUES)}}
        for field in _ACTION_FIELDS
    ]}


def _contribution_clause() -> dict:
    return {"$or": [
        {field: {"$in": list(_CONTRIBUTION_VALUES)}}
        for field in _ACTION_FIELDS
    ]}


def _symbol_clause(symbol: str) -> dict:
    return {"$or": [{path: symbol} for path in _SYMBOL_PATHS]}


def _extract(doc: dict, path: str):
    """Walk a dotted path through nested dicts. Returns None if absent."""
    cur = doc
    for key in path.split("."):
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur


def _normalize_governor_call(doc: Optional[dict]) -> Optional[dict]:
    """Pull the governor's signals — executable, veto, confidence, stance,
    reason, timestamp — from whichever shape the receipt uses."""
    if not doc:
        return None
    # Try common payload containers in priority order.
    for container_path in ("intent", "payload", "call", "data", ""):
        node = _extract(doc, container_path) if container_path else doc
        if not isinstance(node, dict):
            continue
        # We accept the node as the "governor payload" if ANY governance
        # signal lives there.
        signals = ("executable", "veto", "stance", "confidence")
        if any(k in node for k in signals):
            return {
                "executable": node.get("executable"),
                "veto": bool(node.get("veto", False)),
                "confidence": float(
                    node.get("confidence")
                    or node.get("calibrated_confidence")
                    or node.get("raw_confidence")
                    or 0.0
                ),
                "stance": (
                    node.get("stance")
                    or node.get("call")
                    or node.get("authority_call")
                    or ""
                ),
                "reason": (
                    node.get("execution_gate_reason")
                    or node.get("reason")
                    or node.get("gate_reason")
                    or "unspecified"
                ),
                "ts": doc.get("timestamp") or doc.get("ts") or doc.get("created_at"),
                "shape_container": container_path or "root",
            }
    return None


# ── Roster-bound seat lookups ─────────────────────────────────────────
async def _seat_holder(role: str, lane: Optional[str] = None) -> Optional[str]:
    """Current occupant of `role` in the live roster, or None if vacant.

    Lane-isolated (2026-05-17, rev2): seats are STRICTLY separated by
    lane. When `lane="crypto"` we look up ONLY the crypto seat
    (`crypto_executor` → `crypto`, otherwise `crypto_<role>`). If the
    crypto seat is vacant, the call returns None — there is NO fallback
    to the equity seat. The earlier fallback let an equity-Governor
    occupant silently govern crypto intents; that violated lane
    isolation. To run crypto, the operator must explicitly assign each
    crypto seat.

    Mapping: the `crypto_executor` role is stored as `crypto` in the
    roster (legacy name from when crypto was a single executor seat).
    Everything else uses the `crypto_<role>` prefix.
    """
    from shared.roster import get_roster  # noqa: WPS433
    r = await get_roster()
    assignments = r.get("assignments") or {}

    if (lane or "").lower() == "crypto":
        crypto_role = "crypto" if role == "executor" else f"crypto_{role}"
        return assignments.get(crypto_role)

    return assignments.get(role)


async def _latest_governor_call(symbol: Optional[str], lane: Optional[str] = None) -> tuple[Optional[str], Optional[dict]]:
    """(holder, doc) — most recent authority_call by the current Governor
    seat holder for `symbol`. Returns (None, None) if the seat is vacant.
    Lane-aware: for crypto intents reads `crypto_governor` first."""
    holder = await _seat_holder("governor", lane=lane)
    if not holder or not symbol:
        return holder, None
    query = {"$and": [
        _brain_match_clause(holder),
        _authority_call_clause(),
        _symbol_clause(symbol),
    ]}
    doc = await db[SHARED_RECEIPTS].find_one(query, {"_id": 0}, sort=[("timestamp", -1)])
    return holder, doc


async def _latest_governor_any_call(lane: Optional[str] = None) -> tuple[Optional[str], Optional[dict]]:
    """(holder, doc) — most recent authority_call by Governor for ANY symbol.
    Used to distinguish 'governor offline' from 'governor uncertain on this name'.
    Lane-aware."""
    holder = await _seat_holder("governor", lane=lane)
    if not holder:
        return holder, None
    query = {"$and": [_brain_match_clause(holder), _authority_call_clause()]}
    doc = await db[SHARED_RECEIPTS].find_one(query, {"_id": 0}, sort=[("timestamp", -1)])
    return holder, doc


async def _latest_opponent_contribution(lane: Optional[str] = None) -> tuple[Optional[str], Optional[dict]]:
    """(holder, doc) — most recent sovereign contribution by Opponent seat.
    Lane-aware: for crypto intents reads `crypto_opponent` first."""
    holder = await _seat_holder("opponent", lane=lane)
    if not holder:
        return holder, None
    query = {"$and": [_brain_match_clause(holder), _contribution_clause()]}
    doc = await db[SOVEREIGN_AUDIT_LOG].find_one(query, {"_id": 0}, sort=[("ts", -1)])
    return holder, doc


def _doc_ts(doc: Optional[dict]) -> Optional[str]:
    if not doc:
        return None
    return doc.get("timestamp") or doc.get("ts") or doc.get("created_at")


def _is_fresh(ts: Optional[str], max_age_seconds: int = _COUNCIL_FRESHNESS_SECONDS) -> bool:
    if not ts:
        return False
    try:
        # Parse ISO; tolerate "Z" suffix.
        cleaned = str(ts).replace("Z", "+00:00")
        emitted = datetime.fromisoformat(cleaned)
        if emitted.tzinfo is None:
            emitted = emitted.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - emitted).total_seconds()
        return age <= max_age_seconds
    except Exception:  # noqa: BLE001
        return False


# ── The graduated verdict ─────────────────────────────────────────────
def _governance_verdict(
    intent: dict,
    gov_norm: Optional[dict],
    governor_alive: bool,
    governor_holder: Optional[str],
    policy: Optional[dict] = None,
) -> dict:
    """Pure function: given the intent, the normalized governor call,
    and a lane policy, return the verdict dict.

    Output:
      {
        allowed: bool,
        reason: code,
        disagreement: bool,
        record_pushback: bool,
        risk_multiplier: float (continuous, clamped),
        effective_conf: float (executor conf after governor multipliers),
      }

    Doctrine: governor must be HEARD. Hard veto blocks. Soft dissent
    SHAPES (conf × dissent_conf_mult, size × dissent_size_mult) rather
    than killing — unless the effective conf falls below the floor, in
    which case we block to protect against weak-conviction-into-headwind
    trades.
    """
    p = policy or COUNCIL_POLICY["equity"]
    executor_conf = float(
        intent.get("confidence")
        or intent.get("calibrated_confidence")
        or 0.0
    )

    def _result(allowed, reason, disagreement, size_mult, conf_mult, pushback=False):
        eff_conf = executor_conf * conf_mult
        # Clamp size by global bounds. Single-agent influence is clamped
        # by the caller against the lane baseline (1.0).
        return {
            "allowed": allowed,
            "reason": reason,
            "disagreement": disagreement,
            "record_pushback": pushback,
            "risk_multiplier": _clamp_size(size_mult, p) if allowed else 0.0,
            "effective_conf": eff_conf,
        }

    # Governor seat vacant — no one to be heard. Block.
    if not governor_holder:
        return _result(False, "GOVERNOR_SEAT_VACANT", False, 0.0, 0.0)

    # Governor alive but no stance on this symbol → SOFT downweight, do
    # NOT hard-block. Better to trade smaller than to freeze the action
    # space waiting for a stance that may never come.
    if gov_norm is None:
        if not governor_alive:
            return _result(False, "GOVERNOR_OFFLINE", False, 0.0, 0.0)
        size_mult = p["GOVERNOR_NO_STANCE_SIZE_MULT"]
        conf_mult = p["GOVERNOR_NO_STANCE_CONF_MULT"]
        eff_conf = executor_conf * conf_mult
        if eff_conf < p["MIN_EXECUTOR_CONF_FLOOR"]:
            return _result(False, "NO_STANCE_LOW_EFFECTIVE_CONF", True, 0.0, conf_mult, pushback=True)
        return _result(True, "GOVERNOR_NO_STANCE_SOFT_DOWNWEIGHT", True, size_mult, conf_mult, pushback=True)

    governor_veto = bool(gov_norm.get("veto", False))
    governor_conf = float(gov_norm.get("confidence") or 0.0)
    executable = gov_norm.get("executable")
    stance = str(gov_norm.get("stance") or "").upper()

    # Disagreement signal — any of these count.
    disagreement = (
        governor_veto
        or executable is False
        or stance in {"VETO", "DISSENT", "RISK_DOWN", "HOLD", "REJECT", "ABSTAIN"}
    )

    # Hard veto: explicit veto bit AND high conviction. True safety stop.
    if governor_veto and governor_conf >= p["GOVERNOR_HARD_VETO_THRESHOLD"]:
        return _result(False, "GOVERNOR_HARD_VETO", True, 0.0, 0.0, pushback=True)

    # Soft dissent: shape, don't kill. conf × dissent_conf_mult; size ×
    # dissent_size_mult. Block only if effective conf falls below floor.
    if disagreement:
        size_mult = p["GOVERNOR_DISSENT_SIZE_MULT"]
        conf_mult = p["GOVERNOR_DISSENT_CONF_MULT"]
        eff_conf = executor_conf * conf_mult
        if eff_conf < p["MIN_EXECUTOR_CONF_FLOOR"]:
            return _result(False, "SOFT_DISSENT_BELOW_FLOOR", True, 0.0, conf_mult, pushback=True)
        return _result(True, "SOFT_DISSENT_DOWNWEIGHTED", True, size_mult, conf_mult, pushback=True)

    # No dissent — lane baseline. Momentum weighting allows crypto to
    # punch slightly above the equity baseline (≤ MAX_UPWEIGHT).
    size_mult = 1.0 * p["MOMENTUM_WEIGHTING"]
    return _result(True, "NO_GOVERNOR_DISSENT", False, size_mult, 1.0)


async def _evaluate_council(intent: dict) -> tuple[list[dict], float]:
    """Returns (gate_rows, risk_multiplier).

    Two gates: governor_authority + opponent_objection. Verdicts come
    from `_governance_verdict` (graduated). Every evaluation writes a
    row to SHARED_GOVERNANCE_DECISIONS so outcomes can later score who
    was right.
    """
    sym = intent.get("symbol")
    action = (intent.get("action") or "").upper()
    intent_id = intent.get("intent_id", "?")
    lane = intent.get("lane")
    policy = _policy_for_lane(lane)
    executor_holder = await _seat_holder("executor", lane=lane)
    governor_holder, gov_doc = await _latest_governor_call(sym, lane=lane)
    gov_norm = _normalize_governor_call(gov_doc)

    if gov_norm is None:
        # No per-symbol call — is the governor alive at all?
        _, gov_any = await _latest_governor_any_call(lane=lane)
        governor_alive = _is_fresh(_doc_ts(gov_any), _GOVERNOR_OFFLINE_THRESHOLD_SECONDS)
        gov_any_ts = _doc_ts(gov_any)
    else:
        governor_alive = True
        gov_any_ts = gov_norm.get("ts")

    verdict = _governance_verdict(intent, gov_norm, governor_alive, governor_holder, policy)

    # Build the gate row for the governor.
    gov_reason_text = {
        "GOVERNOR_HARD_VETO": (
            f"GOVERNOR ({governor_holder}) HARD VETO on {sym} ({lane or 'equity'}): "
            f"conf={gov_norm.get('confidence') if gov_norm else 'n/a'} "
            f"≥ {policy['GOVERNOR_HARD_VETO_THRESHOLD']}"
        ),
        "SOFT_DISSENT_DOWNWEIGHTED": (
            f"GOVERNOR ({governor_holder}) dissented on {sym}; "
            f"executor ({executor_holder}) conf "
            f"{float(intent.get('confidence') or 0.0):.2f} × "
            f"{policy['GOVERNOR_DISSENT_CONF_MULT']:.2f} = "
            f"{verdict.get('effective_conf', 0):.2f} (≥ floor "
            f"{policy['MIN_EXECUTOR_CONF_FLOOR']:.2f}) — fires at "
            f"size×{verdict['risk_multiplier']:.2f}"
        ),
        "SOFT_DISSENT_BELOW_FLOOR": (
            f"GOVERNOR ({governor_holder}) dissented on {sym}; "
            f"effective conf {verdict.get('effective_conf', 0):.2f} "
            f"< floor {policy['MIN_EXECUTOR_CONF_FLOOR']:.2f} — "
            f"conviction too weak after governor suppression"
        ),
        "GOVERNOR_NO_STANCE_SOFT_DOWNWEIGHT": (
            f"GOVERNOR ({governor_holder}) live but no stance on {sym} — "
            f"soft downweight: size×{verdict['risk_multiplier']:.2f}"
        ),
        "NO_STANCE_LOW_EFFECTIVE_CONF": (
            f"GOVERNOR ({governor_holder}) silent on {sym} AND executor "
            f"conf too low after suppression "
            f"({verdict.get('effective_conf', 0):.2f} < "
            f"{policy['MIN_EXECUTOR_CONF_FLOOR']:.2f})"
        ),
        "NO_GOVERNOR_DISSENT": (
            f"GOVERNOR ({governor_holder}) recorded stance with no "
            f"dissent on {sym} — size×{verdict['risk_multiplier']:.2f}"
        ),
        "GOVERNOR_OFFLINE": (
            f"GOVERNOR ({governor_holder}) silent for "
            f"≥ {_GOVERNOR_OFFLINE_THRESHOLD_SECONDS // 60}m — "
            f"last seen: {gov_any_ts or 'never'}"
        ),
        "GOVERNOR_SEAT_VACANT": (
            "GOVERNOR seat is vacant — no one to record a stance"
        ),
    }
    gov_gate = {
        "name": "governor_authority",
        "passed": verdict["allowed"],
        "reason": gov_reason_text.get(verdict["reason"], verdict["reason"]),
        "verdict_code": verdict["reason"],
        "disagreement": verdict["disagreement"],
        "risk_multiplier": verdict["risk_multiplier"],
        "effective_conf": verdict.get("effective_conf"),
        "lane": lane,
        "policy_used": "crypto" if (lane or "").lower() == "crypto" else "equity",
    }

    # ── opponent_objection ─────────────────────────────────────────────
    # Seat-bound: queries whoever holds the Opponent seat. Advisory only
    # now — never hard-blocks. The opponent's view is captured in the
    # governance row and feeds the outcome learner.
    opponent_holder, opp_doc = await _latest_opponent_contribution(lane=lane)
    opp_ts = _doc_ts(opp_doc)

    if not opponent_holder:
        opp_gate = {
            "name": "opponent_objection",
            "passed": True,
            "reason": "OPPONENT seat vacant — no opposition signal",
            "opponent_holder": None,
            "opponent_conf": 0.0,
            "opponent_side": None,
            "opponent_opposes": False,
        }
    elif not opp_doc or not _is_fresh(opp_ts):
        opp_gate = {
            "name": "opponent_objection",
            "passed": True,
            "reason": f"OPPONENT ({opponent_holder}) silent — no fresh contribution",
            "opponent_holder": opponent_holder,
            "opponent_conf": 0.0,
            "opponent_side": None,
            "opponent_opposes": False,
        }
    else:
        payload = (
            opp_doc.get("payload")
            or opp_doc.get("data")
            or opp_doc.get("contribution")
            or {}
        )
        if not isinstance(payload, dict):
            payload = {}
        r_conf = float(
            payload.get("confidence")
            or payload.get("conviction")
            or opp_doc.get("confidence")
            or 0.0
        )
        r_side_raw = (
            payload.get("side")
            or payload.get("stance")
            or payload.get("bias")
            or opp_doc.get("side")
            or ""
        )
        r_side = str(r_side_raw).lower()
        direction = (
            "bullish" if action in ("BUY", "COVER")
            else "bearish" if action in ("SELL", "SHORT")
            else None
        )
        opposes = (
            (direction == "bullish" and r_side in ("bearish", "short", "sell", "down"))
            or (direction == "bearish" and r_side in ("bullish", "long", "buy", "up"))
        )
        opp_gate = {
            "name": "opponent_objection",
            "passed": True,  # advisory; never blocks on its own
            "reason": (
                f"OPPONENT ({opponent_holder}) {r_side or 'neutral'} "
                f"@ conf {r_conf:.2f} — "
                + ("opposes " if opposes else "agrees with ")
                + f"{action} {sym} (advisory, logged for outcome scoring)"
            ),
            "opponent_holder": opponent_holder,
            "opponent_conf": r_conf,
            "opponent_side": r_side,
            "opponent_opposes": opposes,
        }

    # ── Compose final size: governor verdict × opponent influence × clamps ─
    # Doctrine: each agent shapes risk, none collapse it (except hard veto).
    base_size = verdict["risk_multiplier"]  # already 0 if blocked
    final_size = base_size
    opp_influence_applied = 0.0

    if verdict["allowed"] and opp_gate.get("opponent_opposes"):
        # Opponent pulls size DOWN proportional to their confidence and
        # the lane's OPPONENT_INFLUENCE. Maximum pull bounded by
        # MAX_SINGLE_AGENT_INFLUENCE so REDEYE can't single-handedly
        # freeze a strong Camaro setup.
        opp_conf = float(opp_gate["opponent_conf"])
        raw_pull = opp_conf * policy["OPPONENT_INFLUENCE"]
        opp_influence_applied = min(raw_pull, policy["MAX_SINGLE_AGENT_INFLUENCE"])
        final_size = _clamp_agent_delta(base_size, base_size * (1.0 - opp_influence_applied), policy)
        # Update opponent gate reason to reflect the actual influence applied.
        opp_gate["reason"] = (
            f"OPPONENT ({opp_gate['opponent_holder']}) opposes "
            f"{action} {sym} @ conf {opp_gate['opponent_conf']:.2f} — "
            f"size pulled by {opp_influence_applied:.0%} "
            f"(base {base_size:.2f} → {final_size:.2f})"
        )
        opp_gate["opp_influence_applied"] = opp_influence_applied

    # Final clamp against lane bounds (defense in depth).
    final_size = _clamp_size(final_size, policy) if verdict["allowed"] else 0.0

    # ── Quantum-inspired regime overlay (2026-02-15) ──────────────────
    # The quantum state observes the council's stances + intent features
    # and produces a BOUNDED risk multiplier + regime probability field
    # + HOLD-lock signal. By doctrine it MAY modulate risk only — it
    # cannot change direction or promote HOLD into a trade. We multiply
    # the council-composed size by quantum_state.risk_multiplier and
    # re-clamp against lane bounds (defense in depth).
    qs_opinions: list = []
    # Executor's call goes in as the actionable direction.
    qs_opinions.append(_QSBrainOpinion(
        brain=str(executor_holder or intent.get("stack") or "executor"),
        direction=str(action),
        confidence=float(intent.get("confidence") or 0.0),
    ))
    # Governor's call — derive a coarse direction from the stance and
    # executable flag. If governor said executable=False or veto/dissent
    # we map to HOLD (a "don't act" advisory); explicit executable=True
    # mirrors the executor's direction; otherwise HOLD.
    if gov_norm is not None:
        if (gov_norm.get("veto") or gov_norm.get("executable") is False
                or str(gov_norm.get("stance") or "").upper()
                    in {"HOLD", "VETO", "DISSENT", "REJECT", "ABSTAIN", "RISK_DOWN"}):
            gov_dir = "HOLD"
        elif gov_norm.get("executable") is True:
            gov_dir = action
        else:
            gov_dir = "HOLD"
        qs_opinions.append(_QSBrainOpinion(
            brain=str(governor_holder or "governor"),
            direction=gov_dir,
            confidence=float(gov_norm.get("confidence") or 0.5),
        ))
    # Opponent's call — map their side to a direction. Skip if no signal.
    opp_side = (opp_gate.get("opponent_side") or "").lower() if opp_gate else ""
    if opp_side:
        opp_dir = (
            "SHORT" if opp_side in ("bearish", "short", "sell", "down")
            else "BUY" if opp_side in ("bullish", "long", "buy", "up")
            else "HOLD"
        )
        qs_opinions.append(_QSBrainOpinion(
            brain=str(opp_gate.get("opponent_holder") or "opponent"),
            direction=opp_dir,
            confidence=float(opp_gate.get("opponent_conf") or 0.0),
        ))

    market_features = intent.get("features") or intent.get("market_features") or {}
    qs_verdict = _build_quantum_state(
        opinions=qs_opinions,
        market_features=market_features if isinstance(market_features, dict) else {},
    )
    quantum_dict = qs_verdict.to_dict()
    if verdict["allowed"]:
        # Apply the quantum multiplier and re-clamp against lane bounds.
        pre_qs = final_size
        final_size = _clamp_size(final_size * qs_verdict.risk_multiplier, policy)
        quantum_dict["pre_quantum_size"] = pre_qs
        quantum_dict["post_quantum_size"] = final_size
    # Reflect the composed size back on the governor row so downstream
    # readers (auto_router) see the post-composition number.
    gov_gate["risk_multiplier"] = final_size
    gov_gate["quantum_state"] = quantum_dict

    # Audit: write both council decisions to mc_shelly for training.
    for g in (gov_gate, opp_gate):
        record_async(
            event_type="council_pass" if g["passed"] else "council_block",
            brain=intent.get("stack"),
            symbol=sym,
            action=action,
            outcome="pass" if g["passed"] else "block",
            rationale=g["reason"],
            ref_id=intent_id,
            gate_name=g["name"],
        )

    # ── Stamp gate rows with personality envelope (advisory metadata).
    #    Permissions DO NOT come from personality — they come from
    #    seat_policy. This envelope just tells the operator and any
    #    consumer "this brain made this call from this bias/voice".
    if governor_holder:
        _stamp_personality(governor_holder, gov_gate)
    if opp_gate.get("opponent_holder"):
        _stamp_personality(opp_gate["opponent_holder"], opp_gate)

    # ── Governance decision row (per-intent learning ledger) ──────────
    # Captures both seats' stances, the verdict, and the resulting
    # risk_multiplier. Shelly/outcomes can join on intent_id to score
    # who was right after the trade resolves.
    exec_personality = _personality_of(executor_holder) or {}
    gov_personality = _personality_of(governor_holder) or {}
    opp_personality = _personality_of(opp_gate.get("opponent_holder")) or {}
    governance_row = {
        "ts": _now_iso(),
        "intent_id": intent_id,
        "symbol": sym,
        "lane": lane,
        "policy_used": "crypto" if (lane or "").lower() == "crypto" else "equity",
        "executor_seat_holder": executor_holder,
        "executor_personality_bias": exec_personality.get("bias"),
        "executor_action": action,
        "executor_confidence": float(intent.get("confidence") or 0.0),
        "executor_effective_conf": verdict.get("effective_conf"),
        "governor_seat_holder": governor_holder,
        "governor_personality_bias": gov_personality.get("bias"),
        "governor_stance": (gov_norm or {}).get("stance"),
        "governor_executable": (gov_norm or {}).get("executable"),
        "governor_veto": (gov_norm or {}).get("veto"),
        "governor_confidence": (gov_norm or {}).get("confidence"),
        "governor_call_ts": (gov_norm or {}).get("ts"),
        "opponent_seat_holder": opp_gate.get("opponent_holder"),
        "opponent_personality_bias": opp_personality.get("bias"),
        "opponent_confidence": opp_gate.get("opponent_conf"),
        "opponent_side": opp_gate.get("opponent_side"),
        "opponent_opposes": opp_gate.get("opponent_opposes"),
        "opp_influence_applied": opp_influence_applied,
        "disagreement": verdict["disagreement"],
        "verdict_code": verdict["reason"],
        "final_allowed": verdict["allowed"],
        "base_risk_multiplier": base_size,
        "risk_multiplier": final_size,
        "quantum_state": quantum_dict,
        # Hard limits (from personality "never" clauses) are advisory.
        # Authority is still seat-bound — this flag just records whether
        # the decision aligned with the doctrinal limits of the seat
        # holder's personality, for downstream training.
        "hard_limits_respected": gov_gate.get("hard_limits_respected", True)
        and opp_gate.get("hard_limits_respected", True),
        "stack_weights": policy["STACK_WEIGHTS"],
        "thresholds": {
            "hard_veto":          policy["GOVERNOR_HARD_VETO_THRESHOLD"],
            "dissent_conf_mult":  policy["GOVERNOR_DISSENT_CONF_MULT"],
            "dissent_size_mult":  policy["GOVERNOR_DISSENT_SIZE_MULT"],
            "min_executor_conf_floor": policy["MIN_EXECUTOR_CONF_FLOOR"],
            "opponent_influence": policy["OPPONENT_INFLUENCE"],
            "max_upweight":       policy["MAX_UPWEIGHT"],
            "max_downweight":     policy["MAX_DOWNWEIGHT"],
            "max_agent_influence": policy["MAX_SINGLE_AGENT_INFLUENCE"],
            "momentum_weighting": policy["MOMENTUM_WEIGHTING"],
        },
    }
    try:
        await db[SHARED_GOVERNANCE_DECISIONS].insert_one(governance_row)
    except Exception:  # noqa: BLE001
        # The governance ledger is for learning; don't let a write
        # failure kill the gate evaluation.
        pass

    return [gov_gate, opp_gate], final_size


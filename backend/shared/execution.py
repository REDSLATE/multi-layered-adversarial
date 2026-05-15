"""Execution router — intent → gate chain → broker.

Doctrine:
  * Brains never call this router. Operator JWT only.
  * Intent must hold the Executor seat at ingest AND now.
  * Every gate is logged. Block reasons are surfaced to the UI.
  * Caps are SOFTWARE; see `shared/exposure_caps.py`.
  * Order routing uses notional (dollar-amount) market day orders for
    the paper-trading phase — keeps caps trivially enforceable
    regardless of price discovery latency.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from auth import get_current_user
from db import db
from namespaces import (
    EXECUTION_RECEIPTS,
    SHARED_GATE_RESULTS,
    SHARED_GOVERNANCE_DECISIONS,
    SHARED_INTENTS,
    SHARED_RECEIPTS,
    SOVEREIGN_AUDIT_LOG,
)
from shared.broker.alpaca_routes import get_alpaca_adapter
from shared.exposure_caps import caps_snapshot, evaluate_all
from shared.executor_seat import get_executor_holder
from shared.mc_shelly import record_async


router = APIRouter(tags=["execution"])


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ───────────────────────────── council ─────────────────────────────
# Doctrine (rev3, 2026-02-15): the council is bound to SEATS, not brain
# identities. The Governor seat (whoever holds it) must record a stance
# on every Executor seat order. Dissent is not a binary block — it's a
# graduated verdict:
#
#   * HARD_VETO          → block (explicit veto bit set + governor conf
#                                  ≥ GOVERNOR_HARD_VETO_THRESHOLD)
#   * SOFT_DISSENT       → executor may override if its own conf ≥
#                          MIN_EXECUTOR_CONF_TO_OVERRIDE_SOFT_DISSENT.
#                          Override fires the trade at a reduced
#                          risk_multiplier (default 0.50).
#   * NO_DISSENT         → trade fires at full size.
#   * NO_STANCE / OFFLINE→ block. Governor must be HEARD before any
#                          intent fires (the missing-pushback bug).
#
# Every evaluation writes one row to `shared_governance_decisions` so
# Shelly / the stacks can later score who was right when the outcome
# resolves.

# Per-doctrine constants. Raise/lower these to retune the policy.
GOVERNOR_HARD_VETO_THRESHOLD = 0.85
GOVERNOR_SOFT_DISSENT_THRESHOLD = 0.55  # advisory; informs scoring
MIN_EXECUTOR_CONF_TO_OVERRIDE_SOFT_DISSENT = 0.72
SOFT_DISSENT_RISK_MULTIPLIER = 0.50

# How fresh a council signal must be to count.
_COUNCIL_FRESHNESS_SECONDS = 600  # 10 minutes
# How long the governor seat can be silent before we consider it offline.
_GOVERNOR_OFFLINE_THRESHOLD_SECONDS = 1800  # 30 minutes


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
async def _seat_holder(role: str) -> Optional[str]:
    """Current occupant of `role` in the live roster, or None if vacant."""
    from shared.roster import get_roster  # noqa: WPS433
    r = await get_roster()
    return (r.get("assignments") or {}).get(role)


async def _latest_governor_call(symbol: Optional[str]) -> tuple[Optional[str], Optional[dict]]:
    """(holder, doc) — most recent authority_call by the current Governor
    seat holder for `symbol`. Returns (None, None) if the seat is vacant."""
    holder = await _seat_holder("governor")
    if not holder or not symbol:
        return holder, None
    query = {"$and": [
        _brain_match_clause(holder),
        _authority_call_clause(),
        _symbol_clause(symbol),
    ]}
    doc = await db[SHARED_RECEIPTS].find_one(query, {"_id": 0}, sort=[("timestamp", -1)])
    return holder, doc


async def _latest_governor_any_call() -> tuple[Optional[str], Optional[dict]]:
    """(holder, doc) — most recent authority_call by Governor for ANY symbol.
    Used to distinguish 'governor offline' from 'governor uncertain on this name'."""
    holder = await _seat_holder("governor")
    if not holder:
        return holder, None
    query = {"$and": [_brain_match_clause(holder), _authority_call_clause()]}
    doc = await db[SHARED_RECEIPTS].find_one(query, {"_id": 0}, sort=[("timestamp", -1)])
    return holder, doc


async def _latest_opponent_contribution() -> tuple[Optional[str], Optional[dict]]:
    """(holder, doc) — most recent sovereign contribution by Opponent seat."""
    holder = await _seat_holder("opponent")
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
) -> dict:
    """Pure function: given the intent and the normalized governor call,
    return the verdict dict {allowed, reason, disagreement,
    record_pushback, risk_multiplier}.

    Doctrine: governor must be HEARD on every intent. Dissent must be
    LOGGED. Only hard veto BLOCKS. Soft dissent down-sizes a strong
    executor; blocks a weak one.
    """
    executor_conf = float(
        intent.get("confidence")
        or intent.get("calibrated_confidence")
        or 0.0
    )

    # Governor seat vacant — there is no one to be heard. Block.
    if not governor_holder:
        return {
            "allowed": False,
            "reason": "GOVERNOR_SEAT_VACANT",
            "disagreement": False,
            "record_pushback": False,
            "risk_multiplier": 0.0,
        }

    # Governor seat occupied but has not spoken (within freshness window)
    # on this symbol. The doctrine: every intent must have a recorded
    # stance from the Governor seat. Absence = degraded governance = block.
    if gov_norm is None:
        if not governor_alive:
            return {
                "allowed": False,
                "reason": "GOVERNOR_OFFLINE",
                "disagreement": False,
                "record_pushback": False,
                "risk_multiplier": 0.0,
            }
        return {
            "allowed": False,
            "reason": "GOVERNOR_NO_STANCE_ON_SYMBOL",
            "disagreement": False,
            "record_pushback": False,
            "risk_multiplier": 0.0,
        }

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
    if governor_veto and governor_conf >= GOVERNOR_HARD_VETO_THRESHOLD:
        return {
            "allowed": False,
            "reason": "GOVERNOR_HARD_VETO",
            "disagreement": True,
            "record_pushback": True,
            "risk_multiplier": 0.0,
        }

    # Soft dissent: executor may override if its own conviction is high.
    if disagreement:
        if executor_conf >= MIN_EXECUTOR_CONF_TO_OVERRIDE_SOFT_DISSENT:
            return {
                "allowed": True,
                "reason": "EXECUTOR_OVERRIDES_SOFT_DISSENT",
                "disagreement": True,
                "record_pushback": True,
                "risk_multiplier": SOFT_DISSENT_RISK_MULTIPLIER,
            }
        return {
            "allowed": False,
            "reason": "SOFT_DISSENT_LOW_EXECUTOR_CONF",
            "disagreement": True,
            "record_pushback": True,
            "risk_multiplier": 0.0,
        }

    # No dissent — full size.
    return {
        "allowed": True,
        "reason": "NO_GOVERNOR_DISSENT",
        "disagreement": False,
        "record_pushback": False,
        "risk_multiplier": 1.0,
    }


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
    executor_holder = await _seat_holder("executor")
    governor_holder, gov_doc = await _latest_governor_call(sym)
    gov_norm = _normalize_governor_call(gov_doc)

    if gov_norm is None:
        # No per-symbol call — is the governor alive at all?
        _, gov_any = await _latest_governor_any_call()
        governor_alive = _is_fresh(_doc_ts(gov_any), _GOVERNOR_OFFLINE_THRESHOLD_SECONDS)
        gov_any_ts = _doc_ts(gov_any)
    else:
        governor_alive = True
        gov_any_ts = gov_norm.get("ts")

    verdict = _governance_verdict(intent, gov_norm, governor_alive, governor_holder)

    # Build the gate row for the governor.
    gov_reason_text = {
        "GOVERNOR_HARD_VETO": (
            f"GOVERNOR ({governor_holder}) hard veto on {sym}: "
            f"conf={gov_norm.get('confidence') if gov_norm else 'n/a'} "
            f"≥ {GOVERNOR_HARD_VETO_THRESHOLD}"
        ),
        "SOFT_DISSENT_LOW_EXECUTOR_CONF": (
            f"GOVERNOR ({governor_holder}) dissented on {sym}; "
            f"executor ({executor_holder}) conf "
            f"{float(intent.get('confidence') or 0.0):.2f} "
            f"< override threshold {MIN_EXECUTOR_CONF_TO_OVERRIDE_SOFT_DISSENT}"
        ),
        "EXECUTOR_OVERRIDES_SOFT_DISSENT": (
            f"GOVERNOR ({governor_holder}) dissented but executor "
            f"({executor_holder}) overrode at conf "
            f"{float(intent.get('confidence') or 0.0):.2f} — "
            f"trade fires at risk×{SOFT_DISSENT_RISK_MULTIPLIER:.2f}"
        ),
        "NO_GOVERNOR_DISSENT": (
            f"GOVERNOR ({governor_holder}) recorded stance with no "
            f"dissent on {sym} — full size"
        ),
        "GOVERNOR_NO_STANCE_ON_SYMBOL": (
            f"GOVERNOR ({governor_holder}) is live but recorded no "
            f"stance on {sym} — governance must be heard"
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
    }

    # ── opponent_objection ─────────────────────────────────────────────
    # Seat-bound: queries whoever holds the Opponent seat. Advisory only
    # now — never hard-blocks. The opponent's view is captured in the
    # governance row and feeds the outcome learner.
    opponent_holder, opp_doc = await _latest_opponent_contribution()
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
            "passed": True,  # advisory; never blocks
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

    # ── Governance decision row (per-intent learning ledger) ──────────
    # Captures both seats' stances, the verdict, and the resulting
    # risk_multiplier. Shelly/outcomes can join on intent_id to score
    # who was right after the trade resolves.
    governance_row = {
        "ts": _now_iso(),
        "intent_id": intent_id,
        "symbol": sym,
        "lane": intent.get("lane"),
        "executor_seat_holder": executor_holder,
        "executor_action": action,
        "executor_confidence": float(intent.get("confidence") or 0.0),
        "governor_seat_holder": governor_holder,
        "governor_stance": (gov_norm or {}).get("stance"),
        "governor_executable": (gov_norm or {}).get("executable"),
        "governor_veto": (gov_norm or {}).get("veto"),
        "governor_confidence": (gov_norm or {}).get("confidence"),
        "governor_call_ts": (gov_norm or {}).get("ts"),
        "opponent_seat_holder": opp_gate.get("opponent_holder"),
        "opponent_confidence": opp_gate.get("opponent_conf"),
        "opponent_side": opp_gate.get("opponent_side"),
        "opponent_opposes": opp_gate.get("opponent_opposes"),
        "disagreement": verdict["disagreement"],
        "verdict_code": verdict["reason"],
        "final_allowed": verdict["allowed"],
        "risk_multiplier": verdict["risk_multiplier"],
        "thresholds": {
            "hard_veto": GOVERNOR_HARD_VETO_THRESHOLD,
            "soft_dissent": GOVERNOR_SOFT_DISSENT_THRESHOLD,
            "executor_override": MIN_EXECUTOR_CONF_TO_OVERRIDE_SOFT_DISSENT,
        },
    }
    try:
        await db[SHARED_GOVERNANCE_DECISIONS].insert_one(governance_row)
    except Exception:  # noqa: BLE001
        # The governance ledger is for learning; don't let a write
        # failure kill the gate evaluation.
        pass

    return [gov_gate, opp_gate], verdict["risk_multiplier"]


# ───────────────────────────── gate chain ─────────────────────────────

async def _evaluate_gates(intent: dict, order_notional_usd: float) -> dict:
    """Run the full gate chain for an intent.

    Returns:
        {
          "verdict": "would_pass" | "would_block",
          "gates": [{name, passed, reason}, ...],
          "order_notional_usd": float,
        }
    """
    gates: list[dict] = []

    # 1. Schema invariants — pinned by IntentIn validators.
    gates.append({
        "name": "schema_invariants",
        "passed": intent.get("may_execute") is False and intent.get("requires_gate_pass") is True,
        "reason": "may_execute pinned False; requires_gate_pass pinned True",
    })

    # 2. Action-routable check — only BUY/SELL/SHORT/COVER are routable.
    action = intent.get("action")
    routable = action in ("BUY", "SELL", "SHORT", "COVER")
    gates.append({
        "name": "action_routable",
        "passed": routable,
        "reason": (
            f"action {action!r} is routable to the broker"
            if routable else
            f"action {action!r} is not a routable order (HOLD/etc are watchlist signals)"
        ),
    })

    # 3. Executor seat — held at ingest AND still held now.
    #    The seat policy is also lane-scoped: a brain holding the equity
    #    `executor` seat cannot fire a crypto intent (and vice versa).
    #    Both checks must pass.
    from shared.seat_policy import seat_may_execute_lane  # noqa: WPS433
    from shared.executor_seat import (  # noqa: WPS433
        get_seat_holder,
        seats_with_execute,
    )
    intent_lane_for_seat = intent.get("lane")
    intent_stack = intent.get("stack")

    # Find any execute-capable seat that's lane-eligible AND currently
    # held by this intent's brain.
    eligible_seats = seats_with_execute(intent_lane_for_seat)
    current_holder = None
    matched_seat = None
    for seat_name in eligible_seats:
        holder = await get_seat_holder(seat_name)
        if holder == intent_stack:
            matched_seat = seat_name
            current_holder = holder
            break
    if current_holder is None:
        # Fall back to the legacy executor lookup so empty-seat / wrong-
        # lane scenarios produce useful messages.
        current_holder = await get_executor_holder()

    held_at_intent = bool(intent.get("holds_executor_seat"))
    held_at_post = intent.get("executor_holder_at_post")
    holds_now = matched_seat is not None
    # Lane-scope check: the matched seat's policy must allow this lane.
    lane_allowed = seat_may_execute_lane(matched_seat, intent_lane_for_seat)

    if holds_now and lane_allowed and held_at_intent:
        seat_pass, seat_reason = True, (
            f"{intent_stack} holds the {matched_seat!r} seat "
            f"(lane={intent_lane_for_seat or 'any'}); held at ingest"
        )
    elif holds_now and not lane_allowed:
        seat_pass, seat_reason = False, (
            f"{intent_stack} holds {matched_seat!r}, but seat does not authorize "
            f"lane={intent_lane_for_seat!r} — wrong-lane seat blocked"
        )
    elif held_at_intent and not holds_now:
        seat_pass, seat_reason = False, (
            f"{intent_stack} held an execute-seat at ingest but no longer "
            f"holds one matching lane={intent_lane_for_seat!r}"
        )
    elif not held_at_intent and held_at_post is None:
        seat_pass, seat_reason = False, (
            "Execute-seat was EMPTY when intent was posted — no authority"
        )
    else:
        seat_pass, seat_reason = False, (
            f"Execute-seat was held by {held_at_post} at post time, not {intent_stack}"
        )
    gates.append({"name": "executor_seat_check", "passed": seat_pass, "reason": seat_reason})

    # 4. Live-trading-disabled (paper mode).
    gates.append({
        "name": "live_trading_disabled",
        "passed": True,
        "reason": "LIVE_TRADING_ENABLED stays False — paper broker only",
    })

    # 5. Broker connected — lane-aware. Equity intents need Alpaca;
    #    crypto intents need Kraken. If lane is unknown the resolver
    #    fails closed when routing — surfaced as a separate gate failure.
    intent_lane = intent.get("lane")
    if intent_lane:
        from shared.broker_router import adapter_for_lane as _adapter_for_lane  # noqa: WPS433
        broker_for_intent = await _adapter_for_lane(intent_lane)
        broker_connected = broker_for_intent is not None
        broker_reason = (
            f"broker for lane={intent_lane!r} present ({broker_for_intent.name})"
            if broker_connected else
            f"no broker configured / connected for lane={intent_lane!r}"
        )
    else:
        # Legacy intents without lane fall back to the Alpaca check —
        # this keeps the equities flow alive for any pre-canonical
        # intents already queued in the DB.
        adapter = await get_alpaca_adapter()
        broker_connected = adapter is not None
        broker_reason = (
            "Alpaca paper adapter present (legacy / lane-untagged intent)"
            if broker_connected else
            "lane missing AND Alpaca not connected — NO_TRADE"
        )
    gates.append({
        "name": "broker_connected",
        "passed": broker_connected,
        "reason": broker_reason,
    })

    # ─── 6a. Council enforcement ──────────────────────────────────────
    # Doctrine (rev3, 2026-02-15): SEAT-BOUND graduated verdict. The
    # Governor seat holder's most-recent stance shapes the verdict;
    # only HARD_VETO blocks. Soft dissent down-sizes a strong executor
    # via `risk_multiplier`. See `_evaluate_council` for the policy.
    council_gates, risk_multiplier = await _evaluate_council(intent)
    gates.extend(council_gates)

    # If the council asked for a reduced size, reflect that in the
    # notional that subsequent gates and the broker see. Caps evaluate
    # against the dropped notional so they never accidentally lift
    # under reduced-size trades.
    effective_notional = order_notional_usd * risk_multiplier if risk_multiplier > 0 else order_notional_usd

    # 6b. Hard exposure caps. Lane-aware: crypto gets the $10/order cap;
    #    equities get the lifted global cap.
    side = action or ""
    cap_evals = await evaluate_all(effective_notional, side, lane=intent.get("lane"))
    for c in cap_evals:
        gates.append({"name": c.name, "passed": c.passed, "reason": c.reason})

    verdict = "would_pass" if all(g["passed"] for g in gates) else "would_block"

    # MC Shelly — one row per gate, tagged with intent context. Lets
    # the operator slice training data by "which gate fails most when
    # the OPP is in seat" type questions.
    for g in gates:
        record_async(
            event_type="gate_pass" if g["passed"] else "gate_fail",
            brain=intent.get("stack"),
            symbol=intent.get("symbol"),
            action=intent.get("action"),
            outcome="pass" if g["passed"] else "fail",
            rationale=g.get("reason"),
            ref_id=intent.get("intent_id"),
            gate_name=g.get("name"),
        )

    return {
        "verdict": verdict,
        "gates": gates,
        "order_notional_usd": order_notional_usd,
        "effective_notional_usd": effective_notional,
        "risk_multiplier": risk_multiplier,
        "caps": caps_snapshot(),
    }


# ───────────────────────────── dry-run ─────────────────────────────

@router.post("/execution/dry_run")
async def execution_dry_run(
    intent_id: str = Query(..., description="intent_id to evaluate"),
    order_notional_usd: float = Query(
        default=10.0,
        ge=0.01,
        le=10_000.0,
        description="proposed order notional in USD (defaults to the per-order cap)",
    ),
    user: dict = Depends(get_current_user),  # noqa: B008
):
    """Evaluate the full gate chain WITHOUT placing an order."""
    intent = await db[SHARED_INTENTS].find_one({"intent_id": intent_id}, {"_id": 0})
    if not intent:
        raise HTTPException(status_code=404, detail=f"intent {intent_id} not found")

    result = await _evaluate_gates(intent, order_notional_usd)
    new_state = "dry_run_passed" if result["verdict"] == "would_pass" else "dry_run_blocked"
    await db[SHARED_INTENTS].update_one(
        {"intent_id": intent_id},
        {"$set": {
            "gate_state": new_state,
            "last_dry_run_ts": _now_iso(),
            "last_dry_run_by": user.get("email"),
            "last_dry_run_notional_usd": order_notional_usd,
        }},
    )
    await db[SHARED_GATE_RESULTS].insert_one({
        "intent_id": intent_id,
        "kind": "dry_run",
        "ts": _now_iso(),
        "by": user.get("email"),
        "order_notional_usd": order_notional_usd,
        "verdict": result["verdict"],
        "gates": result["gates"],
    })

    return {
        "intent_id": intent_id,
        "evaluated_by": user.get("email"),
        "ts": _now_iso(),
        **result,
    }


# ───────────────────────────── submit ─────────────────────────────

class SubmitBody(BaseModel):
    intent_id: str = Field(..., min_length=8, max_length=80)
    order_notional_usd: float = Field(default=10.0, ge=0.01, le=10_000.0)
    confirm: str = Field(default="", description="must equal 'execute' to actually route")


@router.post("/execution/submit")
async def execution_submit(
    body: SubmitBody,
    user: dict = Depends(get_current_user),  # noqa: B008
):
    """Route the intent through the gate chain and, if it passes,
    submit a market-day notional order to the broker.

    Idempotency: each intent can be executed AT MOST ONCE. Re-submits
    are rejected with 409.
    """
    if body.confirm != "execute":
        raise HTTPException(
            status_code=400,
            detail="confirmation phrase missing — set confirm='execute' to route this order",
        )

    intent = await db[SHARED_INTENTS].find_one({"intent_id": body.intent_id}, {"_id": 0})
    if not intent:
        raise HTTPException(status_code=404, detail=f"intent {body.intent_id} not found")
    if intent.get("executed"):
        raise HTTPException(
            status_code=409,
            detail=f"intent {body.intent_id} already executed at {intent.get('executed_at')}",
        )

    # Re-run the gate chain at submit time — state may have shifted
    # between the dry-run and the click (seat rotated, caps changed,
    # broker disconnected).
    result = await _evaluate_gates(intent, body.order_notional_usd)
    if result["verdict"] != "would_pass":
        # Audit-log the block so the operator can see why on the page.
        await db[SHARED_GATE_RESULTS].insert_one({
            "intent_id": body.intent_id,
            "kind": "submit_blocked",
            "ts": _now_iso(),
            "by": user.get("email"),
            "order_notional_usd": body.order_notional_usd,
            "verdict": result["verdict"],
            "gates": result["gates"],
        })
        await db[SHARED_INTENTS].update_one(
            {"intent_id": body.intent_id},
            {"$set": {
                "gate_state": "blocked",
                "last_submit_ts": _now_iso(),
                "last_submit_by": user.get("email"),
            }},
        )
        # Pick the first failing gate as the surface reason.
        first_block = next((g for g in result["gates"] if not g["passed"]), None)
        raise HTTPException(
            status_code=403,
            detail={
                "blocked_by": first_block["name"] if first_block else "unknown",
                "reason": first_block["reason"] if first_block else "gate chain blocked",
                "gates": result["gates"],
            },
        )

    # All gates passed — route the order via the broker router (lane-aware).
    side = "BUY" if intent["action"] in ("BUY", "COVER") else "SELL"
    client_order_id = f"mc-{body.intent_id[:8]}-{uuid.uuid4().hex[:6]}"

    try:
        from shared.broker_router import BrokerRouteBlocked as _Blocked  # noqa: WPS433
        from shared.broker_router import route_order as _route_order  # noqa: WPS433
        order = await _route_order(
            intent,
            notional_usd=body.order_notional_usd,
            client_order_id=client_order_id,
        )
    except _Blocked as e:
        await db[SHARED_GATE_RESULTS].insert_one({
            "intent_id": body.intent_id,
            "kind": "submit_no_trade",
            "ts": _now_iso(),
            "by": user.get("email"),
            "reason": str(e),
        })
        record_async(
            event_type="order_rejected",
            brain=intent.get("stack"),
            symbol=intent.get("symbol"),
            action=intent.get("action"),
            outcome="no_trade",
            error_reason=str(e),
            ref_id=body.intent_id,
        )
        raise HTTPException(
            status_code=403,
            detail={"blocked_by": "broker_router", "reason": str(e)},
        ) from e
    except Exception as e:  # noqa: BLE001
        await db[SHARED_GATE_RESULTS].insert_one({
            "intent_id": body.intent_id,
            "kind": "submit_error",
            "ts": _now_iso(),
            "by": user.get("email"),
            "error": str(e),
        })
        record_async(
            event_type="order_rejected",
            brain=intent.get("stack"),
            symbol=intent.get("symbol"),
            action=intent.get("action"),
            outcome="rejected",
            error_reason=str(e),
            ref_id=body.intent_id,
        )
        raise HTTPException(status_code=502, detail=f"broker rejected order: {e}") from e

    now = _now_iso()
    receipt = {
        "receipt_id": str(uuid.uuid4()),
        "intent_id": body.intent_id,
        "stack": intent.get("stack"),
        "symbol": intent.get("symbol"),
        "canonical": order.get("canonical"),
        "lane": order.get("lane"),
        "broker_symbol": order.get("broker_symbol"),
        "action": intent.get("action"),
        "side": side,
        "notional_usd": float(body.order_notional_usd),
        "broker": order.get("broker", "unknown"),
        "broker_order_id": order["order_id"],
        "client_order_id": order.get("client_order_id"),
        "status": order.get("status"),
        "submitted_at": order.get("submitted_at") or now,
        "filled_at": order.get("filled_at"),
        "filled_qty": order.get("filled_qty", 0.0),
        "filled_avg_price": order.get("filled_avg_price"),
        "executed_at": now,
        "executed_by": user.get("email"),
        "gates_passed": result["gates"],
    }
    await db[EXECUTION_RECEIPTS].insert_one(receipt)
    await db[SHARED_INTENTS].update_one(
        {"intent_id": body.intent_id},
        {"$set": {
            "executed": True,
            "executed_at": now,
            "execution_receipt_id": receipt["receipt_id"],
            "broker_order_id": order["order_id"],
            "gate_state": "passed",
            "last_submit_ts": now,
            "last_submit_by": user.get("email"),
        }},
    )
    await db[SHARED_GATE_RESULTS].insert_one({
        "intent_id": body.intent_id,
        "kind": "submit_passed",
        "ts": now,
        "by": user.get("email"),
        "order_notional_usd": float(body.order_notional_usd),
        "broker_order_id": order["order_id"],
        "gates": result["gates"],
    })

    # MC Shelly — record the order routing. Position = EXE by definition
    # (only the executor-seat brain reaches this code path).
    record_async(
        event_type="order_routed",
        brain=intent.get("stack"),
        symbol=intent.get("symbol"),
        action=intent.get("action"),
        outcome="executed",
        ref_id=receipt["receipt_id"],
        extra={
            "broker_order_id": order["order_id"],
            "notional_usd": float(body.order_notional_usd),
            "status": order.get("status"),
        },
    )

    return {
        "ok": True,
        "intent_id": body.intent_id,
        "receipt": receipt,
        "order": order,
        "verdict": "executed",
    }


# ───────────────────────────── receipts ─────────────────────────────

@router.get("/execution/receipts")
async def list_receipts(
    limit: int = Query(default=50, ge=1, le=500),
    intent_id: Optional[str] = Query(default=None),
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    q: dict = {}
    if intent_id:
        q["intent_id"] = intent_id
    rows = (
        await db[EXECUTION_RECEIPTS]
        .find(q, {"_id": 0})
        .sort("executed_at", -1)
        .to_list(limit)
    )
    return {"items": rows, "count": len(rows), "caps": caps_snapshot()}


@router.get("/execution/caps")
async def caps_status(_user: dict = Depends(get_current_user)):  # noqa: B008
    """Operator view of the hard caps + current consumption."""
    from shared.exposure_caps import daily_spend_usd, open_notional_usd  # noqa: WPS433
    spent = await daily_spend_usd()
    open_ = await open_notional_usd()
    caps = caps_snapshot()
    return {
        "caps": caps,
        "today": {
            "spent_usd": spent,
            "remaining_usd": max(0.0, caps["per_day_usd"] - spent),
        },
        "open": {
            "open_notional_usd": open_,
            "remaining_usd": max(0.0, caps["open_notional_usd"] - open_),
        },
    }



# ──────────────────── council lookup diagnostic ────────────────────
# Operator-facing debug endpoint: shows EXACTLY what the executor's
# seat-bound council gates see for a symbol — who holds Governor /
# Opponent right now, what those occupants last said, and the
# resulting graduated verdict. Use this to verify governance is being
# heard before deploying changes.

@router.get("/admin/council/lookup-debug")
async def council_lookup_debug(
    symbol: str = Query(..., min_length=1, max_length=32),
    executor_confidence: float = Query(
        default=0.7, ge=0.0, le=1.0,
        description="simulated executor conviction to test the verdict against",
    ),
    action: str = Query(default="BUY", description="simulated intent action"),
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    """Returns who holds each seat, what they last said, and the
    graduated verdict that would fire for a hypothetical intent at
    `executor_confidence`. This makes seat-binding visible: switch the
    Governor seat to a different brain and re-hit this endpoint to see
    the verdict flip."""
    governor_holder, gov_doc = await _latest_governor_call(symbol)
    _, gov_any = await _latest_governor_any_call()
    opponent_holder, opp_doc = await _latest_opponent_contribution()
    executor_holder = await _seat_holder("executor")
    gov_norm = _normalize_governor_call(gov_doc)
    gov_any_ts = _doc_ts(gov_any)
    governor_alive = _is_fresh(gov_any_ts, _GOVERNOR_OFFLINE_THRESHOLD_SECONDS)

    # Compute the verdict a real intent would receive.
    sim_intent = {
        "intent_id": "diagnostic-sim",
        "symbol": symbol,
        "action": action.upper(),
        "confidence": executor_confidence,
        "stack": executor_holder,
    }
    verdict = _governance_verdict(sim_intent, gov_norm, governor_alive, governor_holder)

    # Collection health: counts under the CURRENT seat occupants.
    gov_total = 0
    if governor_holder:
        gov_total = await db[SHARED_RECEIPTS].count_documents(
            {"$and": [_brain_match_clause(governor_holder), _authority_call_clause()]}
        )
    opp_total = 0
    if opponent_holder:
        opp_total = await db[SOVEREIGN_AUDIT_LOG].count_documents(
            {"$and": [_brain_match_clause(opponent_holder), _contribution_clause()]}
        )

    return {
        "symbol": symbol,
        "seats": {
            "executor": executor_holder,
            "governor": governor_holder,
            "opponent": opponent_holder,
        },
        "collection_health": {
            "shared_receipts_collection": SHARED_RECEIPTS,
            "governor_authority_call_total": gov_total,
            "sovereign_audit_collection": SOVEREIGN_AUDIT_LOG,
            "opponent_entries_total": opp_total,
        },
        "governor": {
            "holder": governor_holder,
            "call_found_for_symbol": gov_doc is not None,
            "normalized": gov_norm,
            "raw_doc": gov_doc,
            "any_recent_call_ts": gov_any_ts,
            "governor_alive": governor_alive,
            "governor_offline_threshold_seconds": _GOVERNOR_OFFLINE_THRESHOLD_SECONDS,
        },
        "opponent": {
            "holder": opponent_holder,
            "doc_found": opp_doc is not None,
            "doc_ts": _doc_ts(opp_doc),
            "fresh": _is_fresh(_doc_ts(opp_doc)),
            "raw_doc": opp_doc,
            "freshness_window_seconds": _COUNCIL_FRESHNESS_SECONDS,
        },
        "simulated_verdict": {
            "input_executor_confidence": executor_confidence,
            "input_action": action.upper(),
            **verdict,
        },
        "policy_thresholds": {
            "GOVERNOR_HARD_VETO_THRESHOLD": GOVERNOR_HARD_VETO_THRESHOLD,
            "GOVERNOR_SOFT_DISSENT_THRESHOLD": GOVERNOR_SOFT_DISSENT_THRESHOLD,
            "MIN_EXECUTOR_CONF_TO_OVERRIDE_SOFT_DISSENT": MIN_EXECUTOR_CONF_TO_OVERRIDE_SOFT_DISSENT,
            "SOFT_DISSENT_RISK_MULTIPLIER": SOFT_DISSENT_RISK_MULTIPLIER,
        },
    }

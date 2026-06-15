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

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from auth import get_current_user
from db import db
from namespaces import (
    EXECUTION_RECEIPTS,
    PATTERNS_UNIVERSE,
    SHARED_GATE_RESULTS,
    SHARED_INTENTS,
    SHARED_RECEIPTS,
    SOVEREIGN_AUDIT_LOG,
)
# Council doctrine and helpers were extracted 2026-02-15 to
# `shared/council.py` to keep this module under control (was 1355
# lines). We import the helpers used by the gate chain and the
# diagnostic endpoint here.
from shared.council import (
    COUNCIL_POLICY,
    _COUNCIL_FRESHNESS_SECONDS,
    _GOVERNOR_OFFLINE_THRESHOLD_SECONDS,
    _authority_call_clause,
    _brain_match_clause,
    _contribution_clause,
    _doc_ts,
    _evaluate_council,
    _governance_verdict,
    _is_fresh,
    _latest_governor_any_call,
    _latest_governor_call,
    _latest_opponent_contribution,
    _normalize_governor_call,
    _policy_for_lane,
    _seat_holder,
)
from shared.exposure_caps import caps_snapshot, evaluate_all
from shared.mc_shelly import record_async
from shared.runtime.paradox_record import write_paradox_record


router = APIRouter(tags=["execution"])


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ───────────────────────────── gate chain ─────────────────────────────

# Hard gates that the operator override cannot bypass. Per operator
# directive (2026-02-19, "Override EVERYTHING except the $10 per-ticker
# cap + freeze"), the ONLY gates that stay authoritative under the
# operator override are the exposure caps (money safety). The freeze
# and the Webull $1-$10 pre-trade cap live in `broker_router` and are
# enforced there regardless of this flag — they're hard by location.
#
# Everything else in `_evaluate_gates` (schema_invariants,
# action_routable, broker_connected, lane_execution_enabled,
# executor_seat_check, symbol_in_universe, roadguard_spread_floor,
# rr_ratio_floor, council_*, position_aware_*) is overridable. The
# operator owns the trade.
_HARD_GATES_NEVER_OVERRIDABLE = frozenset({
    "cap_per_order",
    "cap_open_notional",
    "cap_per_day",
    "cap_per_order_lane",
    "cap_open_notional_lane",
    "cap_per_day_lane",
})


async def _evaluate_gates(
    intent: dict,
    order_notional_usd: float,
    *,
    operator_override: bool = False,
    override_reason: str = "",
) -> dict:
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

    # ─── 2b. Position-aware intent classification (2026-06-10, P2) ────
    # Doctrine pin: the operator deferred this in 2026-06-09 while live
    # trading was on prod. This is the integration point referenced in
    # `shared/position_model.py` docstring. The gate compares the brain's
    # stated `position_evolution` against the classifier's verdict using
    # the LIVE broker position from `position_context`. Disagreement
    # → misread row written + gate FAILS (block) when enforcement is
    # active, else audit-only (records but passes).
    #
    # Enforcement is operator-controlled via
    # `POST /api/admin/position-misreads/enforcement` (mode: block | audit_only).
    # Default = audit_only so the system observes its own misreads
    # before being trusted to block on them.
    #
    # Safety:
    #   * If position_context can't be resolved (broker offline, no
    #     symbol on intent, etc.) the gate PASSES with "no inventory
    #     data" — we DON'T fail-closed because the equity bootstrap
    #     path can run without a live adapter.
    #   * Only routable intents reach this gate (action_routable above
    #     would already have blocked HOLD/etc).
    if routable:
        from shared.position_model import (  # noqa: WPS433
            PositionState, PositionSide, classify_intent, IntentType,
            detect_misread, MISREAD_COLLECTION,
        )
        from shared.position_context import (  # noqa: WPS433
            get_position_context,
        )
        from routes.position_misread_admin import (  # noqa: WPS433
            is_misread_enforcement_enabled,
        )
        sym_for_pa = (intent.get("symbol") or "").strip()
        lane_for_pa = (intent.get("lane") or "").strip()
        intended_qty = float(intent.get("qty") or intent.get("quantity") or 0.0)
        pa_passed = True
        pa_reason = "no inventory data; position-aware check skipped"
        pa_misread: Optional[dict] = None
        try:
            if sym_for_pa and lane_for_pa and intended_qty > 0:
                pos_ctx = await get_position_context(sym_for_pa, lane_for_pa)
                actual_side_str = (pos_ctx.get("current_side") or "flat").lower()
                actual_signed = float(pos_ctx.get("signed_qty") or 0.0)
                try:
                    actual_side = PositionSide(actual_side_str)
                except ValueError:
                    actual_side = PositionSide.FLAT
                current_state = PositionState(
                    symbol=sym_for_pa,
                    signed_qty=actual_signed,
                )
                # Authoritative classification from the broker truth.
                truth_intent = classify_intent(action, intended_qty, current_state)

                # Compare against what the brain claimed via
                # `position_evolution`. Map evolution tags to IntentType
                # for a like-for-like comparison.
                claimed_evo = (intent.get("position_evolution") or "").lower().strip()
                _EVO_TO_INTENT = {
                    "open": IntentType.OPEN,
                    "add": IntentType.ADD,
                    "reduce": IntentType.REDUCE,
                    "close": IntentType.CLOSE,
                    "partial_cover": IntentType.REDUCE,
                    "full_cover": IntentType.CLOSE,
                    "scale_in": IntentType.ADD,
                    "scale_out": IntentType.REDUCE,
                    "flip": IntentType.FLIP,
                }
                claimed_intent = _EVO_TO_INTENT.get(claimed_evo)

                # The brain's `current_side` was already stamped on
                # the intent at brain-tick time. That's the "what the
                # brain thought" half of the misread signature.
                assumed_side_str = (
                    intent.get("current_side")
                    or intent.get("assumed_side")
                    or "flat"
                ).lower()
                try:
                    assumed_side = PositionSide(assumed_side_str)
                except ValueError:
                    assumed_side = PositionSide.FLAT

                # If we have BOTH a claim and a truth, compare them.
                # When the brain didn't carry a claim, we still write
                # the misread row but skip the disagreement gate fail
                # (no way to disagree with a missing claim).
                disagrees = (
                    claimed_intent is not None
                    and claimed_intent != truth_intent
                )

                # ALSO compute the misread separately — `detect_misread`
                # has stronger logic for the side-disagreement axis
                # (the AAPL-specific case where brain thought FLAT but
                # broker showed SHORT).
                misread = detect_misread(
                    emitted_action=action,
                    assumed_side=assumed_side,
                    actual=current_state,
                    brain=str(intent.get("stack") or ""),
                    lane=lane_for_pa,
                    intended_qty=intended_qty,
                    note=(
                        f"intent_id={intent.get('intent_id')} "
                        f"claimed_evo={claimed_evo or '∅'} "
                        f"setup_score={intent.get('setup_score')}"
                    ),
                )

                if misread is not None or disagrees:
                    enforce = await is_misread_enforcement_enabled()
                    if misread is not None:
                        # Persist the misread row (fire-and-forget; never
                        # blocks the gate decision).
                        from db import db as _db  # noqa: WPS433
                        try:
                            await _db[MISREAD_COLLECTION].insert_one(
                                misread.to_doc(),
                            )
                            pa_misread = misread.to_doc()
                        except Exception:  # noqa: BLE001
                            pass
                    if enforce:
                        pa_passed = False
                        pa_reason = (
                            f"POSITION_MISREAD — brain claimed "
                            f"evolution={claimed_evo or '∅'} (intent="
                            f"{claimed_intent.value if claimed_intent else '∅'}"
                            f"), broker truth says "
                            f"intent={truth_intent.value} (side="
                            f"{actual_side.value}, signed_qty="
                            f"{actual_signed:.4f}). "
                            f"Enforcement is ACTIVE — refusing to route."
                        )
                    else:
                        pa_reason = (
                            f"position_misread_observed (audit_only) — "
                            f"brain claimed evolution={claimed_evo or '∅'} "
                            f"(intent="
                            f"{claimed_intent.value if claimed_intent else '∅'}"
                            f"), broker truth says intent="
                            f"{truth_intent.value} (side="
                            f"{actual_side.value}, signed_qty="
                            f"{actual_signed:.4f}). "
                            f"Recorded to shared_position_misreads."
                        )
                else:
                    pa_reason = (
                        f"position-aware check agrees: action={action} "
                        f"against side={actual_side.value} "
                        f"(signed_qty={actual_signed:.4f}) → "
                        f"intent={truth_intent.value}"
                    )
        except Exception as exc:  # noqa: BLE001
            # Position lookup itself failed — log via reason but don't
            # block. The dedupe / sizing / cap gates still own safety.
            pa_passed = True
            pa_reason = (
                f"position-aware check unavailable: {type(exc).__name__}: {exc!s} "
                f"— passing through; other gates still authoritative"
            )
        gate_row = {
            "name": "position_aware_intent_classification",
            "passed": pa_passed,
            "reason": pa_reason,
        }
        if pa_misread is not None:
            gate_row["misread"] = pa_misread
        gates.append(gate_row)

    # 3. Executor seat — POSITION model (Doctrine, 2026-05-28).
    #    Authority lives in the SEAT, not in any specific brain.
    #    Whichever brain currently holds an execute-capable seat for
    #    this intent's lane has the authority to route. The brain that
    #    posted the intent is informational only.
    #
    #    Prior to this revision, the gate required
    #    `holder == intent.stack` AND `held_at_post == intent.stack`,
    #    which effectively bound authority to the brain at post-time.
    #    That made rotation useless: pending intents emitted while
    #    Camaro held the seat could not execute after the operator
    #    swapped Camaro out, because they were stamped
    #    `executor_holder_at_post=camaro` and would only pass if
    #    Camaro re-took the seat. Operator confirmed (2026-05-28) the
    #    correct doctrine is position-only: seat = authority.
    #
    #    `holds_executor_seat` and `executor_holder_at_post` continue
    #    to be stamped on every intent for the audit trail, but they
    #    no longer participate in the gate decision.
    from shared.seat_policy import seat_may_execute_lane  # noqa: WPS433
    from shared.executor_seat import (  # noqa: WPS433
        get_seat_holder,
        seats_with_execute,
    )
    intent_lane_for_seat = intent.get("lane")
    intent_stack = intent.get("stack")

    # Find ANY execute-capable seat for this lane that is currently held.
    eligible_seats = seats_with_execute(intent_lane_for_seat)
    current_holder = None
    matched_seat = None
    for seat_name in eligible_seats:
        holder = await get_seat_holder(seat_name)
        if holder:
            matched_seat = seat_name
            current_holder = holder
            break

    holds_now = matched_seat is not None
    lane_allowed = seat_may_execute_lane(matched_seat, intent_lane_for_seat)

    if holds_now and lane_allowed:
        seat_pass, seat_reason = True, (
            f"executor seat for lane={intent_lane_for_seat or 'any'!r} held by "
            f"{current_holder!r} (seat={matched_seat!r}); position-model authority — "
            f"intent posted by {intent_stack!r}"
        )
    elif holds_now and not lane_allowed:
        seat_pass, seat_reason = False, (
            f"{current_holder!r} holds {matched_seat!r}, but seat does not authorize "
            f"lane={intent_lane_for_seat!r} — wrong-lane seat blocked"
        )
    else:
        seat_pass, seat_reason = False, (
            f"executor seat for lane={intent_lane_for_seat!r} is vacant — "
            f"no authority to route. Assign via POST /api/executor/rotate "
            f"(or roster assignment for crypto seats)."
        )
    gates.append({"name": "executor_seat_check", "passed": seat_pass, "reason": seat_reason})

    # 4. Live-trading-disabled (DEFANGED 2026-02-17).
    #    This gate used to assert "LIVE_TRADING_ENABLED stays False" and
    #    surface paper-only messaging. Per operator order, all phantom
    #    "blocked" / "paper-only" / "observation-only" enforcements are
    #    removed. The gate is retained for the receipt schema's stability
    #    (downstream consumers still look for the named gate row) but it
    #    is now a no-op pass with a neutral reason.
    gates.append({
        "name": "live_trading_disabled",
        "passed": True,
        "reason": "live order routing enabled — seat policy is the authority",
    })

    # 5. Broker connected — lane-aware AND override-aware (2026-06-10).
    #    Equity intents normally need Alpaca/Public; crypto intents need
    #    Kraken. An intent carrying `broker_override` (e.g. "webull")
    #    routes through that broker instead — the gate MUST check the
    #    broker the LIVE path will actually use, not just the lane
    #    default. Before the override hand-off this gate would block
    #    Webull-routed intents on missing Public.com config even though
    #    the live route doesn't touch Public.
    intent_lane = intent.get("lane")
    intent_override = (intent.get("broker_override") or "").strip().lower() or None
    if intent_lane:
        from shared.broker_router import adapter_for_lane as _adapter_for_lane  # noqa: WPS433
        broker_for_intent = await _adapter_for_lane(intent_lane, intent_override)
        broker_connected = broker_for_intent is not None
        if broker_connected:
            override_tag = (
                f" (override→{intent_override})"
                if intent_override and intent_override == broker_for_intent.name
                else ""
            )
            broker_reason = (
                f"broker for lane={intent_lane!r} present "
                f"({broker_for_intent.name}){override_tag}"
            )
        else:
            override_tag = (
                f", override={intent_override!r}"
                if intent_override else ""
            )
            broker_reason = (
                f"no broker configured / connected for lane={intent_lane!r}"
                f"{override_tag}"
            )
    else:
        # Lane-less intents are NO_TRADE post-Alpaca-deprecation
        # (2026-02-19). Every intent MUST carry a lane so the router
        # can resolve the correct broker. Pre-canonical intents
        # without a lane fail-closed.
        broker_connected = False
        broker_reason = "intent missing lane — NO_TRADE (Alpaca legacy fallback removed)"
    gates.append({
        "name": "broker_connected",
        "passed": broker_connected,
        "reason": broker_reason,
    })

    # ─── 5c. Symbol-in-universe (2026-02-19) ─────────────────────────
    # Canonical boundary: every intent's symbol MUST be in MC's
    # `patterns_universe`, with a `lane` that matches the intent's
    # lane. Doctrine (c): MC verifies boundaries, brains propose.
    # This gate gives MC the lever to control WHAT brains are allowed
    # to propose without modifying any brain code — a single operator
    # curl adds or removes a tradeable symbol fleet-wide.
    #
    # Backward-compat: any row in `patterns_universe` without a `lane`
    # field is treated as equity (matches the legacy semantic). The
    # boot-seed in server.py backfills `lane` onto pre-existing rows
    # so this is a one-deploy migration with no operator action.
    from namespaces import PATTERNS_UNIVERSE  # noqa: WPS433
    from shared.broker_symbol_resolver import _strip_canonical_prefix  # noqa: WPS433
    # 2026-02-19 (operator unification): the gate keys on bare tickers
    # because `patterns_universe` stores bare tickers ("AAPL", not
    # "EQ:AAPL"). Manual injections sometimes carry the already-
    # canonical form ("EQ:AAPL", "CRYPTO:BTC-USD"). Strip the prefix
    # here so the lookup is shape-agnostic.
    intent_symbol = _strip_canonical_prefix(
        (intent.get("symbol") or "").upper().strip(),
    )
    intent_lane_for_universe = (intent_lane or "").lower().strip()
    universe_row = await db[PATTERNS_UNIVERSE].find_one(
        {"symbol": intent_symbol, "active": {"$ne": False}},
        {"_id": 0, "symbol": 1, "lane": 1, "active": 1},
    )
    if not universe_row:
        univ_pass = False
        univ_reason = (
            f"symbol {intent_symbol!r} is not in MC's active "
            f"`patterns_universe`. Add via "
            f"`POST /api/admin/patterns/universe` "
            f'{{\"symbol\":\"{intent_symbol}\",\"lane\":'
            f'\"{intent_lane_for_universe or "equity"}\"}} '
            f"before this intent can route."
        )
    else:
        row_lane = (universe_row.get("lane") or "equity").lower().strip()
        if not intent_lane_for_universe:
            # Legacy lane-untagged intent. Accept against any
            # universe lane to preserve the equity-bootstrap path —
            # the `broker_connected` gate above already forces
            # Alpaca for these.
            univ_pass = True
            univ_reason = (
                f"symbol {intent_symbol!r} in universe "
                f"(lane={row_lane!r}); intent has no lane tag — "
                f"legacy fallback accepted"
            )
        elif row_lane == intent_lane_for_universe:
            univ_pass = True
            univ_reason = (
                f"symbol {intent_symbol!r} in universe with "
                f"lane={row_lane!r}, matches intent lane"
            )
        else:
            univ_pass = False
            univ_reason = (
                f"symbol {intent_symbol!r} is in universe but "
                f"lane mismatch — universe says {row_lane!r}, "
                f"intent says {intent_lane_for_universe!r}. "
                f"Re-tag the symbol or the intent."
            )
    gates.append({
        "name": "symbol_in_universe",
        "passed": univ_pass,
        "reason": univ_reason,
    })

    # ─── 5b. Lane execution toggle (2026-02-18) ──────────────────────
    # Operator-owned kill switch. Decoupled from broker credential
    # state. Default OFF — execution must be explicitly enabled per
    # lane. The credential could be live and validated, but the
    # operator still gates routing through this toggle.
    from shared.lane_execution import is_lane_execution_enabled  # noqa: WPS433
    lane_for_toggle = intent_lane or "equity"
    lane_exec_enabled = await is_lane_execution_enabled(lane_for_toggle)
    gates.append({
        "name": "lane_execution_enabled",
        "passed": lane_exec_enabled,
        "reason": (
            f"operator has enabled execution for lane={lane_for_toggle!r}"
            if lane_exec_enabled else
            f"operator has NOT enabled execution for lane={lane_for_toggle!r} "
            f"— flip via POST /api/admin/execution/lane-toggles"
        ),
    })

    # ─── 6.0 RoadGuard — deterministic market-structure caps ──────────
    # Doctrine (c, 2026-05-20): RoadGuard owns "is this market safe to
    # trade RIGHT NOW" as PURE MATH. No opinions, no calibrations, no
    # brain involvement. Governor dampens within the safe zone;
    # RoadGuard kills if the market structure itself is unsafe.
    #
    # Caps are per-lane and intentionally generous — the goal is to
    # catch broken / illiquid / chaotic markets, not to second-guess
    # the brain's edge. Sub-cap conditions are sized down by Governor
    # (not killed) so the system can still learn from marginal fills.
    snapshot = intent.get("snapshot") or {}
    spread_bps_raw = snapshot.get("spread_bps")
    LANE_SPREAD_CAP = {
        "crypto": 200.0,   # 2.00% — only kill truly broken crypto markets
        "equity": 50.0,    # 0.50% — equities should be much tighter
    }
    lane_for_roadguard = intent_lane or "equity"
    spread_cap = LANE_SPREAD_CAP.get(lane_for_roadguard)

    if spread_bps_raw is None:
        # 2026-02-20 operator directive: do NOT fail closed on missing
        # snapshot for crypto majors. The Webull crypto entitlement
        # sometimes returns price without bid/ask, leaving spread_bps
        # absent. That was killing 100% of crypto intents under the
        # old "fail closed" branch even though Kraken majors actually
        # run <5 bps. Trust a documented fallback for known liquid
        # pairs; still fail closed for unknown / exotic pairs so we
        # can't accidentally trade a chaotic market.
        _CRYPTO_KNOWN_LIQUID = {
            "BTC/USD", "BTCUSD", "BTC-USD",
            "ETH/USD", "ETHUSD", "ETH-USD",
            "SOL/USD", "SOLUSD", "SOL-USD",
            "ADA/USD", "ADAUSD", "ADA-USD",
            "XRP/USD", "XRPUSD", "XRP-USD",
            "DOGE/USD", "DOGEUSD",
            "AVAX/USD", "AVAXUSD",
            "MATIC/USD", "MATICUSD",
            "DOT/USD",  "DOTUSD",
            "LINK/USD", "LINKUSD",
            "LTC/USD",  "LTCUSD",
        }
        intent_sym_upper = str(intent.get("symbol") or "").upper()
        if (lane_for_roadguard == "crypto"
                and intent_sym_upper in _CRYPTO_KNOWN_LIQUID):
            gates.append({
                "name": "roadguard_spread_floor",
                "passed": True,
                "reason": (
                    f"roadguard passed via known-liquid-pair fallback "
                    f"(sym={intent_sym_upper}); snapshot.spread_bps was "
                    f"absent but the pair is on the Kraken majors list "
                    f"(<5 bps typical). Lift this branch by hydrating "
                    f"snapshot.spread_bps in the crypto enricher."
                ),
            })
        else:
            gates.append({
                "name": "roadguard_spread_floor",
                "passed": False,
                "reason": "ROADGUARD_MISSING_SPREAD_BPS — snapshot absent; cannot verify market structure",
            })
    else:
        try:
            spread_bps_val = float(spread_bps_raw)
        except (TypeError, ValueError):
            spread_bps_val = None  # type: ignore[assignment]

        if spread_bps_val is None:
            gates.append({
                "name": "roadguard_spread_floor",
                "passed": False,
                "reason": f"ROADGUARD_BAD_SPREAD_BPS — non-numeric ({spread_bps_raw!r})",
            })
        elif spread_cap is None:
            # Unknown lane → no cap to check; passive pass.
            gates.append({
                "name": "roadguard_spread_floor",
                "passed": True,
                "reason": f"roadguard inactive for lane={lane_for_roadguard!r}",
            })
        else:
            passed = spread_bps_val <= spread_cap
            gates.append({
                "name": "roadguard_spread_floor",
                "passed": passed,
                "reason": (
                    f"spread {spread_bps_val:.2f} bps ≤ {spread_cap:.0f} bps cap "
                    f"(lane={lane_for_roadguard})"
                    if passed else
                    f"ROADGUARD_SPREAD_CAP — spread {spread_bps_val:.2f} bps > "
                    f"{spread_cap:.0f} bps cap (lane={lane_for_roadguard})"
                ),
            })

    # ─── 6.0b R:R floor (2026-05-27, Phase A — equity-only, 3:1) ─────
    # Doctrine: every equity entry intent (BUY / SHORT) must clear a
    # 3:1 reward-to-risk ratio. Phase A is fail-SOFT for intents
    # missing target_price/stop_price (typed warn, pass) so brain
    # teams have a rollout window. The 3:1 ratio enforcement itself
    # is HARD from day one. Crypto + exit verbs skip this gate.
    # Pure-function evaluator lives in `shared/rr_gate.py`.
    from shared.rr_gate import evaluate_rr  # noqa: WPS433
    rr = evaluate_rr(intent)
    rr_gate_reason = rr.reason
    if rr.passed and rr.rr_ratio is not None:
        rr_gate_reason = (
            f"RR_RATIO_OK — reward/risk = {rr.rr_ratio:.2f} "
            f"≥ {rr.rr_min:.1f} floor ({rr.direction})"
        )
    elif rr.passed and rr.phase_a_soft:
        rr_gate_reason = (
            f"{rr.reason} — Phase A soft-pass; brain should ship "
            f"target_price + stop_price + snapshot.price to engage "
            f"the {rr.rr_min:.1f}:1 floor"
        )
    elif not rr.passed and rr.rr_ratio is not None:
        rr_gate_reason = (
            f"RR_RATIO_BELOW_FLOOR — reward/risk = {rr.rr_ratio:.2f} "
            f"< {rr.rr_min:.1f} floor ({rr.direction}); "
            f"target={rr.target_price} stop={rr.stop_price} entry={rr.entry_price}"
        )
    elif not rr.passed and rr.reason == "RR_INVALID_PRICES":
        rr_gate_reason = (
            f"RR_INVALID_PRICES — {rr.direction} intent with incoherent "
            f"target/stop: target={rr.target_price} entry={rr.entry_price} "
            f"stop={rr.stop_price} (reward={rr.reward} risk={rr.risk})"
        )
    gates.append({
        "name": "rr_ratio_floor",
        "passed": rr.passed,
        "reason": rr_gate_reason,
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

    # 6b. Hard exposure caps. Lane-aware: crypto gets the $30/order cap;
    #    equities get the lifted global cap.
    #
    # Doctrine pin (2026-06-10): pass `position_evolution` so
    # `evaluate_open_notional` can correctly distinguish OPEN/ADD
    # (grows exposure) from REDUCE/CLOSE/COVER (shrinks exposure).
    # Before this, a BUY-to-COVER counted as opening — symmetric
    # sign-flip bug. See `shared/exposure_caps.evaluate_open_notional`.
    side = action or ""
    cap_evals = await evaluate_all(
        effective_notional, side,
        lane=intent.get("lane"),
        position_evolution=intent.get("position_evolution"),
    )
    for c in cap_evals:
        gates.append({"name": c.name, "passed": c.passed, "reason": c.reason})

    # Patent suspension (2026-02-17 operator directive). The Patent-stack
    # restrictions cascaded after the crash and locked every brain out of
    # execution. Operator suspended every non-seat gate. The gates still
    # RUN above so the audit trail records what WOULD have failed under
    # doctrine, but their verdicts are force-passed here and tagged
    # `suspended: true`. Seat-layer gates (executor_seat_check,
    # schema_invariants, action_routable, live_trading_disabled) are NEVER
    # forced — they remain authoritative. See `namespaces.SEAT_LAYER_GATES`.
    from namespaces import (  # noqa: WPS433
        PATENT_SUSPENSION_ACTIVE,
        SEAT_LAYER_GATES,
    )
    if PATENT_SUSPENSION_ACTIVE:
        for g in gates:
            if g["name"] in SEAT_LAYER_GATES:
                continue
            if g.get("passed") is False:
                g["suspended"] = True
                g["doctrine_reason"] = g.get("reason")
                g["reason"] = (
                    f"[SUSPENDED — Patent-stack restrictions lifted by "
                    f"operator] {g.get('reason')}"
                )
                g["passed"] = True

    # ─── Operator override (2026-02-19) ─────────────────────────────
    # Lift every soft gate's failure when the operator has explicitly
    # opted in via the submit body. Hard gates (money caps, broker
    # connection, operator's own kill switches, schema doctrine) stay
    # authoritative — see `_HARD_GATES_NEVER_OVERRIDABLE`. The reason
    # gets stamped on the gate row for the audit trail so a future
    # operator can answer "who decided this trade should bypass the
    # spread floor and why" without grepping logs.
    overridden_names: list[str] = []
    if operator_override:
        for g in gates:
            if g.get("passed"):
                continue
            if g["name"] in _HARD_GATES_NEVER_OVERRIDABLE:
                continue
            g["operator_override"] = True
            g["override_reason"] = override_reason or "(no reason provided)"
            g["doctrine_reason"] = g.get("doctrine_reason") or g.get("reason")
            g["reason"] = (
                f"[OVERRIDDEN BY OPERATOR] {g.get('reason')} "
                f"— override_reason={override_reason!r}"
            )
            g["passed"] = True
            overridden_names.append(g["name"])

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
        "operator_override": operator_override,
        "override_reason": override_reason if operator_override else None,
        "overridden_gate_names": overridden_names if operator_override else [],
    }


# ───────────────────────────── dry-run ─────────────────────────────

async def run_dry_run_for_intent(
    intent_id: str,
    order_notional_usd: float = 10.0,
    *,
    actor: str = "auto_dry_run",
) -> dict:
    """Internal dry-run runner — same gate evaluation as the HTTP
    endpoint, callable from background tasks. Returns the result dict.

    Doctrine pin (2026-05-27, auto-dry-run-on-ingest):
        Intents must NEVER sit at `gate_state=pending` indefinitely.
        Before this hook existed, brains emitted intents and nothing
        automatic evaluated them — operators had to manually call
        `/execution/dry_run` for every single one. Result: 100+
        pending intents per brain on prod, 6000+ on preview. This
        runner is fire-and-forget from `shared/intents.py:_ingest`
        so every new intent has a verdict within milliseconds.

    Best-effort: persistence failures are swallowed so the brain's
    POST never blocks on bookkeeping. The intent stays at `pending`
    if anything fails, and a manual re-run will recover.
    """
    intent = await db[SHARED_INTENTS].find_one({"intent_id": intent_id}, {"_id": 0})
    if not intent:
        raise HTTPException(status_code=404, detail=f"intent {intent_id} not found")

    result = await _evaluate_gates(intent, order_notional_usd)
    new_state = "dry_run_passed" if result["verdict"] == "would_pass" else "dry_run_blocked"
    # 2026-02-20: also persist the simpler `dry_run_state` field that
    # `matches_tier_1` and the post-mortem aggregator both read. Before
    # this fix the field was never written, so 100% of intents tripped
    # `auto_submit_skipped/dry_run_not_ready` ("dry_run_state '' !=
    # required 'passed'") and the funnel's dry-run-blocked bucket
    # always read zero. Trace-confirmed on preview at 21:09:56 with
    # intent ebd5418b (chevelle BUY AAPL conf=0.90, gate_state=
    # dry_run_passed, but dry_run_state=None → maybe_auto_submit
    # rejected).
    dry_run_summary = "passed" if result["verdict"] == "would_pass" else "blocked"
    await db[SHARED_INTENTS].update_one(
        {"intent_id": intent_id},
        {"$set": {
            "gate_state": new_state,
            "dry_run_state": dry_run_summary,
            "last_dry_run_ts": _now_iso(),
            "last_dry_run_by": actor,
            "last_dry_run_notional_usd": order_notional_usd,
        }},
    )
    await db[SHARED_GATE_RESULTS].insert_one({
        "intent_id": intent_id,
        "kind": "dry_run",
        "ts": _now_iso(),
        "by": actor,
        "order_notional_usd": order_notional_usd,
        "verdict": result["verdict"],
        "gates": result["gates"],
    })

    # PARADOX audit — append-only emergent-auditor artifact. Best-effort.
    await write_paradox_record(
        intent=intent,
        gates=result["gates"],
        risk_multiplier=result.get("risk_multiplier"),
        evaluation_kind="dry_run",
        evaluated_by=actor,
    )

    return result


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
    """Evaluate the full gate chain WITHOUT placing an order.

    Then — if Shelly's auto-submit policy is on and the intent
    qualifies — call `maybe_auto_submit` to advance it through the
    real submit path (2026-02-19 bug fix: previously this endpoint
    transitioned the intent to `dry_run_passed` and stopped, leaking
    every eligible intent into the "Never submitted (no audit row)"
    bucket).
    """
    result = await run_dry_run_for_intent(
        intent_id, order_notional_usd, actor=user.get("email") or "operator",
    )
    if result.get("verdict") == "would_pass":
        try:
            from shared.auto_submit_policy import maybe_auto_submit  # noqa: WPS433
            await maybe_auto_submit(intent_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("auto_submit chain failed for %s: %s", intent_id, e)
    return {
        "intent_id": intent_id,
        "evaluated_by": user.get("email"),
        "ts": _now_iso(),
        **result,
    }


@router.get("/execution/last-submit-block")
async def execution_last_submit_block(
    intent_id: str = Query(..., description="intent_id to look up"),
    user: dict = Depends(get_current_user),  # noqa: B008
):
    """Return the most recent `submit_blocked` audit row for an intent.

    Why this exists (2026-02-19, P1):
        Some production proxies (Cloudflare, ingress configs with body
        stripping on 4xx) silently drop the response body of an HTTP
        403, leaving the operator staring at a bare "HTTP 403" with
        no idea which gate refused. MC already persists the full gate
        breakdown to `shared_gate_results` (kind="submit_blocked") on
        every block, so the UI can fetch it here as a fallback and
        render the same `blocked_by` / `reason` / `gates` payload that
        the inline 403 body WOULD have carried.

    Returns 404 if no submit_block has been recorded for this intent.
    """
    # 2026-02-19 (rev2 — opaque-403 doom loop fix): include EVERY
    # audit `kind` the submit pipeline writes. Previously this set
    # missed `submit_no_trade` (the broker-router NO_TRADE path:
    # Webull cap evaluator, MC receipt rejection, broker frozen,
    # lane disabled, adapter missing creds), which is the MOST
    # COMMON 403 source on the small-pilot route. When the prod
    # proxy strips the 403 body AND the fallback returns 404, the
    # UI shows a blank red bar — exactly the screenshot the operator
    # filed. With `submit_no_trade` added, the fallback always finds
    # the row the submit handler just wrote.
    _SUBMIT_AUDIT_KINDS = (
        "submit_blocked",     # gate chain rejected
        "submit_no_trade",    # broker_router NO_TRADE (most common)
        "submit_timeout",     # broker did not respond in 20s
        "submit_error",       # broker raised an exception
    )
    row = await db[SHARED_GATE_RESULTS].find_one(
        {"intent_id": intent_id, "kind": {"$in": list(_SUBMIT_AUDIT_KINDS)}},
        {"_id": 0},
        sort=[("ts", -1)],
    )
    if not row:
        raise HTTPException(
            status_code=404,
            detail=f"no submit_block audit row found for intent {intent_id}",
        )
    # Surface a `blocked_by` + `reason` synthesized from the first
    # failing gate, mirroring the inline 403 detail shape. The UI's
    # existing render code (which reads `blocked_by`/`reason`/`gates`
    # off the error object) then "just works" against this fallback.
    #
    # 2026-02-19 (rev2): NO_TRADE/TIMEOUT/ERROR rows don't carry a
    # `gates` array — they're broker-side rejections, not gate-chain
    # blocks. Synthesize a single-row `gates` list so the UI's
    # existing "failingGates" rendering path still surfaces the
    # broker reason inside the red bar (instead of "blocked_by:
    # submit_no_trade" with nothing else).
    kind = row.get("kind") or "unknown"
    gates = row.get("gates") or []
    first_block = next((g for g in gates if not g.get("passed")), None)
    # Broker-side / non-gate-chain rejection — synthesize a virtual
    # gate row so the UI has something readable to render.
    if not gates and kind in ("submit_no_trade", "submit_timeout", "submit_error"):
        synthetic_reason = (
            row.get("reason")
            or row.get("error")
            or {
                "submit_no_trade": "broker_router NO_TRADE (no reason recorded)",
                "submit_timeout": "broker did not respond within 20s",
                "submit_error": "broker raised an exception (no detail recorded)",
            }.get(kind, "broker rejected order")
        )
        gates = [{
            "name": {
                "submit_no_trade": "broker_router",
                "submit_timeout": "broker_submit_timeout",
                "submit_error": "broker_submit_error",
            }.get(kind, kind),
            "passed": False,
            "reason": synthetic_reason,
        }]
        first_block = gates[0]
    return {
        "intent_id": intent_id,
        "kind": kind,
        "ts": row.get("ts"),
        "by": row.get("by"),
        "order_notional_usd": row.get("order_notional_usd"),
        "blocked_by": (
            (first_block or {}).get("name")
            or (kind if kind in _SUBMIT_AUDIT_KINDS else "unknown")
        ),
        "reason": (
            (first_block or {}).get("reason")
            or row.get("reason")
            or row.get("error")
            or "gate chain blocked"
        ),
        "gates": gates,
        "verdict": row.get("verdict"),
        "_from_audit": True,
    }




# ───────────────────── auto-dry-run drain (one-time backfill) ─────────────────────

@router.post("/admin/intents/auto-dry-run-drain")
async def auto_dry_run_drain(
    limit: int = Query(500, ge=1, le=5000, description="max pending intents to process"),
    stack: Optional[str] = Query(None, description="filter to one brain (alpha|camaro|chevelle|redeye)"),
    user: dict = Depends(get_current_user),  # noqa: B008
):
    """One-shot drain of `gate_state=pending` intents through dry-run.

    Doctrine: this is the catch-up sweep for backlog accumulated before
    the auto-dry-run-on-ingest hook was wired. Idempotent: re-running
    after the first pass leaves zero `pending` rows so it's a no-op.

    Each intent gets the same `_evaluate_gates` call as a manual
    dry-run, transitioning to `dry_run_passed` or `dry_run_blocked`
    with full `shared_gate_results` provenance. Per-intent failures
    are swallowed so a single bad row doesn't halt the drain.
    """
    q: dict = {"gate_state": "pending"}
    if stack:
        q["stack"] = stack
    pending = await db[SHARED_INTENTS].find(
        q, {"_id": 0, "intent_id": 1, "stack": 1, "symbol": 1},
    ).sort("ingest_ts", 1).to_list(limit)

    actor = f"auto_drain:{user.get('email','operator')}"
    processed = 0
    passed = 0
    blocked = 0
    auto_submitted = 0
    failures: list[dict] = []

    # 2026-02-19 fix: chain maybe_auto_submit after every would_pass
    # dry-run. The drain previously stopped at `dry_run_passed`, which
    # meant 2965 backlog intents leaked into the post-mortem panel's
    # "Never submitted (no audit row)" bucket. With Shelly enabled,
    # she now picks them up here.
    from shared.auto_submit_policy import maybe_auto_submit  # noqa: WPS433

    for p in pending:
        iid = p["intent_id"]
        try:
            result = await run_dry_run_for_intent(iid, 10.0, actor=actor)
            processed += 1
            if result["verdict"] == "would_pass":
                passed += 1
                # Chain auto-submit — same gate-respecting path the
                # ingest hook uses. maybe_auto_submit is fully
                # idempotent; it writes its own audit row for both
                # skip and success.
                try:
                    sub_result = await maybe_auto_submit(iid)
                    if sub_result is not None:
                        auto_submitted += 1
                except Exception as e:  # noqa: BLE001
                    failures.append({"intent_id": iid, "error": f"auto_submit: {repr(e)[:160]}"})
            else:
                blocked += 1
        except Exception as e:  # noqa: BLE001
            failures.append({"intent_id": iid, "error": repr(e)[:200]})

    return {
        "requested_limit": limit,
        "stack_filter": stack,
        "pending_found": len(pending),
        "processed": processed,
        "would_pass": passed,
        "would_block": blocked,
        "auto_submitted": auto_submitted,
        "failures": failures[:20],
        "failure_count": len(failures),
        "doctrine_note": (
            "Drain runs the same gate chain as a manual dry-run. "
            "Re-run safely; once the backlog is cleared this is a no-op."
        ),
    }



# ───────────────────────────── submit ─────────────────────────────

class SubmitBody(BaseModel):
    intent_id: str = Field(..., min_length=8, max_length=80)
    order_notional_usd: float = Field(default=10.0, ge=0.01, le=10_000.0)
    confirm: str = Field(default="", description="must equal 'execute' to actually route")
    # ─── Operator override (2026-02-19) ───────────────────────────
    # When True, every SOFT gate failure is lifted with the supplied
    # reason stamped on the gate row + receipt. Hard money safety
    # (per-ticker cap, freeze, broker_connected, lane toggle,
    # schema invariants, action_routable) stays authoritative —
    # operator cannot bypass those. Requires a non-empty
    # `override_reason` (min 8 chars) so the audit trail isn't
    # littered with "test" or empty strings.
    operator_override: bool = Field(
        default=False,
        description="if True, soft gates are bypassed; hard caps + freeze stay",
    )
    override_reason: str = Field(
        default="",
        max_length=500,
        description="required when operator_override=True (min 8 chars)",
    )
    # ─── Manual BUY/SELL choice (2026-02-19) ──────────────────────
    # The operator can flip the action at submit time. Original brain
    # action is preserved on the receipt for the audit trail. Only
    # BUY or SELL accepted — operator cannot fabricate HOLD/SHORT/
    # COVER without an underlying intent shape.
    action_override: Optional[str] = Field(
        default=None,
        description="optional BUY/SELL override; receipt stamps original action",
    )


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

    # Operator override sanity — reason must be substantial enough
    # to be useful in an audit trail. 8 chars is a low bar but
    # weeds out "test" / "" / " ".
    if body.operator_override:
        reason_clean = (body.override_reason or "").strip()
        if len(reason_clean) < 8:
            raise HTTPException(
                status_code=400,
                detail=(
                    "operator_override=true requires `override_reason` of at "
                    "least 8 characters describing why every soft gate is "
                    "being bypassed (audit-trail requirement)"
                ),
            )

    # Validate the manual action override.
    action_override = (body.action_override or "").strip().upper() or None
    if action_override and action_override not in ("BUY", "SELL"):
        raise HTTPException(
            status_code=400,
            detail=(
                f"action_override must be BUY or SELL, got {body.action_override!r}"
            ),
        )

    intent = await db[SHARED_INTENTS].find_one({"intent_id": body.intent_id}, {"_id": 0})
    if not intent:
        raise HTTPException(status_code=404, detail=f"intent {body.intent_id} not found")
    if intent.get("executed"):
        raise HTTPException(
            status_code=409,
            detail=f"intent {body.intent_id} already executed at {intent.get('executed_at')}",
        )

    # Apply the action override BEFORE the gate chain runs so every
    # downstream check (action_routable, council, broker_router) sees
    # the operator's chosen side, not the brain's. Mutate a working
    # copy — never the DB row.
    original_action = intent.get("action")
    if action_override and action_override != original_action:
        intent = {**intent, "action": action_override}

    # Safety net: with operator_override=True the `action_routable`
    # gate is overridable, but the broker_router's `side = "BUY" if
    # action in ("BUY","COVER") else "SELL"` silently coerces HOLD/
    # unknown into SELL. Refuse explicitly so a misclick on a HOLD
    # intent can't fire an accidental short.
    effective_action = intent.get("action")
    if effective_action not in ("BUY", "SELL", "SHORT", "COVER"):
        raise HTTPException(
            status_code=400,
            detail=(
                f"intent action is {effective_action!r}; not routable. "
                f"Set `action_override` to BUY or SELL to route this intent."
            ),
        )

    # Re-run the gate chain at submit time — state may have shifted
    # between the dry-run and the click (seat rotated, caps changed,
    # broker disconnected).
    result = await _evaluate_gates(
        intent,
        body.order_notional_usd,
        operator_override=body.operator_override,
        override_reason=body.override_reason.strip() if body.operator_override else "",
    )
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
        # PARADOX audit — record the blocked submit as a kernel REJECTED
        # verdict against the executor's call.
        await write_paradox_record(
            intent=intent,
            gates=result["gates"],
            risk_multiplier=result.get("risk_multiplier"),
            evaluation_kind="submit_blocked",
            evaluated_by=user.get("email"),
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
        # 2026-02-19: 20s ceiling around the broker submit. Webull's
        # SDK calls are already off-loop via `run_in_executor`, but a
        # slow Webull-API round-trip (rate limit, network jitter, IPO
        # day load) can still take 30+ seconds — which is the
        # Cloudflare gateway timeout. Converting that to a clean
        # 504 with `broker_submit_timeout_20s` instead of an HTTP 502
        # gives the operator something readable on the dashboard.
        # 20s is well under the gateway ceiling.
        order = await asyncio.wait_for(
            _route_order(
                intent,
                notional_usd=body.order_notional_usd,
                client_order_id=client_order_id,
            ),
            timeout=20.0,
        )
    except asyncio.TimeoutError:
        await db[SHARED_GATE_RESULTS].insert_one({
            "intent_id": body.intent_id,
            "kind": "submit_timeout",
            "ts": _now_iso(),
            "by": user.get("email"),
            "reason": "broker_submit_timeout_20s",
        })
        record_async(
            event_type="order_rejected",
            brain=intent.get("stack"),
            symbol=intent.get("symbol"),
            action=intent.get("action"),
            outcome="no_trade",
            error_reason="broker_submit_timeout_20s",
            ref_id=body.intent_id,
        )
        raise HTTPException(
            status_code=504,
            detail={
                "blocked_by": "broker_submit_timeout",
                "reason": (
                    "broker did not respond within 20s — order NOT submitted. "
                    "Safe to retry; the order_id was never minted. If this "
                    "persists, check the broker status page or operator logs."
                ),
            },
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
        "mc_receipt": order.get("mc_receipt"),
        "mc_receipt_status": order.get("mc_receipt_status"),
        "mc_receipt_enforced": order.get("mc_receipt_enforced"),
        # ── Operator override audit (2026-02-19) ──
        "operator_override": bool(body.operator_override),
        "override_reason": (
            body.override_reason.strip() if body.operator_override else None
        ),
        "overridden_gate_names": result.get("overridden_gate_names") or [],
        # ── Manual action override audit (2026-02-19) ──
        "action_overridden": bool(action_override and action_override != original_action),
        "original_action": original_action,
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
        "operator_override": bool(body.operator_override),
        "override_reason": (
            body.override_reason.strip() if body.operator_override else None
        ),
        "overridden_gate_names": result.get("overridden_gate_names") or [],
        "action_overridden": bool(action_override and action_override != original_action),
        "original_action": original_action,
    })

    # PARADOX audit — the executor's call passed every gate AND
    # produced a broker receipt. Stamp the artifact with
    # audit_status determined by OPPONENT_MODE.
    await write_paradox_record(
        intent=intent,
        gates=result["gates"],
        risk_multiplier=result.get("risk_multiplier"),
        evaluation_kind="submit_passed",
        evaluated_by=user.get("email"),
    )

    # Live-position lifecycle (2026-02-16) — open a tracked position
    # against this filled receipt. Idempotent on receipt_id; safe if
    # called again. Fire-and-forget would lose the position_id we want
    # to return to the operator, so we await but the call is cheap.
    try:
        from shared.live_positions import open_from_receipt as _open_pos  # noqa: WPS433
        live_pos = await _open_pos(receipt, intent=intent)
    except Exception as e:  # noqa: BLE001
        # Never fail an executed trade on the bookkeeping write.
        print(f"[execution] live_positions.open_from_receipt failed: {e}")
        live_pos = None

    # VRL verification (2026-02-16) — capture slippage/drift evidence
    # immediately. Idempotent on receipt_id. Errors are absorbed; the
    # operator can re-run /api/admin/vrl/verify later if this is skipped.
    try:
        from shared.vrl import verify_receipt as _verify  # noqa: WPS433
        await _verify(receipt, intent=intent)
    except Exception as e:  # noqa: BLE001
        print(f"[execution] vrl.verify_receipt failed: {e}")

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

    # Strip Mongo's mutated `_id` ObjectId from the response — `insert_one`
    # added it in place to `receipt` and ObjectId isn't JSON-serializable.
    response_receipt = {k: v for k, v in receipt.items() if k != "_id"}
    return {
        "ok": True,
        "intent_id": body.intent_id,
        "receipt": response_receipt,
        "order": order,
        "verdict": "executed",
        "live_position": live_pos,
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


@router.get("/config/exposure-caps")
async def exposure_caps_config(_user: dict = Depends(get_current_user)):  # noqa: B008
    """Doctrine surface — single source of truth for exposure caps.
    Pure config, no DB usage. UI, Mission Control, RoadGuard, and future
    clients should all read from this endpoint instead of hardcoding.

    Shape:
        {
          "per_order_usd":        global default per-order cap
          "per_day_usd":          rolling 24h day cap
          "open_notional_usd":    aggregate open-position cap
          "per_order_by_lane_usd": { "<lane>": <cap> }  per-lane overrides
        }

    Effective per-order cap for a given lane:
      per_order_by_lane_usd[lane] if present, else per_order_usd
    """
    return caps_snapshot()




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
    lane: str = Query(default="equity", description="equity or crypto"),
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    """Returns who holds each seat, what they last said, and the
    graduated verdict that would fire for a hypothetical intent at
    `executor_confidence` on the requested `lane`. This makes seat-
    binding and lane-policy visible: switch the Governor seat or the
    lane and re-hit this endpoint to see the verdict flip."""
    policy = _policy_for_lane(lane)
    governor_holder, gov_doc = await _latest_governor_call(symbol, lane=lane)
    _, gov_any = await _latest_governor_any_call(lane=lane)
    opponent_holder, opp_doc = await _latest_opponent_contribution(lane=lane)
    executor_holder = await _seat_holder("executor", lane=lane)
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
        "lane": lane,
    }
    verdict = _governance_verdict(sim_intent, gov_norm, governor_alive, governor_holder, policy)

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
        "lane": lane,
        "policy_used": "crypto" if lane.lower() == "crypto" else "equity",
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
            "input_lane": lane,
            **verdict,
        },
        "active_policy": policy,
        "all_policies": COUNCIL_POLICY,
    }



# ──────────────────── live-trade gate diagnose ────────────────────
# Operator-facing diagnose endpoint. Surfaces ALL blockers preventing
# a live trade on a given lane WITHOUT requiring an actual intent.
# Use when "no trades are being made" to see exactly which gate is
# stopping the order. Also runs broker-adapter sanity (Kraken keys
# decrypt, Alpaca adapter loads, etc.).

@router.get("/admin/execution/diagnose")
async def execution_diagnose(
    lane: str = Query(default="crypto", description="equity or crypto"),
    notional_usd: float = Query(default=25.0, gt=0.0, le=100_000.0),
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    """Run the full gate chain against a synthetic intent for `lane` and
    return every gate's pass/fail plus broker-adapter sanity. The
    response surfaces the FIRST blocker so the operator can act."""
    from shared.broker_router import adapter_for_lane as _adapter_for_lane  # noqa: WPS433
    from shared.crypto.kraken import get_active_keys_status  # noqa: WPS433
    from shared.executor_seat import get_seat_holder, seats_with_execute  # noqa: WPS433

    lane_l = (lane or "crypto").lower()
    if lane_l not in ("equity", "crypto"):
        raise HTTPException(status_code=400, detail=f"lane must be equity|crypto, got {lane!r}")

    # Symbol pick — sample from MC's active `patterns_universe` so
    # the synthetic always uses a symbol that's actually in scope.
    # Falls back to a sensible default if the universe is empty
    # (cold-start). Previously hard-coded to "SPY" / "BTC/USD",
    # which made the diagnose card render "LIVE TRADE: BLOCKED"
    # on equity forever because SPY isn't in the operator's
    # watchlist — confusing the operator into thinking the broker
    # was down. Now: pick the first active universe symbol for the
    # lane so the synthetic reads READY when MC is healthy.
    universe_pick = await db[PATTERNS_UNIVERSE].find_one(
        {"lane": lane_l, "active": {"$ne": False}},
        {"_id": 0, "symbol": 1},
        sort=[("symbol", 1)],
    )
    if universe_pick and universe_pick.get("symbol"):
        sample_symbol = universe_pick["symbol"]
    else:
        sample_symbol = "BTC/USD" if lane_l == "crypto" else "AAPL"

    # Find current executor seat holder for this lane (so the synthetic
    # intent's `stack` matches whoever owns the seat — otherwise the
    # gate would always fail on seat-mismatch and obscure other issues).
    executor_holder = None
    for s in seats_with_execute(lane_l):
        h = await get_seat_holder(s)
        if h:
            executor_holder = h
            break

    # Sample snapshot — gives the probe a realistic spread so gate 7
    # doesn't pre-block the diagnostic on its OWN missing data. Before
    # this (pre-2026-02-18), the synthetic emitted `snapshot=None` and
    # gate 7 fail-closed with ROADGUARD_MISSING_SPREAD_BPS regardless
    # of MC's actual health, making the "LIVE TRADE: BLOCKED" banner
    # a permanent false alarm. The probe now answers the honest
    # question: "if a real brain shipped a clean intent right now,
    # would MC route it?".
    sample_snapshot = (
        {"spread_bps": 12.0, "price": 65000.0, "volume": 50_000_000,
         "market_regime": "strong"}
        if lane_l == "crypto" else
        {"spread_bps": 5.0, "price": 450.0, "volume": 80_000_000,
         "market_regime": "strong"}
    )
    sim_intent = {
        "intent_id": "diagnose-sim",
        "stack": executor_holder or "operator",
        "symbol": sample_symbol,
        "action": "BUY",
        "lane": lane_l,
        "may_execute": False,
        "requires_gate_pass": True,
        "holds_executor_seat": executor_holder is not None,
        "executor_holder_at_post": executor_holder,
        "confidence": 0.7,
        "snapshot": sample_snapshot,
    }
    gate_result = await _evaluate_gates(sim_intent, notional_usd)

    # Broker-adapter sanity.
    broker_status: dict = {"lane": lane_l}
    if lane_l == "crypto":
        kraken_status = await get_active_keys_status()
        broker_status["kraken_credentials"] = {
            k: v for k, v in kraken_status.items()
            if k not in ("public_key", "private_key")  # never leak plaintext
        }
        # Operator-facing remediation hint keyed by failure state.
        REMEDIATION = {
            "ok": "credentials decrypted — if orders still fail, check API key scopes (must include `execute_orders` and `query_funds`) on kraken.com",
            "no_credentials": "POST {public_key, private_key} to /api/admin/kraken/connect to seed the encrypted singleton",
            "missing_field": "singleton exists but a field is empty — re-POST both keys to /api/admin/kraken/connect to overwrite",
            "decrypt_failed": "CREDENTIALS_ENCRYPTION_KEY drifted vs encrypt-time. Re-POST both keys to /api/admin/kraken/connect to re-encrypt under the current key.",
        }
        broker_status["remediation"] = REMEDIATION.get(
            kraken_status.get("state"), "see kraken_credentials.detail",
        )
        adapter = await _adapter_for_lane("crypto")
        broker_status["adapter_loaded"] = adapter is not None
        broker_status["adapter_name"] = getattr(adapter, "name", None)
    else:
        # Equity lane → Public.com (Alpaca was retired 2026-06-XX;
        # references to alpaca_credentials / get_alpaca_adapter here
        # used to make the diagnose UI render "EQUITY · ALPACA · NOT
        # LOADED" forever even though the brain runtime had moved on
        # to Public.com. Now keys off `public_credentials` and uses
        # the same lane-adapter pattern the crypto branch does.)
        adapter = await _adapter_for_lane("equity")
        broker_status["adapter_loaded"] = adapter is not None
        broker_status["adapter_name"] = getattr(adapter, "name", None)
        # Public.com status doc preview (no secrets).
        doc = await db["public_credentials"].find_one(
            {"_id": "singleton"},
            {
                "_id": 0, "execution_enabled": 1, "account_id": 1,
                "secret_preview": 1, "base_url": 1,
                "access_token_expires_at": 1, "updated_at": 1,
            },
        )
        broker_status["public_credentials"] = doc
        broker_status["remediation"] = (
            "POST {secret} to /api/admin/public/connect "
            "if public_credentials is None, then flip "
            "execution_enabled=True with the typed-phrase confirmation."
        ) if not doc or not doc.get("execution_enabled") else (
            "public.com connection live — if orders still fail, "
            "check that the access token hasn't expired (see "
            "access_token_expires_at) and the account has buying power."
        )

    first_block = next((g for g in gate_result["gates"] if not g["passed"]), None)

    return {
        "lane": lane_l,
        "sample_symbol": sample_symbol,
        "synthetic_notional_usd": notional_usd,
        "synthetic_intent": sim_intent,
        "verdict": gate_result["verdict"],
        "first_blocker": first_block,
        "gates": gate_result["gates"],
        "broker": broker_status,
        "caps": gate_result.get("caps"),
        "risk_multiplier": gate_result.get("risk_multiplier"),
        "checked_at": _now_iso(),
    }



# ──────────────────── last-block-reason (operator) ────────────────────
# Surfaces the most recent N intents per brain (or fleet-wide) along
# with the FIRST failing gate row from `shared_gate_results`. Turns
# "no trades are firing" into a 5-second glance — operator sees the
# gate name + reason for each blocked intent without scrolling
# individual receipts.

@router.get("/admin/execution/last-block-reason")
async def last_block_reason(
    stack: Optional[str] = Query(
        default=None,
        description="filter to one brain (alpha|camaro|chevelle|redeye). Omit for fleet-wide.",
    ),
    limit: int = Query(default=20, ge=1, le=100),
    include_hold: bool = Query(
        default=False,
        description="include HOLD intents (watchlist signals). Off by default — HOLDs "
                    "are not trade attempts and clutter the view.",
    ),
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    """Return the latest N blocked intents with the FIRST failing gate.

    Doctrine: this is a *read-only* diagnostic. No state mutation, no
    re-evaluation. It joins `shared_intents` (gate_state in
    {dry_run_blocked, blocked, rejected_at_ingest}) with the latest
    `shared_gate_results` row per intent and surfaces the first
    `passed=False` gate.

    Useful when the operator sees "intents emitted but no trades" —
    one call answers "which gate is killing them, and why".
    """
    q: dict = {"gate_state": {"$in": ["dry_run_blocked", "blocked", "rejected_at_ingest"]}}
    if stack:
        q["stack"] = stack
    if not include_hold:
        q["action"] = {"$in": ["BUY", "SELL", "SHORT", "COVER"]}

    intents = await db[SHARED_INTENTS].find(
        q,
        {
            "_id": 0,
            "intent_id": 1,
            "stack": 1,
            "symbol": 1,
            "action": 1,
            "lane": 1,
            "gate_state": 1,
            "ingest_ts": 1,
            "last_dry_run_ts": 1,
        },
    ).sort("ingest_ts", -1).to_list(limit)

    out: list[dict] = []
    gate_counter: dict[str, int] = {}
    for it in intents:
        gr = await db[SHARED_GATE_RESULTS].find_one(
            {"intent_id": it["intent_id"]},
            {"_id": 0, "gates": 1, "ts": 1, "kind": 1},
            sort=[("ts", -1)],
        )
        first_fail = None
        if gr:
            first_fail = next(
                (g for g in (gr.get("gates") or []) if not g.get("passed")),
                None,
            )
        if it.get("gate_state") == "rejected_at_ingest" and not first_fail:
            # Ingest-time rejection — no gate row, surface a synthetic one.
            first_fail = {
                "name": "ingest_rejection",
                "passed": False,
                "reason": "intent rejected at ingest (schema / lane / sovereign mode)",
            }
        row = {
            "intent_id": it["intent_id"],
            "stack": it.get("stack"),
            "symbol": it.get("symbol"),
            "action": it.get("action"),
            "lane": it.get("lane"),
            "gate_state": it.get("gate_state"),
            "ingest_ts": it.get("ingest_ts"),
            "last_evaluated_ts": (gr or {}).get("ts") or it.get("last_dry_run_ts"),
            "evaluation_kind": (gr or {}).get("kind"),
            "first_failing_gate": (first_fail or {}).get("name"),
            "reason": (first_fail or {}).get("reason"),
        }
        out.append(row)
        gname = row["first_failing_gate"] or "unknown"
        gate_counter[gname] = gate_counter.get(gname, 0) + 1

    # Summary by failing-gate, sorted descending.
    summary = sorted(
        [{"gate": k, "n": v} for k, v in gate_counter.items()],
        key=lambda r: -r["n"],
    )

    return {
        "stack_filter": stack,
        "include_hold": include_hold,
        "requested_limit": limit,
        "returned": len(out),
        "checked_at": _now_iso(),
        "summary_by_failing_gate": summary,
        "items": out,
    }

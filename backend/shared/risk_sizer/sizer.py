"""Canonical stop resolution + risk-based position sizing.

ONE canonical stop per trade: a valid brain-authored stop (bounded
1%-5%) beats the lane exit-policy SL%; whichever wins is used for
sizing, persisted on the intent, and enforced by the Exit Monitor.
The Exit Monitor never silently replaces it afterward.
"""
from __future__ import annotations

import logging
import math
from typing import Any, Optional

logger = logging.getLogger("risedual.risk_sizer")


async def resolve_canonical_stop(intent: dict, lane_policy: dict) -> dict:
    """Returns {stop_fraction, stop_price|None, target_price|None,
    source: BRAIN|EXIT_POLICY, entry_price|None, rejected_brain_stop}."""
    entry = None
    for key in ("price_at_signal", "price", "entry_price"):
        v = intent.get(key) or (intent.get("evidence") or {}).get(key)
        try:
            if v and float(v) > 0:
                entry = float(v)
                break
        except (TypeError, ValueError):
            continue

    lo = float(lane_policy.get("min_stop_fraction", 0.01))
    hi = float(lane_policy.get("max_stop_fraction", 0.05))
    action = (intent.get("action") or "").upper()

    rejected = None
    stop_raw = intent.get("stop_price")
    if stop_raw and entry:
        try:
            stop = float(stop_raw)
            frac = abs(entry - stop) / entry
            side_ok = stop < entry if action == "BUY" else stop > entry
            if side_ok and lo <= frac <= hi:
                return {
                    "stop_fraction": frac,
                    "stop_price": stop,
                    "target_price": intent.get("target_price"),
                    "source": "BRAIN",
                    "entry_price": entry,
                    "rejected_brain_stop": None,
                }
            rejected = (
                f"brain stop {stop} rejected: frac={frac:.4f} "
                f"bounds=[{lo},{hi}] side_ok={side_ok}"
            )
        except (TypeError, ValueError) as exc:  # noqa: BLE001
            rejected = f"brain stop unparseable: {exc}"

    # Fallback: live exit-policy SL% for the lane — the SAME value the
    # Exit Monitor applies when adopting the position.
    from shared.exits.policy import get_policy  # noqa: WPS433
    pol = await get_policy()
    lane = (intent.get("lane") or "crypto").lower()
    sl_pct = float((pol.get(lane) or {}).get("sl_pct") or 3.0)
    frac = sl_pct / 100.0
    stop_price = None
    if entry:
        stop_price = entry * (1 - frac) if action == "BUY" else entry * (1 + frac)
    return {
        "stop_fraction": frac,
        "stop_price": stop_price,
        "target_price": None,
        "source": "EXIT_POLICY",
        "entry_price": entry,
        "rejected_brain_stop": rejected,
    }


async def build_position_plan(
    intent: dict,
    *,
    governor_multiplier: float,
    skip_roadguard: bool = False,
) -> dict:
    """Full sizing decision. `approved=False` plans carry the exact
    rejection reason. Approved plans reserve pending risk atomically
    (released/confirmed by the router after the broker responds)."""
    from shared.risk_sizer import balance, open_risk  # noqa: WPS433
    from shared.risk_sizer.policy import get_sizer_policy  # noqa: WPS433

    lane = (intent.get("lane") or "crypto").lower()
    intent_id = intent.get("intent_id") or ""
    policy = await get_sizer_policy()
    lane_pol = policy[lane]
    bal_pol = policy["balance"]

    def _reject(reason: str, **extra: Any) -> dict:
        return {"approved": False, "reason": reason, "lane": lane,
                "final_notional": 0.0, **extra}

    gm = max(0.0, min(1.0, float(governor_multiplier)))
    if gm <= 0:
        return _reject("governor_multiplier_zero")

    # RoadGuard hard block — same authority the router's master-switch
    # gate enforces, consulted here so a sizer-level plan can NEVER be
    # approved while trading is frozen (final notional forced to 0).
    # `skip_roadguard` exists ONLY for read-only admin previews.
    try:
        from shared.hotpath import policy_snapshot  # noqa: WPS433
        _ps = policy_snapshot.get()
        if not skip_roadguard and (
            not _ps.get("master_switch_enabled", True)
            or _ps.get("broker_freeze_reason")
        ):
            return _reject(
                "roadguard_hard_block",
                roadguard_reason=_ps.get("broker_freeze_reason") or "master_switch_off",
            )
    except Exception:  # noqa: BLE001
        pass  # router master-switch gate still enforces upstream

    # Edge-vs-cost gate: expected edge must EXCEED estimated fees +
    # slippage or the trade is rejected. No edge data → gate skipped.
    edge = intent.get("expected_edge_fraction")
    if edge is None:
        edge = (intent.get("evidence") or {}).get("expected_edge_fraction")
    if edge is not None:
        try:
            edge_f = float(edge)
            fee_f = float(
                intent.get("estimated_fee_fraction")
                if intent.get("estimated_fee_fraction") is not None
                else lane_pol["fee_buffer_fraction"],
            )
            slip_f = float(
                intent.get("estimated_slippage_fraction")
                if intent.get("estimated_slippage_fraction") is not None
                else lane_pol.get("slippage_buffer_fraction") or 0.0,
            )
            if edge_f <= fee_f + slip_f:
                return _reject("edge_does_not_cover_costs",
                               expected_edge_fraction=edge_f,
                               estimated_cost_fraction=round(fee_f + slip_f, 6))
        except (TypeError, ValueError):
            pass

    # Options contract-quality gates run BEFORE the balance fetch —
    # a bad contract never costs a broker round-trip.
    _opt_meta = None
    if lane == "options":
        from shared.risk_sizer import options_gate  # noqa: WPS433
        _chk = options_gate.check(intent, lane_pol)
        if not _chk["ok"]:
            return _reject(_chk["reason"], **(_chk.get("detail") or {}))
        _opt_meta = _chk["meta"]

    snap = await balance.get_balance_snapshot(
        lane,
        timeout_s=float(bal_pol["live_timeout_s"]),
        cache_max_age_s=float(bal_pol["cache_max_age_s"]),
    )
    if snap is None:
        return _reject("no_balance_no_trade")
    equity = float(snap["equity"])
    available = float(snap["available"])
    if equity <= 0 or available <= 0:
        return _reject("insufficient_balance", balance_source=snap["source"])

    base_risk = equity * float(lane_pol["risk_fraction"])
    adjusted_risk = base_risk * gm

    open_r = open_risk.total_open_risk(lane)
    remaining_capacity = max(
        0.0, equity * float(lane_pol["max_open_risk_fraction"]) - open_r,
    )
    final_risk = min(adjusted_risk, remaining_capacity)
    if final_risk <= 0:
        return _reject("portfolio_risk_budget_exhausted",
                       portfolio_open_risk=round(open_r, 2),
                       balance_source=snap["source"])

    if lane == "options":
        from shared.risk_sizer import options_gate  # noqa: WPS433
        return options_gate.build_options_receipt(
            intent, lane_pol, meta=_opt_meta, equity=equity,
            available=available, final_risk=final_risk, gm=gm,
            snap=snap, open_r=open_r, reject=_reject,
        )

    stop = await resolve_canonical_stop(intent, lane_pol)
    stop_frac = float(stop["stop_fraction"])

    risk_based = final_risk / stop_frac
    allocation_cap = equity * float(lane_pol["max_position_fraction"])
    spendable = available * (1.0 - float(lane_pol["reserve_fraction"]))
    final_notional = math.floor(min(risk_based, allocation_cap, spendable) * 100) / 100.0

    if final_notional < float(lane_pol["minimum_order_notional"]):
        return _reject("below_minimum_order_notional",
                       computed_notional=final_notional,
                       balance_source=snap["source"])

    # Re-derive the risk actually carried at the final notional — the
    # caps can only shrink it, never grow it.
    carried_risk = final_notional * stop_frac

    # FINAL SAFETY INVARIANT (operator acceptance test): recomputed
    # projected loss at the persisted stop must never exceed the
    # approved risk budget — rounding/conversion included.
    projected_loss = carried_risk
    if stop["entry_price"] and stop["stop_price"]:
        qty = final_notional / float(stop["entry_price"])
        if (intent.get("action") or "").upper() == "SHORT":
            projected_loss = max(0.0, float(stop["stop_price"]) - float(stop["entry_price"])) * qty
        else:
            projected_loss = max(0.0, float(stop["entry_price"]) - float(stop["stop_price"])) * qty
    if projected_loss > final_risk + 0.01:
        return _reject("projected_loss_exceeds_budget",
                       projected_loss_at_stop=round(projected_loss, 4),
                       risk_budget_max=round(final_risk, 2))

    import uuid as _uuid
    receipt = {
        "approved": True,
        "reason": "approved",
        "plan_id": _uuid.uuid4().hex,
        "lane": lane,
        "balance_source": snap["source"],
        "balance_age_ms": snap["age_ms"],
        "account_equity": round(equity, 2),
        "available_quote_balance": round(available, 2),
        "reserved_notional": final_notional,
        "risk_budget": round(carried_risk, 2),
        "risk_budget_max": round(final_risk, 2),
        "stop_source": stop["source"],
        "stop_distance": round(stop_frac, 6),
        "stop_price": stop["stop_price"],
        "target_price": stop["target_price"],
        "entry_price": stop["entry_price"],
        "rejected_brain_stop": stop["rejected_brain_stop"],
        "allocation_cap": round(allocation_cap, 2),
        "spendable_cash": round(spendable, 2),
        "portfolio_open_risk": round(open_r, 2),
        "governor_multiplier": gm,
        "gross_notional": round(risk_based, 2),
        "final_notional": final_notional,
        "projected_loss_at_stop": round(projected_loss, 4),
    }
    if intent_id:
        open_risk.reserve_pending(intent_id, lane, carried_risk, final_notional)
    logger.info(
        "risk_sizer %s %s: eq=$%.2f(%s) risk=$%.2f stop=%.2f%%(%s) → $%.2f",
        lane, intent_id, equity, snap["source"], carried_risk,
        stop_frac * 100, stop["source"], final_notional,
    )
    return receipt

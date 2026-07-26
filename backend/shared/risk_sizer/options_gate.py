"""Options-lane validation + premium-based sizing.

Options ride the SAME shared risk engine (live balance, risk budget,
portfolio cap, Governor reduce-only, RoadGuard, atomic reservation) —
this module only adds what options need on top: contract-quality gates
(DTE, open interest, spread, Delta, Theta) and sizing based on premium
paid rather than underlying price. Long options: max loss = premium ×
premium_stop_fraction; projected loss ≤ risk budget holds by
construction (whole contracts only).
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("risedual.risk_sizer.options")


def _num(container: dict, intent: dict, key: str) -> Optional[float]:
    v = container.get(key, intent.get(key))
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def check(intent: dict, pol: dict) -> dict:
    """Contract-quality gates. Returns {ok: True, meta} or
    {ok: False, reason, detail}."""
    o = intent.get("option") or {}

    premium = _num(o, intent, "premium")
    if not premium or premium <= 0:
        return {"ok": False, "reason": "options_missing_premium"}

    dte = _num(o, intent, "dte")
    if dte is None:
        exp = o.get("expiration") or intent.get("expiration")
        if exp:
            try:
                d = datetime.fromisoformat(str(exp).replace("Z", "+00:00"))
                if d.tzinfo is None:
                    d = d.replace(tzinfo=timezone.utc)
                dte = (d - datetime.now(timezone.utc)).total_seconds() / 86400.0
            except ValueError:
                dte = None
    if dte is None:
        return {"ok": False, "reason": "options_missing_expiration"}
    if dte < float(pol["min_dte"]) or dte > float(pol["max_dte"]):
        return {"ok": False, "reason": "options_dte_out_of_bounds",
                "detail": {"dte": round(dte, 1),
                           "bounds": [pol["min_dte"], pol["max_dte"]]}}

    oi = _num(o, intent, "open_interest")
    if oi is None or oi < float(pol["min_open_interest"]):
        return {"ok": False, "reason": "options_open_interest_too_low",
                "detail": {"open_interest": oi,
                           "minimum": pol["min_open_interest"]}}

    bid, ask = _num(o, intent, "bid"), _num(o, intent, "ask")
    if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
        return {"ok": False, "reason": "options_missing_quote"}
    mid = (bid + ask) / 2.0
    spread_frac = (ask - bid) / mid if mid > 0 else 1.0
    if spread_frac > float(pol["max_spread_fraction"]):
        return {"ok": False, "reason": "options_spread_too_wide",
                "detail": {"spread_fraction": round(spread_frac, 4),
                           "maximum": pol["max_spread_fraction"]}}

    delta = _num(o, intent, "delta")
    if delta is None:
        return {"ok": False, "reason": "options_missing_delta"}
    if not (float(pol["min_abs_delta"]) <= abs(delta) <= float(pol["max_abs_delta"])):
        return {"ok": False, "reason": "options_delta_out_of_bounds",
                "detail": {"delta": delta,
                           "bounds": [pol["min_abs_delta"], pol["max_abs_delta"]]}}

    theta = _num(o, intent, "theta")
    if theta is None:
        return {"ok": False, "reason": "options_missing_theta"}
    theta_frac = abs(theta) / premium
    if theta_frac > float(pol["max_theta_fraction_per_day"]):
        return {"ok": False, "reason": "options_theta_decay_too_high",
                "detail": {"theta_fraction_per_day": round(theta_frac, 4),
                           "maximum": pol["max_theta_fraction_per_day"]}}

    return {"ok": True, "meta": {
        "premium": premium, "dte": round(dte, 2), "open_interest": oi,
        "bid": bid, "ask": ask, "mid": round(mid, 4),
        "spread_fraction": round(spread_frac, 4),
        "delta": delta, "theta": theta,
    }}


def build_options_receipt(
    intent: dict, pol: dict, *, meta: dict, equity: float, available: float,
    final_risk: float, gm: float, snap: dict, open_r: float, reject: Any,
) -> dict:
    """Premium-based contract sizing inside the approved risk budget."""
    from shared.risk_sizer import open_risk as _open_risk  # noqa: WPS433

    lane = "options"
    intent_id = intent.get("intent_id") or ""
    mult = float(pol["contract_multiplier"])
    stop_frac = max(0.0, min(1.0, float(pol["premium_stop_fraction"]))) or 1.0

    per_contract_cost = meta["premium"] * mult
    max_loss_per_contract = per_contract_cost * stop_frac

    premium_cap = equity * float(pol["max_premium_fraction"])
    allocation_cap = equity * float(pol["max_position_fraction"])
    spendable = available * (1.0 - float(pol["reserve_fraction"]))
    cost_cap = min(premium_cap, allocation_cap, spendable)

    contracts = int(min(final_risk / max_loss_per_contract,
                        cost_cap / per_contract_cost))
    if contracts < 1:
        return reject("options_below_minimum_contracts",
                      per_contract_cost=round(per_contract_cost, 2),
                      max_loss_per_contract=round(max_loss_per_contract, 2),
                      risk_budget_max=round(final_risk, 2),
                      cost_cap=round(cost_cap, 2))

    final_notional = round(contracts * per_contract_cost, 2)
    carried_risk = contracts * max_loss_per_contract
    projected_loss = carried_risk
    if projected_loss > final_risk + 0.01:
        return reject("projected_loss_exceeds_budget",
                      projected_loss_at_stop=round(projected_loss, 4),
                      risk_budget_max=round(final_risk, 2))

    receipt = {
        "approved": True,
        "reason": "approved",
        "plan_id": uuid.uuid4().hex,
        "lane": lane,
        "balance_source": snap["source"],
        "balance_age_ms": snap["age_ms"],
        "account_equity": round(equity, 2),
        "available_quote_balance": round(available, 2),
        "reserved_notional": final_notional,
        "risk_budget": round(carried_risk, 2),
        "risk_budget_max": round(final_risk, 2),
        "stop_source": "PREMIUM",
        "stop_distance": stop_frac,
        "stop_price": None,
        "target_price": None,
        "entry_price": meta["premium"],
        "rejected_brain_stop": None,
        "contracts": contracts,
        "option": meta,
        "per_contract_cost": round(per_contract_cost, 2),
        "max_premium_cap": round(premium_cap, 2),
        "allocation_cap": round(allocation_cap, 2),
        "spendable_cash": round(spendable, 2),
        "portfolio_open_risk": round(open_r, 2),
        "governor_multiplier": gm,
        "gross_notional": final_notional,
        "final_notional": final_notional,
        "projected_loss_at_stop": round(projected_loss, 4),
    }
    if intent_id:
        _open_risk.reserve_pending(intent_id, lane, carried_risk, final_notional)
    logger.info(
        "risk_sizer options %s: eq=$%.2f(%s) risk=$%.2f prem=$%.2f×%d → $%.2f",
        intent_id, equity, snap["source"], carried_risk,
        meta["premium"], contracts, final_notional,
    )
    return receipt

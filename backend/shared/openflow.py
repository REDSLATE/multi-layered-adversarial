"""OpenFlow Core — operator-pinned 2026-06-29.

Doctrine (operator spec):
    Only the Seat can say TRADE.
    Conductor can resize.
    Tripwire can emergency-halt.
    Nothing else can veto.

Pipeline:
    Brain advisory intent
            ↓
    Seat authority check         (existing, unchanged)
            ↓
    OpenFlow Conductor           (sizing)
            ↓
    Budget                       (capital + daily-risk accounting)
            ↓
    Tripwire                     (catastrophe-only halt)
            ↓
    Broker live submit           (existing adapter, unchanged)

Status: env-gated, default OFF.
    `EQUITY_USE_OPENFLOW=true` routes auto-submitted equity intents
    through this module instead of the 16-gate chain. Manual
    submits still hit `execution_submit` until that fork ships
    separately. Crypto always uses the legacy path.

Cash-account doctrine (preserved from PRD):
    The original P0 rule "100% block rate for any non-cash trades"
    is enforced by Tripwire — a non-cash account state HALTs the
    pipeline. Not a soft check, not a Conductor reduction.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from db import db
from namespaces import SHARED_GATE_RESULTS

logger = logging.getLogger("openflow")

OPENFLOW_CONFIG = "openflow_config"
OPENFLOW_BUDGET_STATE = "openflow_budget_state"
OPENFLOW_TRIPWIRE_AUDIT = "openflow_tripwire_audit"


# ── Operator doctrine constants ─────────────────────────────────────


# Default budget — operator's first-live numbers.
DEFAULT_BUDGET = {
    "initial_capital": 500.0,
    "max_daily_risk": 25.0,
    "notional_default_usd": 5.0,
    "notional_max_usd": 25.0,
}


def is_equity_openflow_enabled() -> bool:
    """Env switch. Default OFF for safety. Operator flips per pod
    by setting `EQUITY_USE_OPENFLOW=true` (or via runtime flag —
    see `_runtime_flag_enabled` below)."""
    raw = os.environ.get("EQUITY_USE_OPENFLOW", "")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


async def _runtime_flag_enabled() -> bool:
    """Mongo-backed flag so flipping doesn't require a redeploy.
    Reads `runtime_flags._id='equity_use_openflow'.enabled`."""
    try:
        doc = await db["runtime_flags"].find_one(
            {"_id": "equity_use_openflow"}, {"_id": 0, "enabled": 1},
        )
        return bool((doc or {}).get("enabled", False))
    except Exception:  # noqa: BLE001
        return False


async def equity_openflow_active() -> bool:
    """True if either the env or Mongo flag says ON."""
    return is_equity_openflow_enabled() or await _runtime_flag_enabled()


# ── Dataclasses ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class ConductorVerdict:
    """Conductor never blocks. Returns the resolved notional and
    any reasons it had to clamp from intent's request."""
    notional_usd: float
    requested_usd: float
    clamped_to_max: bool
    used_default: bool
    reasons: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class TripwireVerdict:
    """Tripwire only HALTs on catastrophe. Returns PASS otherwise."""
    halted: bool
    reason: Optional[str]
    severity: str  # 'PASS' | 'CRITICAL' | 'FATAL'
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OpenFlowVerdict:
    """Final go/no-go for the broker submit step."""
    allow: bool
    notional_usd: float
    conductor: ConductorVerdict
    tripwire: TripwireVerdict
    seat_holder: Optional[str]


# ── Budget ──────────────────────────────────────────────────────────


async def get_budget_config(lane: str = "equity") -> dict[str, float]:
    """Read the persisted budget config for a lane. Operator-set
    via the admin endpoint. Falls back to defaults if no row."""
    doc = await db[OPENFLOW_CONFIG].find_one(
        {"_id": lane}, {"_id": 0},
    )
    if not doc:
        return dict(DEFAULT_BUDGET)
    return {
        "initial_capital": float(doc.get("initial_capital", DEFAULT_BUDGET["initial_capital"])),
        "max_daily_risk": float(doc.get("max_daily_risk", DEFAULT_BUDGET["max_daily_risk"])),
        "notional_default_usd": float(doc.get("notional_default_usd", DEFAULT_BUDGET["notional_default_usd"])),
        "notional_max_usd": float(doc.get("notional_max_usd", DEFAULT_BUDGET["notional_max_usd"])),
    }


async def set_budget_config(lane: str, cfg: dict, actor: str) -> dict:
    """Operator-facing: persist the lane's budget. Validation:
    - all numeric, all > 0
    - max_daily_risk ≤ initial_capital (can't risk more than the bank)
    - notional_default ≤ notional_max"""
    initial_capital = float(cfg.get("initial_capital", DEFAULT_BUDGET["initial_capital"]))
    max_daily_risk = float(cfg.get("max_daily_risk", DEFAULT_BUDGET["max_daily_risk"]))
    notional_default = float(cfg.get("notional_default_usd", DEFAULT_BUDGET["notional_default_usd"]))
    notional_max = float(cfg.get("notional_max_usd", DEFAULT_BUDGET["notional_max_usd"]))

    if initial_capital <= 0 or max_daily_risk <= 0 or notional_default <= 0 or notional_max <= 0:
        raise ValueError("budget values must all be positive")
    if max_daily_risk > initial_capital:
        raise ValueError(
            f"max_daily_risk ({max_daily_risk}) cannot exceed "
            f"initial_capital ({initial_capital})"
        )
    if notional_default > notional_max:
        raise ValueError(
            f"notional_default ({notional_default}) cannot exceed "
            f"notional_max ({notional_max})"
        )

    doc = {
        "_id": lane,
        "initial_capital": initial_capital,
        "max_daily_risk": max_daily_risk,
        "notional_default_usd": notional_default,
        "notional_max_usd": notional_max,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "updated_by": actor,
    }
    await db[OPENFLOW_CONFIG].update_one(
        {"_id": lane}, {"$set": doc}, upsert=True,
    )
    return doc


async def get_realized_daily_loss_usd(lane: str = "equity") -> float:
    """Today's realized P&L (loss is positive number). Reads from
    the existing realized-pnl audit collection. Returns 0 if no rows."""
    today = datetime.now(timezone.utc).date().isoformat()
    try:
        pipeline = [
            {"$match": {"lane": lane, "realized_at_date": today}},
            {"$group": {"_id": None, "total_pnl": {"$sum": "$realized_pnl_usd"}}},
        ]
        rows = await db["shared_realized_pnl"].aggregate(pipeline).to_list(1)
        if rows:
            pnl = float(rows[0].get("total_pnl") or 0.0)
            return max(0.0, -pnl)  # only count losses as "risk consumed"
    except Exception:  # noqa: BLE001
        pass
    return 0.0


# ── Conductor ───────────────────────────────────────────────────────


def conductor_size(
    requested_usd: Optional[float],
    notional_default_usd: float,
    notional_max_usd: float,
) -> ConductorVerdict:
    """Resize logic. Never blocks — always returns a positive
    notional bounded by the configured max. Operator pin:
    'Conductor never blocks. Budget never starves.'

    If intent didn't request a size, use the default.
    If intent requested > max, clamp to max (with a reason).
    If intent requested ≤ 0, use the default (treat as missing).
    """
    reasons: list[str] = []
    used_default = False
    clamped = False

    if not requested_usd or requested_usd <= 0:
        notional = notional_default_usd
        used_default = True
        reasons.append("used_default_notional")
    elif requested_usd > notional_max_usd:
        notional = notional_max_usd
        clamped = True
        reasons.append(f"clamped_{requested_usd:.0f}_to_{notional_max_usd:.0f}")
    else:
        notional = float(requested_usd)

    return ConductorVerdict(
        notional_usd=notional,
        requested_usd=float(requested_usd or 0.0),
        clamped_to_max=clamped,
        used_default=used_default,
        reasons=reasons,
    )


# ── Tripwire ────────────────────────────────────────────────────────


def evaluate_tripwire(
    intent_action: str,
    proposed_notional_usd: float,
    budget_cfg: dict,
    realized_loss_today_usd: float,
    account_is_cash: bool,
    kill_engine_halt: bool = False,
) -> TripwireVerdict:
    """Catastrophe-only halts. Each condition is named, each carries
    evidence for the audit row.

    Order of precedence (most catastrophic first):
      1. Kill-engine halt — operator-triggered global halt
      2. Cash-account violation — original P0 doctrine ("100% block")
      3. Daily loss limit reached — can't take more risk today
      4. Action sanity — must be BUY or SELL (no HOLD/SHORT to broker)
    """
    if kill_engine_halt:
        return TripwireVerdict(
            halted=True,
            reason="KILL_ENGINE_HALT",
            severity="FATAL",
            evidence={},
        )

    # PRD P0 doctrine — non-cash account is a catastrophe by design.
    if not account_is_cash:
        return TripwireVerdict(
            halted=True,
            reason="CASH_ACCOUNT_VIOLATION",
            severity="FATAL",
            evidence={"account_is_cash": account_is_cash},
        )

    max_daily_risk = float(budget_cfg.get("max_daily_risk", 0.0))
    if max_daily_risk > 0 and realized_loss_today_usd >= max_daily_risk:
        return TripwireVerdict(
            halted=True,
            reason="DAILY_LOSS_LIMIT_REACHED",
            severity="CRITICAL",
            evidence={
                "realized_loss_today_usd": round(realized_loss_today_usd, 2),
                "max_daily_risk_usd": max_daily_risk,
            },
        )

    # Adding this single trade would exceed daily risk if it went
    # to its full proposed notional? Halt.
    if max_daily_risk > 0 and (realized_loss_today_usd + proposed_notional_usd) > max_daily_risk:
        return TripwireVerdict(
            halted=True,
            reason="DAILY_RISK_WOULD_EXCEED",
            severity="CRITICAL",
            evidence={
                "realized_loss_today_usd": round(realized_loss_today_usd, 2),
                "proposed_notional_usd": round(proposed_notional_usd, 2),
                "max_daily_risk_usd": max_daily_risk,
            },
        )

    if intent_action not in ("BUY", "SELL"):
        return TripwireVerdict(
            halted=True,
            reason=f"NON_ROUTABLE_ACTION:{intent_action}",
            severity="CRITICAL",
            evidence={"action": intent_action},
        )

    return TripwireVerdict(halted=False, reason=None, severity="PASS")


# ── Dispatch ────────────────────────────────────────────────────────


async def openflow_dispatch(
    intent: dict,
    seat_holder: Optional[str],
    account_is_cash: bool,
    kill_engine_halt: bool = False,
    actor: str = "auto_submit_openflow",
) -> OpenFlowVerdict:
    """Run the OpenFlow pipeline: Conductor → Budget → Tripwire.

    Returns OpenFlowVerdict — caller submits to broker only when
    `verdict.allow` is True. The caller is responsible for the
    actual broker call; this function makes the decision and writes
    one audit row per call (kind='openflow_dispatched').

    Doctrine pin: this function does NOT call the broker. It does
    NOT bypass seat-holder authority — the caller MUST have already
    verified `seat_holder` is present and matches the intent's
    brain. Tripwire is the only thing here that can refuse.
    """
    lane = (intent.get("lane") or "equity").lower()
    cfg = await get_budget_config(lane=lane)

    conductor = conductor_size(
        requested_usd=intent.get("notional_default_usd")
        or intent.get("notional_usd"),
        notional_default_usd=cfg["notional_default_usd"],
        notional_max_usd=cfg["notional_max_usd"],
    )

    realized_loss = await get_realized_daily_loss_usd(lane=lane)

    tripwire = evaluate_tripwire(
        intent_action=str(intent.get("action") or "").upper(),
        proposed_notional_usd=conductor.notional_usd,
        budget_cfg=cfg,
        realized_loss_today_usd=realized_loss,
        account_is_cash=account_is_cash,
        kill_engine_halt=kill_engine_halt,
    )

    allow = not tripwire.halted and seat_holder is not None

    # Audit row — every openflow_dispatch produces exactly one
    # `openflow_dispatched` audit doc, so the post-mortem panel
    # and Trade Flow page can read the OpenFlow path uniformly.
    try:
        await db[SHARED_GATE_RESULTS].insert_one({
            "intent_id": intent.get("intent_id"),
            "kind": "openflow_dispatched",
            "ts": datetime.now(timezone.utc).isoformat(),
            "by": actor,
            "phase": "openflow",
            "lane": lane,
            "allow": allow,
            "notional_usd": conductor.notional_usd,
            "seat_holder": seat_holder,
            "account_is_cash": account_is_cash,
            "conductor": {
                "requested_usd": conductor.requested_usd,
                "clamped_to_max": conductor.clamped_to_max,
                "used_default": conductor.used_default,
                "reasons": conductor.reasons,
            },
            "tripwire": {
                "halted": tripwire.halted,
                "reason": tripwire.reason,
                "severity": tripwire.severity,
                "evidence": tripwire.evidence,
            },
            "budget_cfg": cfg,
            "realized_loss_today_usd": realized_loss,
        })
    except Exception as audit_err:  # noqa: BLE001
        logger.error("openflow audit write failed: %s", audit_err)

    return OpenFlowVerdict(
        allow=allow,
        notional_usd=conductor.notional_usd,
        conductor=conductor,
        tripwire=tripwire,
        seat_holder=seat_holder,
    )


__all__ = [
    "DEFAULT_BUDGET",
    "OPENFLOW_CONFIG",
    "OPENFLOW_BUDGET_STATE",
    "ConductorVerdict",
    "TripwireVerdict",
    "OpenFlowVerdict",
    "is_equity_openflow_enabled",
    "equity_openflow_active",
    "get_budget_config",
    "set_budget_config",
    "get_realized_daily_loss_usd",
    "conductor_size",
    "evaluate_tripwire",
    "openflow_dispatch",
]

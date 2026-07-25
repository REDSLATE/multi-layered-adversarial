"""Risk — the ONE module that enforces hard limits.

Doctrine (2026-02-27 architectural reduction):

    Market Data → Brain → Seat → RISK → Broker

Risk is the single non-negotiable gate between Seat and Broker. It
is the merger of every "money-safety" check that previously sprawled
across the codebase:
    * shared/broker_freeze.py            (kill switch)
    * shared/broker/webull_caps.py       (per-order cap evaluator)
    * shared/in_flight_orders.py         (idempotency)
    * shared/brain_lane_policy.py        (lane on/off)
    * shared/exposure_caps.py            (daily exposure cap)
    * shared/crypto/exposure_caps.py     (same, crypto)

Risk says NO when:
    1. Master Trading Switch is OFF (operator freeze)
    2. Per-order USD cap is exceeded
    3. Daily exposure cap is exhausted
    4. Lane is disabled (equity/crypto operator toggle)
    5. Intent is already executed (idempotency)

Risk does NOT:
    * second-guess the brain (Brain layer)
    * second-guess the Seat (Seat layer)
    * run dry-runs or simulations (those were diagnostic)
    * score the setup quality (that was diagnostic)
    * apply confidence floors (Seat policy, not money safety)

If Risk says `ok=False`, broker is never called. Period.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional


@dataclass(frozen=True)
class RiskCheck:
    ok: bool
    reason: str
    notional_usd: float
    cap_per_order_usd: float
    cap_daily_usd: float
    spent_today_usd: float


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _per_order_cap() -> float:
    try:
        return float(os.environ.get("RISEDUAL_CAP_PER_ORDER_USD", "10"))
    except (TypeError, ValueError):
        return 10.0


def per_order_cap() -> float:
    """Public accessor for the per-order USD cap. Callers use this to
    reason about the cap without duplicating the env-parse logic — e.g.
    the auto-router's cap-authority guard checks it against the Kraken
    pair-floor before calling risk.check (2026-02-28 doctrine: cap is
    authority, floor is exchange constraint; if they conflict, block
    honestly)."""
    return _per_order_cap()


def _daily_cap() -> float:
    try:
        return float(os.environ.get("RISEDUAL_CAP_DAILY_USD", "1000"))
    except (TypeError, ValueError):
        return 1000.0


# ── 2026-07-24 hot-path audit P1 #3: every per-intent gate read below
# comes from the in-memory ExecutionPolicySnapshot (SQLite-recovered,
# async-refreshed from Atlas) — ZERO synchronous Atlas round trips in
# the execution loop. Async signatures kept for caller compatibility.

async def _daily_cap_effective() -> float:
    """Operator override (`runtime_flags._id=risk_caps.cap_daily_usd`)
    beats the env default. Snapshot-backed; refreshes only when an
    admin write marked the snapshot dirty."""
    from shared.hotpath import policy_snapshot  # noqa: WPS433
    await policy_snapshot.ensure_fresh()
    ov = policy_snapshot.get().get("cap_daily_usd_override")
    if ov is not None:
        return float(ov)
    return _daily_cap()


async def _is_freeze_on() -> bool:
    """Master Trading Switch — when OFF, every Risk check fails.
    Snapshot-backed (`master_trading_switch` flag). Default: ARMED
    (freeze inactive) when the flag was never written."""
    from shared.hotpath import policy_snapshot  # noqa: WPS433
    return policy_snapshot.is_freeze_on()


async def _is_lane_enabled(lane: str) -> bool:
    """Per-lane operator toggle (`lane_enabled` flag). Snapshot-backed;
    defaults to enabled when the doc/key is missing."""
    from shared.hotpath import policy_snapshot  # noqa: WPS433
    return policy_snapshot.is_lane_enabled(lane)


async def _daily_spent_usd() -> float:
    """Today's spend from the LOCAL counter (incremented at execution
    time, SQLite-persisted, rebuilt at boot) — replaces the per-intent
    Atlas `executions` aggregate."""
    from shared.hotpath import daily_spend  # noqa: WPS433
    return daily_spend.get_spent()


async def check(
    intent: dict[str, Any],
    *,
    notional_usd: Optional[float] = None,
) -> RiskCheck:
    """Apply all hard limits. Returns a RiskCheck. Caller must respect
    `ok` — if False, do NOT call the broker."""
    lane = (intent.get("lane") or "").lower()
    intent_id = intent.get("intent_id") or ""

    per_order = _per_order_cap()
    daily = await _daily_cap_effective()
    n = float(notional_usd) if notional_usd is not None else per_order
    n = min(n, per_order)
    spent = await _daily_spent_usd()

    base = dict(
        notional_usd=n,
        cap_per_order_usd=per_order,
        cap_daily_usd=daily,
        spent_today_usd=spent,
    )

    if intent.get("executed"):
        return RiskCheck(ok=False, reason="already_executed", **base)

    if await _is_freeze_on():
        return RiskCheck(ok=False, reason="master_freeze_on", **base)

    if not await _is_lane_enabled(lane):
        return RiskCheck(ok=False, reason=f"lane_disabled:{lane}", **base)

    # Gain Goal drawdown hard stop (2026-07-25): a BINDING risk rule.
    # New ENTRIES for a breached lane are blocked until operator ack
    # or window reset; exits (SELL/COVER) keep flowing so open
    # positions are managed and closed safely. Snapshot-backed.
    action = (intent.get("action") or "").upper()
    if action in ("BUY", "SHORT"):
        from shared.hotpath import policy_snapshot  # noqa: WPS433
        gg = policy_snapshot.get().get("gain_goal") or {}
        if (gg.get("block") or {}).get(lane):
            return RiskCheck(
                ok=False, reason=f"gain_goal_drawdown_breach:{lane}", **base,
            )

    if n <= 0:
        return RiskCheck(ok=False, reason="notional_zero_or_negative", **base)

    if (spent + n) > daily:
        return RiskCheck(
            ok=False,
            reason=f"daily_cap_exceeded:spent={spent:.2f}+req={n:.2f}>cap={daily:.2f}",
            **base,
        )

    # Idempotency double-check against the LOCAL intent queue —
    # `_finalize_gate_state` marks executed synchronously there, so a
    # concurrent route is caught without an Atlas round trip
    # (2026-07-24 hot-path audit).
    if intent_id:
        try:
            from shared.hotpath import intent_queue  # noqa: WPS433
            if intent_queue.is_executed(intent_id):
                return RiskCheck(
                    ok=False, reason="already_executed_concurrent", **base,
                )
        except Exception:  # noqa: BLE001
            pass

    return RiskCheck(ok=True, reason="ok", **base)


__all__ = ["RiskCheck", "check"]

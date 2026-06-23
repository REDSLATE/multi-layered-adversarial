"""Hard exposure caps — code-level rails enforced on every order route.

Doctrine (Week 1 paper):
  * $10  per order  — notional cap on a single intent's order
  * $50  per day    — sum of executed order notional in the rolling 24h window
  * $100 open notional — total live market value across open positions

These caps are SOFTWARE — there is no operator UI to relax them. To
loosen them you change the constants here and redeploy. That's
deliberate: caps are battle-tested in paper so they're proven by the
time live trading lands.

Caps are evaluated by the gate chain BEFORE the broker is touched.
Failure raises `CapExceeded`, which the chain turns into a blocking gate.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from db import db
from namespaces import EXECUTION_RECEIPTS
# Per-lane caps — each lane's per-order ceiling lives in its own
# subpackage so a crypto-only change doesn't touch the equity tree.
# (2026-02-16 reorg.)
from shared.crypto.exposure_caps import CRYPTO_PER_ORDER_USD as _CRYPTO_PER_ORDER_USD


import os


def _env_float(key: str, default: float) -> float:
    raw = os.environ.get(key)
    if raw in (None, ""):
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


# Paper-trading rails. Change here = redeploy — OR set the env override
# below for live-pilot tightening without a redeploy.
#
# 2026-05-14: Caps lifted for paper-trading rollout. Operator confirmed
# the brains should trade freely on paper. The cap STRUCTURE stays in
# place so it can be tightened the day we move toward live trading.
#
# 2026-06-07 (live $500 pilot): env overrides added so the operator
# can ratchet caps DOWN live without touching code:
#   RISEDUAL_CAP_PER_ORDER_USD   — single-order ceiling
#   RISEDUAL_CAP_PER_DAY_USD     — rolling-24h spend ceiling
#   RISEDUAL_CAP_OPEN_NOTIONAL_USD — total open notional ceiling
#   RISEDUAL_CAP_PER_ORDER_EQUITY_USD — per-lane override (equity)
#   RISEDUAL_CAP_PER_ORDER_CRYPTO_USD — per-lane override (crypto)
# Doctrine: env can only TIGHTEN, never loosen — but enforcement of
# that invariant is operator discipline, not code. Pick low values.
CAP_PER_ORDER_USD: float = _env_float("RISEDUAL_CAP_PER_ORDER_USD", 100_000.0)
CAP_PER_DAY_USD: float = _env_float("RISEDUAL_CAP_PER_DAY_USD", 1_000_000.0)
CAP_OPEN_NOTIONAL_USD: float = _env_float("RISEDUAL_CAP_OPEN_NOTIONAL_USD", 1_000_000.0)


# ─── Mongo-backed cap overrides (2026-02-21) ───────────────────────
# Same pattern as the Webull floor override: a Mongo doc wins over
# env, which wins over module default. The operator can raise/lower
# caps from the admin UI without touching deploy env. This was
# motivated by the live-pilot $50/day cap blocking trades on a
# Wednesday morning before market open, with no way to flip the env
# var from a phone.
#
# Mongo doc:
#   runtime_flags._id = "exposure_caps_override"
#   { enabled: true, per_order_usd, per_day_usd, open_notional_usd,
#     updated_at, updated_by, reason }
import time as _time  # noqa: E402

_CAPS_OVERRIDE_CACHE: dict = {}
_CAPS_OVERRIDE_TS: float = 0.0
_CAPS_CACHE_TTL_SEC: float = 5.0
_CAPS_FLAG_DOC_ID = "exposure_caps_override"
# 2026-06-22 — operator-pinned 24h spend reset. When the operator
# wants to "start over" mid-window (e.g., they hit the cap from
# pre-pilot fills but want the rest of the day's budget back), they
# call POST /admin/exposure-caps/reset-daily-spend. That writes
# `runtime_flags._id="daily_spend_reset"` with `reset_at = now`.
# `daily_spend_usd()` then only sums receipts AFTER `reset_at`,
# which is mathematically equivalent to "wipe the 24h tally to $0".
# The reset naturally ages out: once `now - reset_at >= 24h`, all
# pre-reset receipts are outside the window anyway, so the reset
# doc becomes a no-op and the normal rolling behavior resumes.
_DAILY_SPEND_RESET_DOC_ID = "daily_spend_reset"


async def get_daily_spend_reset_at() -> Optional[str]:
    """Return the ISO timestamp of the most recent 24h spend reset,
    or None if no reset has been requested (or the reset has aged
    out past the 24h window). Doctrine: the reset is a baseline
    timestamp, never a delete — audit rows in execution_receipts
    are untouched."""
    doc = await db["runtime_flags"].find_one(
        {"_id": _DAILY_SPEND_RESET_DOC_ID},
        {"_id": 0, "reset_at": 1},
    )
    if not doc:
        return None
    reset_at = doc.get("reset_at")
    if not isinstance(reset_at, str) or not reset_at:
        return None
    return reset_at


def _read_cap_override(field: str) -> Optional[float]:
    """Return the cached Mongo override for `field`, or None if absent
    or stale. Field is one of {per_order_usd, per_day_usd,
    open_notional_usd}."""
    if _CAPS_OVERRIDE_TS == 0.0:
        return None
    if (_time.time() - _CAPS_OVERRIDE_TS) > _CAPS_CACHE_TTL_SEC * 30:
        return None
    if not _CAPS_OVERRIDE_CACHE.get("enabled", False):
        return None
    v = _CAPS_OVERRIDE_CACHE.get(field)
    return float(v) if isinstance(v, (int, float)) and v > 0 else None


async def refresh_cap_overrides_cache() -> dict:
    """Force-refresh the in-memory cap-override cache from Mongo."""
    global _CAPS_OVERRIDE_CACHE, _CAPS_OVERRIDE_TS
    try:
        doc = await db["runtime_flags"].find_one(
            {"_id": _CAPS_FLAG_DOC_ID},
            {"_id": 0},
        ) or {}
        _CAPS_OVERRIDE_CACHE = doc
        _CAPS_OVERRIDE_TS = _time.time()
        return _CAPS_OVERRIDE_CACHE
    except Exception:  # noqa: BLE001
        return _CAPS_OVERRIDE_CACHE


def effective_cap_per_order_usd() -> float:
    return _read_cap_override("per_order_usd") or CAP_PER_ORDER_USD


def effective_cap_per_day_usd() -> float:
    return _read_cap_override("per_day_usd") or CAP_PER_DAY_USD


def effective_cap_open_notional_usd() -> float:
    return _read_cap_override("open_notional_usd") or CAP_OPEN_NOTIONAL_USD

# Per-lane override. Set entries to None for "use the global cap".
# These overrides apply to the per-order cap only — day/open caps
# still use the globals above.
#
# Doctrine pin (2026-06-07): only ADD a lane to this dict when the
# operator explicitly sets `RISEDUAL_CAP_PER_ORDER_<LANE>_USD`. The
# gate-chain emits a gate named `cap_per_order_<lane>` ONLY when the
# lane appears here, otherwise the canonical `cap_per_order` gate
# applies. Implicitly mirroring the global cap into every lane
# would rename gates for tests + dashboards.
CAP_PER_ORDER_BY_LANE: dict[str, float] = {
    "crypto": _env_float(
        "RISEDUAL_CAP_PER_ORDER_CRYPTO_USD", _CRYPTO_PER_ORDER_USD,
    ),
}
if os.environ.get("RISEDUAL_CAP_PER_ORDER_EQUITY_USD"):
    CAP_PER_ORDER_BY_LANE["equity"] = _env_float(
        "RISEDUAL_CAP_PER_ORDER_EQUITY_USD", CAP_PER_ORDER_USD,
    )


class CapExceeded(Exception):
    """Raised when a planned order would breach a hard cap."""


@dataclass
class CapEvaluation:
    name: str
    cap_usd: float
    current_usd: float
    projected_usd: float
    passed: bool
    reason: str


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def daily_spend_usd(window_hours: int = 24) -> float:
    """Sum of executed order notional in the last `window_hours`.

    Honors the operator's `daily_spend_reset` flag: if a reset
    timestamp exists AND it falls inside the rolling window, the
    floor for the sum moves up to the reset time. This effectively
    wipes the 24h tally to $0 at reset time and lets fresh spend
    accumulate from there. Receipts older than the reset still live
    in `execution_receipts` for audit — they're just excluded from
    cap math.
    """
    window_start = (_now() - timedelta(hours=window_hours)).isoformat()
    reset_at = await get_daily_spend_reset_at()
    # Floor the lookback at the more recent of (window_start,
    # reset_at). If the reset was MORE than 24h ago, it's already
    # outside the window and has no effect — naturally aged out.
    since = max(window_start, reset_at) if reset_at else window_start
    cursor = db[EXECUTION_RECEIPTS].find(
        {"executed_at": {"$gte": since}, "side": {"$in": ["BUY", "SELL"]}},
        {"_id": 0, "notional_usd": 1, "side": 1},
    )
    total = 0.0
    async for row in cursor:
        # Treat BUY notional as "spend" and SELL notional as "spend" too —
        # we cap *trading throughput* per day, not just net inflow.
        total += float(row.get("notional_usd") or 0.0)
    return total


async def open_notional_usd() -> float:
    """Sum of |market_value| across live positions at the equity broker.
    Returns 0.0 if no broker is connected (we still let the
    per-order/per-day caps work in dry-run mode).

    2026-02-19: post-Alpaca-deprecation this routes through the lane
    adapter (Webull). Crypto positions are sized by the crypto cap
    stack in `shared/crypto/exposure_caps.py`; here we only count
    equity exposure for the global open-notional ceiling.
    """
    try:
        from shared.broker_router import adapter_for_lane  # noqa: WPS433
        adapter = await adapter_for_lane("equity")
    except Exception:  # noqa: BLE001
        return 0.0
    if not adapter:
        return 0.0
    try:
        positions = await adapter.list_positions()
    except Exception:  # noqa: BLE001
        return 0.0
    return sum(abs(float(p.get("market_value") or 0.0)) for p in positions)


def evaluate_per_order(order_notional_usd: float, lane: Optional[str] = None) -> CapEvaluation:
    # 2026-02-21: Mongo override (when present) wins over the env/default
    # global cap. Lane-specific overrides (CAP_PER_ORDER_BY_LANE) still
    # take priority when set.
    if lane in CAP_PER_ORDER_BY_LANE:
        cap = CAP_PER_ORDER_BY_LANE[lane]
    else:
        cap = effective_cap_per_order_usd()
    passed = order_notional_usd <= cap
    label = f"cap_per_order_{lane}" if lane in CAP_PER_ORDER_BY_LANE else "cap_per_order"
    return CapEvaluation(
        name=label,
        cap_usd=cap,
        current_usd=0.0,
        projected_usd=order_notional_usd,
        passed=passed,
        reason=(
            f"order notional ${order_notional_usd:.2f} ≤ {label} cap ${cap:.2f}"
            if passed else
            f"order notional ${order_notional_usd:.2f} exceeds {label} cap ${cap:.2f}"
        ),
    )


async def evaluate_daily(order_notional_usd: float) -> CapEvaluation:
    spent = await daily_spend_usd()
    projected = spent + order_notional_usd
    cap = effective_cap_per_day_usd()
    passed = projected <= cap
    return CapEvaluation(
        name="cap_per_day",
        cap_usd=cap,
        current_usd=spent,
        projected_usd=projected,
        passed=passed,
        reason=(
            f"24h spend ${spent:.2f} + new ${order_notional_usd:.2f} = "
            f"${projected:.2f} ≤ cap ${cap:.2f}"
            if passed else
            f"24h spend ${spent:.2f} + new ${order_notional_usd:.2f} = "
            f"${projected:.2f} would exceed daily cap ${cap:.2f}"
        ),
    )


async def evaluate_open_notional(
    order_notional_usd: float,
    side: str,
    *,
    position_evolution: Optional[str] = None,
) -> CapEvaluation:
    """Cap check on TOTAL OPEN NOTIONAL (sum of |market_value| across
    live positions).

    Doctrine pin (2026-06-10, P1 — sizing sign-flip fix):

        The old logic was `is_opening = side in ("BUY", "SHORT")`.
        That's wrong for short positions — a BUY against an existing
        SHORT is a COVER (shrinks exposure), but the old check
        counted it as opening and added its notional to the
        projected total. Symmetric inversion: a SELL against an
        existing SHORT is an ADD (grows exposure) but the old check
        counted it as closing.

        Fix: when the caller passes `position_evolution` (from
        position_model.classify_position_evolution / brain runner),
        use it as the source of truth for "does this trade actually
        grow exposure?" — anything in {OPEN, ADD, FLIP, SCALE_IN}
        grows; anything in {REDUCE, CLOSE, PARTIAL_COVER, FULL_COVER,
        SCALE_OUT, HOLD} shrinks or holds.

        Backward compatible: callers that don't pass
        `position_evolution` get the legacy side-only heuristic
        (still wrong for COVERs but doesn't regress the existing
        flat-position case which dominates real-world traffic).
    """
    current = await open_notional_usd()

    # Position-evolution-aware path (doctrinal source of truth).
    if position_evolution:
        pe = (position_evolution or "").lower().strip()
        # Anything that grows magnitude → adds to open exposure.
        GROWS = {"open", "add", "flip", "scale_in"}
        # Anything that shrinks magnitude or holds → no growth.
        SHRINKS = {
            "reduce", "close", "partial_cover", "full_cover",
            "scale_out", "hold",
        }
        if pe in GROWS:
            is_opening = True
        elif pe in SHRINKS:
            is_opening = False
        else:
            # Unknown evolution → fall back to the side heuristic
            # rather than guessing. Operator can audit via the gate
            # reason string.
            is_opening = (side or "").upper() in ("BUY", "SHORT")
    else:
        # Legacy heuristic (kept for callers that don't yet carry
        # position context).
        is_opening = (side or "").upper() in ("BUY", "SHORT")

    projected = current + (order_notional_usd if is_opening else 0.0)
    cap = effective_cap_open_notional_usd()
    passed = projected <= cap
    grew = "grows" if is_opening else "no growth"
    src = position_evolution or "side-only"
    return CapEvaluation(
        name="cap_open_notional",
        cap_usd=cap,
        current_usd=current,
        projected_usd=projected,
        passed=passed,
        reason=(
            f"open notional ${current:.2f}"
            + (f" + new ${order_notional_usd:.2f}" if is_opening else "")
            + f" = ${projected:.2f} ≤ cap ${cap:.2f}"
            + f" ({grew}; source={src})"
            if passed else
            f"open notional ${current:.2f} + new ${order_notional_usd:.2f}"
            + f" = ${projected:.2f} would exceed open-notional cap ${cap:.2f}"
            f" ({grew}; source={src})"
        ),
    )


async def evaluate_all(
    order_notional_usd: float,
    side: str,
    lane: Optional[str] = None,
    *,
    position_evolution: Optional[str] = None,
) -> list[CapEvaluation]:
    """Run every cap check. Returns ordered list of CapEvaluation."""
    return [
        evaluate_per_order(order_notional_usd, lane=lane),
        await evaluate_daily(order_notional_usd),
        await evaluate_open_notional(
            order_notional_usd, side, position_evolution=position_evolution,
        ),
    ]


def caps_snapshot() -> dict:
    """Single source of truth for exposure caps. Returns globals plus
    per-lane overrides so UI / Mission Control / RoadGuard all read the
    same numbers. Adding a new lane override propagates everywhere
    without UI changes."""
    return {
        "per_order_usd": CAP_PER_ORDER_USD,
        "per_day_usd": CAP_PER_DAY_USD,
        "open_notional_usd": CAP_OPEN_NOTIONAL_USD,
        "per_order_by_lane_usd": dict(CAP_PER_ORDER_BY_LANE),
    }


def cap_for_lane(lane: Optional[str]) -> float:
    """Resolve the effective per-order cap for `lane`. Falls back to
    the global per-order cap when no lane override exists."""
    if lane and lane in CAP_PER_ORDER_BY_LANE:
        return CAP_PER_ORDER_BY_LANE[lane]
    return CAP_PER_ORDER_USD

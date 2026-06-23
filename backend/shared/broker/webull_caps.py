"""Webull route caps — pre-trade gate for the Webull broker.

Doctrine pin (operator, 2026-06-10):

    Webull goes LIVE on day one. There is no paper/UAT stop. To keep
    the blast radius small while we shake the integration out, EVERY
    Webull order MUST pass through this gate before it can leave the
    backend:

        1. The "armed" gate (`WEBULL_ARMED=true`) must be flipped on
           in `.env` by the operator. Without it, the route is dead.
           Default is FALSE so a stale or accidentally-deployed env
           cannot trade.

        2. The notional MUST satisfy
               WEBULL_MIN_NOTIONAL_USD  ≤  notional  ≤  WEBULL_MAX_NOTIONAL_USD
           Defaults: $1.00 ≤ N ≤ $10.00. Operator can widen via env
           later, but the floor and ceiling exist precisely to keep
           the live-pilot cost-per-mistake bounded.

           2026-02-19 (rev): floor lowered from $3 → $1 because Webull
           fractional shares clear at a $1 minimum (per their order
           docs). Holding the floor at $3 was over-conservative and
           was forcing the gate to drop legit small fractional intents
           on cheap tickers (e.g., a 0.05-share intent on a $40 ticker
           = $2.00 notional, which is well within Webull's fractional
           tier but was bouncing off the gate). Lowering to $1 widens
           the playable universe without changing the blast-radius
           ceiling.

           2026-02-21 (rev): added Mongo-backed override so the operator
           can flip the floor from the admin UI without redeploying.
           Mongo flag `runtime_flags._id="webull_min_notional_floor"`
           wins over env var when present. This unblocks the case where
           Production deploy env was set to $3.00 and the operator
           cannot easily edit deploy env from their phone.

    These caps are ADDITIVE to the existing $500 exposure cap, the
    in-flight dedupe, and the position-misread detection — they do
    not replace any of them. They apply EXCLUSIVELY to orders that
    route via Webull; Kraken/Public.com orders are untouched.

Reading env vars at call-time (not import-time) so the operator can
flip the armed flag or widen the band without restarting supervisor.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Optional


# Sentinel values used by the router when the gate refuses an order.
# 2026-02-19 (rev): min lowered $3 → $1 to align with Webull's
# fractional-share order minimum. Max held at $10 for blast-radius.
DEFAULT_MIN_NOTIONAL_USD = 1.00
DEFAULT_MAX_NOTIONAL_USD = 10.00

# 2026-02-20 (operator directive): the static `WEBULL_MAX_NOTIONAL_USD`
# ceiling was forcing the operator to hand-tune env vars every time the
# brain's `RISEDUAL_CAP_PER_ORDER_USD` budget moved. Replace the static
# ceiling with a buying-power-scaled cap: per-order notional is capped
# at `buying_power * WEBULL_PCT_OF_BUYING_POWER`, then clamped between
# the hard floor and the hard sanity ceiling. This means the operator
# raises the per-order budget once (as a % of equity) and never has to
# touch a dollar-denominated cap again as the account grows or shrinks.
#
# Static env-var ceiling is kept as a hard upper rail — if the operator
# ever pins `WEBULL_MAX_NOTIONAL_USD` to a literal dollar value, the
# dynamic cap will never exceed it (defense-in-depth).
DEFAULT_PCT_OF_BUYING_POWER = 0.10  # 10% of buying power per order
# Raised from 0.05 → 0.10 on 2026-02-23 (Monday) after operator observed
# WEBULL_NOTIONAL_ABOVE_CAP rejecting every equity intent: 5% of a
# $470 buying power account ≈ $23.55, which sat below the $25 min
# notional floor → 100% equity submit rejection. 10% gives $47 headroom
# on the same account and leaves room for the min-notional floor.
# Operator can still override via `WEBULL_PCT_OF_BUYING_POWER` runtime flag.
HARD_SANITY_CEILING_USD = 500.00    # absolute panic ceiling regardless of BP


# ─── Mongo-backed floor override (2026-02-21) ──────────────────────
# Same pattern as `shared.pipeline.adapter.refresh_pipeline_flag_cache`.
# The Mongo override wins over env vars so the operator can flip the
# floor from the admin UI without touching deploy config.
_CACHED_FLOOR_OVERRIDE: Optional[float] = None
_CACHED_FLOOR_TS: float = 0.0
_FLOOR_CACHE_TTL_SEC: float = 5.0
_FLOOR_FLAG_DOC_ID = "webull_min_notional_floor"

# ─── Mongo-backed pct-of-buying-power override (2026-06-23) ────────
# Mirrors the floor-override pattern above. Default rollout is 10% of
# buying power (DEFAULT_PCT_OF_BUYING_POWER); but on a small account
# (~$470 BP) that's $47/order, which clips intents the brain sized at
# $100. The operator wanted a phone-friendly dial — no redeploy — so
# we add a Mongo override that wins over env. Doc id mirrors the
# floor's: `webull_pct_of_buying_power`, fields `pct` + `enabled`.
_CACHED_PCT_OVERRIDE: Optional[float] = None
_CACHED_PCT_TS: float = 0.0
_PCT_CACHE_TTL_SEC: float = 5.0
_PCT_FLAG_DOC_ID = "webull_pct_of_buying_power"


def _read_cached_floor_override() -> Optional[float]:
    """Return the cached Mongo override if recent, else None.

    The cache is populated by `refresh_webull_floor_cache()` — called
    at boot and by the admin set-floor endpoint. We keep the read sync
    because `evaluate_webull_order()` is sync; the async refresh task
    keeps the cache warm.
    """
    if _CACHED_FLOOR_TS == 0.0:
        return None
    if (time.time() - _CACHED_FLOOR_TS) > _FLOOR_CACHE_TTL_SEC * 30:
        # Cache went stale (>150s). Return None so the next path
        # (env / default) wins. Refresh task should be running.
        return None
    return _CACHED_FLOOR_OVERRIDE


async def refresh_webull_floor_cache() -> Optional[float]:
    """Force-refresh the in-memory floor cache from Mongo. Called at
    boot and by `POST /api/admin/webull-caps/set-floor` so the cache is
    coherent with the operator's last action."""
    global _CACHED_FLOOR_OVERRIDE, _CACHED_FLOOR_TS
    from db import db
    try:
        doc = await db["runtime_flags"].find_one(
            {"_id": _FLOOR_FLAG_DOC_ID},
            {"_id": 0, "floor_usd": 1, "enabled": 1},
        )
        if doc and bool(doc.get("enabled", True)):
            raw = doc.get("floor_usd")
            if isinstance(raw, (int, float)) and raw > 0:
                _CACHED_FLOOR_OVERRIDE = float(raw)
            else:
                _CACHED_FLOOR_OVERRIDE = None
        else:
            _CACHED_FLOOR_OVERRIDE = None
        _CACHED_FLOOR_TS = time.time()
        return _CACHED_FLOOR_OVERRIDE
    except Exception:  # noqa: BLE001
        # Failed Mongo read leaves cache as-is; caller falls back to env.
        return _CACHED_FLOOR_OVERRIDE


def _read_cached_pct_override() -> Optional[float]:
    """Cached read of the Mongo pct-of-buying-power override. Sync so
    `evaluate_webull_order()` (sync) can call it without await."""
    if _CACHED_PCT_TS == 0.0:
        return None
    if (time.time() - _CACHED_PCT_TS) > _PCT_CACHE_TTL_SEC * 30:
        return None
    return _CACHED_PCT_OVERRIDE


async def refresh_webull_pct_cache() -> Optional[float]:
    """Force-refresh the in-memory pct cache from Mongo. Called at
    boot and by `POST /api/admin/webull-caps/set-pct` so the cache
    reflects the operator's last action immediately."""
    global _CACHED_PCT_OVERRIDE, _CACHED_PCT_TS
    from db import db
    try:
        doc = await db["runtime_flags"].find_one(
            {"_id": _PCT_FLAG_DOC_ID},
            {"_id": 0, "pct": 1, "enabled": 1},
        )
        if doc and bool(doc.get("enabled", True)):
            raw = doc.get("pct")
            if isinstance(raw, (int, float)) and 0 < float(raw) <= 1.0:
                _CACHED_PCT_OVERRIDE = float(raw)
            else:
                _CACHED_PCT_OVERRIDE = None
        else:
            _CACHED_PCT_OVERRIDE = None
        _CACHED_PCT_TS = time.time()
        return _CACHED_PCT_OVERRIDE
    except Exception:  # noqa: BLE001
        return _CACHED_PCT_OVERRIDE


class WebullCapBlocked(Exception):
    """Raised when the Webull pre-trade cap refuses an order. Treated
    as a fail-closed NO_TRADE by the broker router."""


@dataclass(frozen=True)
class WebullCapDecision:
    ok: bool
    reason: str
    min_usd: float
    max_usd: float
    armed: bool
    # 2026-02-20: surfaced for trace/post-mortem visibility so the
    # operator can see exactly how the per-order cap was computed.
    buying_power_usd: Optional[float] = None
    pct_of_bp: Optional[float] = None
    cap_source: str = "env"  # "env" | "buying_power" | "sanity_ceiling"

    def raise_if_blocked(self) -> None:
        if not self.ok:
            raise WebullCapBlocked(self.reason)


def _read_float_env(key: str, default: float) -> float:
    """Tolerant float reader — empty or malformed env falls back to
    the doctrine default rather than crashing the route."""
    raw = (os.environ.get(key) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def is_webull_armed() -> bool:
    """Operator-flipped kill switch. Default OFF (fail-closed)."""
    return (os.environ.get("WEBULL_ARMED") or "").strip().lower() in {
        "true", "1", "yes", "on",
    }


def webull_pct_of_buying_power() -> float:
    """Per-order budget as a fraction of Webull buying power.

    Lookup order (highest precedence first):
      1. Mongo override (`runtime_flags._id="webull_pct_of_buying_power"`)
         — operator's phone-friendly dial. Set via admin endpoint.
      2. Env var `WEBULL_PCT_OF_BUYING_POWER` (e.g. `0.05` = 5%).
      3. `DEFAULT_PCT_OF_BUYING_POWER` (10%).

    Returns the first value in (0, 1.0]; anything outside that range
    or malformed reverts to the next source.
    """
    override = _read_cached_pct_override()
    if override is not None and 0 < override <= 1.0:
        return override
    pct = _read_float_env(
        "WEBULL_PCT_OF_BUYING_POWER", DEFAULT_PCT_OF_BUYING_POWER,
    )
    if pct <= 0 or pct > 1.0:
        return DEFAULT_PCT_OF_BUYING_POWER
    return pct


def webull_notional_band(
    buying_power_usd: Optional[float] = None,
) -> tuple[float, float, str]:
    """Return `(min_usd, max_usd, cap_source)` for the Webull route.

    `cap_source` is one of:
      * `"buying_power"` — ceiling derived from BP × pct
      * `"env"`          — ceiling derived from `WEBULL_MAX_NOTIONAL_USD`
      * `"sanity_ceiling"` — ceiling pinned at the hard panic rail

    Doctrine (2026-02-20): when `buying_power_usd` is supplied and > 0,
    the ceiling is computed as `min(bp * pct, env_max, sanity_ceiling)`.
    The env-var `WEBULL_MAX_NOTIONAL_USD` becomes an **upper bound on
    the dynamic cap** — the operator can still pin a hard dollar
    ceiling if they want belt-and-suspenders, but no longer needs to
    bump it every time the account or per-order budget changes.

    When `buying_power_usd` is None / zero / negative, falls back to
    the legacy env-only behavior so we don't go to zero on a transient
    Webull balance-fetch failure (the gate would NO_TRADE the order
    anyway via the `WEBULL_NOTIONAL_ABOVE_CAP` path, which is safer
    than silently routing the order at $0).
    """
    lo = _read_float_env("WEBULL_MIN_NOTIONAL_USD", DEFAULT_MIN_NOTIONAL_USD)
    env_hi = _read_float_env(
        "WEBULL_MAX_NOTIONAL_USD", DEFAULT_MAX_NOTIONAL_USD,
    )
    # 2026-02-21: Mongo override wins over env for the floor. This is
    # how the operator flips $3 → $1 (or any value) from the admin UI
    # without redeploying. The env var remains as a fallback for
    # operators who prefer deploy-config control.
    mongo_floor = _read_cached_floor_override()
    if mongo_floor is not None:
        lo = mongo_floor
    # Sanity rails — never let the floor invert.
    lo = max(0.01, lo)

    # Dynamic ceiling from buying power, when available.
    cap_source = "env"
    if buying_power_usd is not None and buying_power_usd > 0:
        pct = webull_pct_of_buying_power()
        bp_cap = buying_power_usd * pct
        # Pick the smallest of the three ceilings, but tag the source
        # of whichever one bound the result so the operator can see
        # in the trace why the cap was what it was.
        candidates = [
            (bp_cap, "buying_power"),
            (env_hi, "env"),
            (HARD_SANITY_CEILING_USD, "sanity_ceiling"),
        ]
        hi, cap_source = min(candidates, key=lambda x: x[0])
    else:
        hi = min(env_hi, HARD_SANITY_CEILING_USD)
        if hi == HARD_SANITY_CEILING_USD and env_hi > HARD_SANITY_CEILING_USD:
            cap_source = "sanity_ceiling"

    # Floor must never exceed ceiling.
    hi = max(lo, hi)
    return lo, hi, cap_source


def evaluate_webull_order(
    *,
    notional_usd: Optional[float],
    symbol: str,
    buying_power_usd: Optional[float] = None,
) -> WebullCapDecision:
    """The single decision point for whether an order can route via
    Webull. The router calls this BEFORE invoking the adapter.

    `buying_power_usd` (2026-02-20): when supplied, the per-order
    ceiling is computed dynamically as a percent of buying power
    rather than a static dollar value. Falls back gracefully to the
    env-only ceiling when BP is unavailable.

    Returns a `WebullCapDecision`. Callers that want exception
    semantics can use `decision.raise_if_blocked()`.
    """
    lo, hi, cap_source = webull_notional_band(buying_power_usd)
    armed = is_webull_armed()
    pct = webull_pct_of_buying_power() if buying_power_usd else None

    def _decision(ok: bool, reason: str) -> WebullCapDecision:
        return WebullCapDecision(
            ok=ok,
            reason=reason,
            min_usd=lo,
            max_usd=hi,
            armed=armed,
            buying_power_usd=buying_power_usd,
            pct_of_bp=pct,
            cap_source=cap_source,
        )

    if not armed:
        return _decision(
            False,
            "WEBULL_NOT_ARMED — set WEBULL_ARMED=true in .env to "
            "enable the Webull route; NO_TRADE",
        )

    if notional_usd is None:
        return _decision(
            False,
            "WEBULL_NOTIONAL_MISSING — router must pass notional_usd",
        )

    if notional_usd < lo:
        return _decision(
            False,
            f"WEBULL_NOTIONAL_BELOW_FLOOR — ${notional_usd:.2f} "
            f"< ${lo:.2f} for {symbol}; NO_TRADE",
        )

    if notional_usd > hi:
        # Tag the trace with the cap source so the operator immediately
        # sees whether to raise the env ceiling, the BP %, or fund the
        # account.
        if cap_source == "buying_power":
            detail = (
                f" (cap = {pct:.0%} of ${buying_power_usd:.2f} buying "
                f"power; raise WEBULL_PCT_OF_BUYING_POWER or fund "
                f"account)"
            )
        elif cap_source == "sanity_ceiling":
            detail = (
                f" (hit hard sanity ceiling ${HARD_SANITY_CEILING_USD:.2f}; "
                f"edit HARD_SANITY_CEILING_USD if you really need more)"
            )
        else:
            detail = (
                f" (env ceiling WEBULL_MAX_NOTIONAL_USD=${hi:.2f}; raise "
                f"it or rely on dynamic BP cap)"
            )
        return _decision(
            False,
            f"WEBULL_NOTIONAL_ABOVE_CAP — ${notional_usd:.2f} "
            f"> ${hi:.2f} for {symbol}{detail}; NO_TRADE",
        )

    return _decision(True, "WEBULL_CAP_OK")

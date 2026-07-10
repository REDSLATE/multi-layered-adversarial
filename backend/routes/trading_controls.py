"""Trading controls — runtime kill switch and read-only status.

Doctrine pin (2026-05-26):
    The operator MUST be able to halt new broker orders without
    redeploying. The mechanism is a Mongo-backed singleton doc that
    auto-router consults on every tick. Flipping it OFF via the API
    takes effect within `AUTO_ROUTER_INTERVAL_SEC` (default 30s).

    Two independent layers protect order routing:
      1. Env var `AUTO_ROUTER_ENABLED` (deploy-time, requires restart)
      2. Mongo `trading_controls.enabled` (runtime, instant)
    Both must be True for orders to fire. EITHER being False halts.

    Halt is non-destructive: existing positions stay open, broker
    reconciliation keeps running, gates still evaluate. Only the
    final route_order() call is suppressed.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth import get_current_user
from db import db


logger = logging.getLogger("risedual.trading_controls")


COLLECTION = "trading_controls"
DOC_ID = "current"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def get_trading_status() -> dict:
    """Return the live trading-controls state. Seeds an OFF default on
    first read — fail-CLOSED: if the doc has never been touched, MC
    refuses to fire orders. Operator must explicitly enable."""
    doc = await db[COLLECTION].find_one({"_id": DOC_ID}, {"_id": 0})
    if doc:
        return doc
    seed = {
        "enabled": False,
        "reason": "first_boot_default_disabled",
        "updated_at": _now_iso(),
        "updated_by": "system_default",
    }
    await db[COLLECTION].update_one(
        {"_id": DOC_ID},
        {"$set": seed, "$setOnInsert": {"created_at": _now_iso()}},
        upsert=True,
    )
    return seed


async def is_trading_enabled() -> bool:
    """Single-line check for the auto-router. Fail-CLOSED on error
    (Mongo unreachable → no orders fire)."""
    try:
        doc = await get_trading_status()
        return bool(doc.get("enabled", False))
    except Exception as exc:  # noqa: BLE001
        logger.warning("trading_controls fail-CLOSED: %s", exc)
        return False


async def set_trading_enabled(
    enabled: bool, reason: str, actor: str,
) -> dict:
    """Flip the runtime switch. Writes an audit row alongside."""
    payload = {
        "enabled": bool(enabled),
        "reason": reason or "(no reason given)",
        "updated_at": _now_iso(),
        "updated_by": actor,
    }
    await db[COLLECTION].update_one(
        {"_id": DOC_ID}, {"$set": payload}, upsert=True,
    )
    # Audit row — append-only.
    await db[f"{COLLECTION}_audit"].insert_one({
        **payload, "ts": _now_iso(),
    })
    return await get_trading_status()


# ─── HTTP surface ───
router = APIRouter(
    prefix="/admin/trading", tags=["trading-controls"],
)


class ToggleIn(BaseModel):
    enabled: bool
    reason: str = Field(default="", max_length=240)


@router.get("/status")
async def status(_user: dict = Depends(get_current_user)) -> dict:
    """Read-only state — UI polls this for the kill-switch indicator."""
    import os
    from shared import sizing_gate
    doc = await get_trading_status()
    return {
        "ok": True,
        "trading_enabled_runtime": bool(doc.get("enabled")),
        "trading_enabled_env": (
            os.environ.get("AUTO_ROUTER_ENABLED", "true").lower() == "true"
        ),
        "trading_will_fire": (
            bool(doc.get("enabled"))
            and os.environ.get("AUTO_ROUTER_ENABLED", "true").lower() == "true"
        ),
        "micro_live_enabled": sizing_gate.MICRO_LIVE_ENABLED,
        "micro_live_default_cap_usd": sizing_gate.MICRO_LIVE_DEFAULT_CAP_USD,
        "micro_live_crypto_cap_usd": sizing_gate.MICRO_LIVE_CRYPTO_CAP_USD,
        "micro_live_equity_cap_usd": sizing_gate.MICRO_LIVE_EQUITY_CAP_USD,
        "reason": doc.get("reason"),
        "updated_at": doc.get("updated_at"),
        "updated_by": doc.get("updated_by"),
    }


@router.post("/toggle")
async def toggle(
    body: ToggleIn, user: dict = Depends(get_current_user),
) -> dict:
    """Flip the kill switch. Both directions require admin auth.

    Going FROM disabled TO enabled requires a reason — that's the
    audit-chain receipt that proves the operator deliberately turned
    trading on rather than it flipping accidentally."""
    actor = (user or {}).get("email") or "operator"
    if body.enabled and not body.reason.strip():
        raise HTTPException(
            status_code=400,
            detail="reason required when enabling trading",
        )
    new_state = await set_trading_enabled(
        body.enabled, body.reason, actor,
    )
    # 2026-02-19: invalidate the auto_router's arm cache so the flip
    # takes effect on the NEXT tick instead of waiting up to
    # _ARM_CACHE_TTL_SEC (2s). Best-effort — auto_router may not
    # be importable in a test context.
    try:
        from shared.auto_router import _invalidate_arm_cache  # noqa: WPS433
        _invalidate_arm_cache()
    except Exception:  # noqa: BLE001
        pass
    logger.warning(
        "trading_controls FLIPPED: enabled=%s by=%s reason=%r",
        body.enabled, actor, body.reason,
    )
    return {"ok": True, **{k: v for k, v in new_state.items() if k != "_id"}}


@router.get("/audit")
async def audit_log(
    limit: int = 50, _user: dict = Depends(get_current_user),
) -> dict:
    """Last N kill-switch flips. Operator review surface."""
    rows = await db[f"{COLLECTION}_audit"].find(
        {}, {"_id": 0},
    ).sort("ts", -1).to_list(min(limit, 200))
    return {"items": rows, "count": len(rows)}


# ═══════════════════════════════════════════════════════════════════
# UNIFIED ARM SURFACE (2026-07-09 operator directive)
# ═══════════════════════════════════════════════════════════════════
# Doctrine:
#   Production has THREE independent "master" gates and only #1 (an
#   env var) was showing ON, giving the operator false confidence.
#   The two downstream gates (#2 auto_router-path, #3 trader-sidecar)
#   both default to CLOSED when their Mongo docs are missing — which
#   they were. Result: `LIVE / TRUE` badge on the Flags page while
#   every order silently dies at gate #2 or #3.
#
#   This endpoint FLIPS ALL DOWNSTREAM GATES in ONE call so the
#   operator can't accidentally leave the trader half-armed. It
#   writes:
#     • trading_controls._id="current"          (MC path)
#     • runtime_flags._id="master_trading_switch"  (trader path)
#     • ONE unified audit row into trading_controls_audit with
#       source="unified_arm" so both flips share a single receipt.
#
#   Auditability is non-negotiable: every arm/disarm carries actor,
#   reason, ts, and the pre/post state of BOTH docs.

_TRADER_SWITCH_DOC_ID = "master_trading_switch"
_LANE_ENABLED_DOC_ID = "lane_enabled"
_KNOWN_LANES = ("equity", "crypto")


class ArmIn(BaseModel):
    enabled: bool
    reason: str = Field(default="", max_length=240)
    # Optional per-lane override. If omitted, lane_enabled is left
    # untouched. If provided, only the lanes listed are updated (missing
    # lanes retain their current state). This lets the operator say
    # "arm the master switch but keep crypto off" in ONE call.
    lanes: Optional[dict[str, bool]] = None


class LaneToggleIn(BaseModel):
    lane: str
    enabled: bool
    reason: str = Field(default="", max_length=240)


async def _get_trader_switch_state() -> dict:
    """Read the trader-sidecar arm doc. Doesn't seed on miss — the
    trader sidecar's `state.DEFAULT_MASTER_ARMED` is the source of
    truth for absent-doc semantics, and it's `False`."""
    doc = await db["runtime_flags"].find_one(
        {"_id": _TRADER_SWITCH_DOC_ID},
        {"_id": 0},
    )
    return doc or {"enabled": False, "reason": "no_doc_default_disarmed"}


async def _get_lane_enabled_state() -> dict:
    """Read the per-lane enabled doc. Shape:
        {equity: bool, crypto: bool, updated_at, updated_by, reason}
    Both paths (`shared.risk.check._is_lane_enabled` and
    `/app/trader/state.py`) consume this SAME doc, so we only need
    one write and both pipelines see the change.

    Absent-doc convention (kept for parity with both readers):
      • `shared.risk.check` → defaults to TRUE per lane
      • trader `state.py`  → defaults to TRUE per lane (DEFAULT_LANE_ENABLED)
    We surface that convention here so the arm-status endpoint can
    render "unset (defaults to enabled)" honestly.
    """
    doc = await db["runtime_flags"].find_one(
        {"_id": _LANE_ENABLED_DOC_ID}, {"_id": 0},
    )
    return doc or {}


def _lane_from_doc(doc: dict, lane: str) -> bool:
    """Resolve a single lane's effective enabled state, applying the
    default-True convention when the field is missing."""
    v = doc.get(lane) if doc else None
    return True if v is None else bool(v)


async def _set_lane_enabled(
    lanes: dict[str, bool], reason: str, actor: str,
) -> dict:
    """Merge-update the lane_enabled doc — only the keys in `lanes`
    are written; existing lane states are preserved. Returns the
    post-update doc."""
    set_payload: dict[str, Any] = {
        "updated_at": _now_iso(),
        "updated_by": actor,
        "reason": reason or "(no reason given)",
    }
    for lane, enabled in lanes.items():
        set_payload[lane] = bool(enabled)
    await db["runtime_flags"].update_one(
        {"_id": _LANE_ENABLED_DOC_ID},
        {"$set": set_payload, "$setOnInsert": {"created_at": _now_iso()}},
        upsert=True,
    )
    return await _get_lane_enabled_state()


@router.get("/arm/status")
async def arm_status(_user: dict = Depends(get_current_user)) -> dict:
    """Combined view of ALL master + per-lane switch layers so the
    operator dashboard can render a single truthful indicator.

    Returns:
        env_broker_live      — env `BROKER_LIVE_ORDER_ENABLED`
        env_auto_router      — env `AUTO_ROUTER_ENABLED`
        mc_switch            — `trading_controls.enabled` (auto_router path)
        trader_switch        — `runtime_flags.master_trading_switch.enabled`
        lanes                — {equity, crypto} per-lane enabled bools
                                (default True when doc/field missing)
        all_armed            — ALL master layers True AND at least one lane on
        will_fire            — subset the auto_router actually consults
                                (env_auto_router && mc_switch)
        trader_will_fire     — subset the trader sidecar consults
                                (trader_switch)
    """
    import os
    mc = await get_trading_status()
    tr = await _get_trader_switch_state()
    ln = await _get_lane_enabled_state()
    env_ar = os.environ.get("AUTO_ROUTER_ENABLED", "true").lower() == "true"
    env_bl = os.environ.get("BROKER_LIVE_ORDER_ENABLED", "false").lower() == "true"
    mc_on = bool(mc.get("enabled"))
    tr_on = bool(tr.get("enabled"))

    lane_states = {lane: _lane_from_doc(ln, lane) for lane in _KNOWN_LANES}
    any_lane_on = any(lane_states.values())

    # 2026-07-09 sidecar-decommission doctrine: `TRADER_ENABLED`
    # decides whether the `/app/trader` sidecar's broker adapter
    # will actually submit orders (true) or short-circuit as
    # shadow (false). Surfaced here so the operator dashboard shows
    # the one-broker-door truth in the same tile as the arm state.
    import os as _os_te
    trader_authoritative = (
        _os_te.environ.get("TRADER_ENABLED", "false").lower().strip()
        in {"1", "true", "yes", "on"}
    )

    # 2026-07-09 crypto-creds probe: since MC is the sole broker
    # door but Kraken keys historically lived in `KRAKEN_API_KEY`
    # env vars (trader-sidecar pattern) not the encrypted Mongo
    # singleton, the operator needs to see at a glance whether the
    # crypto lane actually has creds to submit with. This runs the
    # same resolver the auto_router uses (Mongo → env fallback), so
    # the answer here matches what a real order would see.
    crypto_broker_ready = False
    crypto_creds_source = None
    crypto_creds_detail = None
    try:
        from shared.crypto.kraken import get_active_keys_status  # noqa: WPS433
        _kst = await get_active_keys_status()
        crypto_broker_ready = _kst.get("state") == "ok"
        crypto_creds_source = _kst.get("source")
        crypto_creds_detail = _kst.get("detail")
    except Exception as _exc:  # noqa: BLE001
        crypto_creds_detail = f"probe failed: {type(_exc).__name__}"

    return {
        "ok": True,
        "env_broker_live": env_bl,
        "env_auto_router": env_ar,
        "trader_authoritative": trader_authoritative,
        "broker_door_owner": (
            "sidecar_and_mc_both" if trader_authoritative else "mc_only"
        ),
        "crypto_broker_ready": crypto_broker_ready,
        "crypto_creds_source": crypto_creds_source,
        "crypto_creds_detail": crypto_creds_detail,
        "mc_switch": {
            "enabled": mc_on,
            "reason": mc.get("reason"),
            "updated_at": mc.get("updated_at"),
            "updated_by": mc.get("updated_by"),
        },
        "trader_switch": {
            "enabled": tr_on,
            "reason": tr.get("reason"),
            "updated_at": tr.get("updated_at"),
            "updated_by": tr.get("updated_by"),
        },
        "lanes": {
            "equity": {
                "enabled": lane_states["equity"],
                "is_default": ln.get("equity") is None,
            },
            "crypto": {
                "enabled": lane_states["crypto"],
                "is_default": ln.get("crypto") is None,
            },
            "reason": ln.get("reason"),
            "updated_at": ln.get("updated_at"),
            "updated_by": ln.get("updated_by"),
        },
        # Boolean summaries the frontend renders in the arm indicator:
        "will_fire": env_ar and mc_on,
        "trader_will_fire": tr_on,
        "all_armed": (
            env_ar and env_bl and mc_on and tr_on and any_lane_on
        ),
    }


@router.post("/arm")
async def arm(
    body: ArmIn, user: dict = Depends(get_current_user),
) -> dict:
    """Unified arm/disarm — flips BOTH master Mongo gates in ONE call
    so the trader can't be left half-armed. Optionally accepts a
    `lanes` map to update per-lane enablement in the same request.

    Enabling requires a reason (audit hygiene). All writes share the
    same reason + timestamp + actor. A single audit row is written
    to `trading_controls_audit` with `source="unified_arm"` so the
    per-doc history and the unified history live in the same table.

    Example:
        POST /admin/trading/arm
        {
          "enabled": true,
          "reason": "morning session start",
          "lanes": {"equity": true, "crypto": false}
        }
    """
    actor = (user or {}).get("email") or "operator"
    if body.enabled and not body.reason.strip():
        raise HTTPException(
            status_code=400,
            detail="reason required when arming trading",
        )
    # Validate lane keys if provided.
    if body.lanes:
        bad = set(body.lanes.keys()) - set(_KNOWN_LANES)
        if bad:
            raise HTTPException(
                status_code=400,
                detail=f"unknown lane(s): {sorted(bad)}. "
                       f"Known lanes: {list(_KNOWN_LANES)}",
            )

    # Snapshot pre-state for audit traceability.
    mc_pre = await get_trading_status()
    tr_pre = await _get_trader_switch_state()
    ln_pre = await _get_lane_enabled_state()

    # 1. Flip MC-path switch.
    mc_new = await set_trading_enabled(body.enabled, body.reason, actor)

    # 2026-02-19: invalidate the auto_router's arm cache so the flip
    # takes effect on the NEXT tick, not on the next TTL expiry.
    try:
        from shared.auto_router import _invalidate_arm_cache  # noqa: WPS433
        _invalidate_arm_cache()
    except Exception:  # noqa: BLE001
        pass

    # 2. Flip trader-sidecar switch. Same-shape write as the MC path;
    #    the trader's state refresher reads this within 60s.
    tr_payload = {
        "enabled": bool(body.enabled),
        "reason": body.reason or "(no reason given)",
        "updated_at": _now_iso(),
        "updated_by": actor,
    }
    await db["runtime_flags"].update_one(
        {"_id": _TRADER_SWITCH_DOC_ID},
        {"$set": tr_payload, "$setOnInsert": {"created_at": _now_iso()}},
        upsert=True,
    )

    # 3. Optional per-lane update (only if the operator specified it).
    ln_new = ln_pre
    if body.lanes:
        ln_new = await _set_lane_enabled(body.lanes, body.reason, actor)

    # 4. Unified audit row — one receipt for the whole flip.
    await db[f"{COLLECTION}_audit"].insert_one({
        "source": "unified_arm",
        "enabled": bool(body.enabled),
        "reason": body.reason or "(no reason given)",
        "updated_at": _now_iso(),
        "updated_by": actor,
        "ts": _now_iso(),
        "pre_state": {
            "mc_switch": bool(mc_pre.get("enabled")),
            "trader_switch": bool(tr_pre.get("enabled")),
            "lanes": {
                lane: _lane_from_doc(ln_pre, lane) for lane in _KNOWN_LANES
            },
        },
        "post_state": {
            "mc_switch": bool(mc_new.get("enabled")),
            "trader_switch": bool(body.enabled),
            "lanes": {
                lane: _lane_from_doc(ln_new, lane) for lane in _KNOWN_LANES
            },
        },
        "lanes_updated": (
            list(body.lanes.keys()) if body.lanes else []
        ),
    })

    logger.warning(
        "unified_arm FLIPPED: enabled=%s by=%s reason=%r "
        "lanes=%s pre(mc=%s,trader=%s)",
        body.enabled, actor, body.reason,
        body.lanes,
        bool(mc_pre.get("enabled")), bool(tr_pre.get("enabled")),
    )

    return await arm_status(_user=user)


@router.post("/lane")
async def toggle_lane(
    body: LaneToggleIn, user: dict = Depends(get_current_user),
) -> dict:
    """Toggle a single lane's enabled state without touching the
    master arm. Fine-grained control — the operator can keep the
    master switches ON and independently disable, say, crypto for
    a maintenance window.

    Enabling a lane requires a reason. Disabling does not — same
    doctrine as the master arm (kill actions never blocked on
    audit-field bureaucracy).
    """
    actor = (user or {}).get("email") or "operator"
    lane = (body.lane or "").lower().strip()
    if lane not in _KNOWN_LANES:
        raise HTTPException(
            status_code=400,
            detail=f"unknown lane: {body.lane!r}. "
                   f"Known lanes: {list(_KNOWN_LANES)}",
        )
    if body.enabled and not body.reason.strip():
        raise HTTPException(
            status_code=400,
            detail="reason required when enabling a lane",
        )

    ln_pre = await _get_lane_enabled_state()
    pre_enabled = _lane_from_doc(ln_pre, lane)

    ln_new = await _set_lane_enabled({lane: body.enabled}, body.reason, actor)

    # Audit row — lane-toggle scoped, still in the unified audit tape.
    await db[f"{COLLECTION}_audit"].insert_one({
        "source": "lane_toggle",
        "lane": lane,
        "enabled": bool(body.enabled),
        "reason": body.reason or "(no reason given)",
        "updated_at": _now_iso(),
        "updated_by": actor,
        "ts": _now_iso(),
        "pre_state": {"lane": lane, "enabled": pre_enabled},
        "post_state": {
            "lane": lane,
            "enabled": _lane_from_doc(ln_new, lane),
        },
    })

    logger.warning(
        "lane_toggle FLIPPED: lane=%s enabled=%s by=%s reason=%r "
        "pre=%s",
        lane, body.enabled, actor, body.reason, pre_enabled,
    )

    return await arm_status(_user=user)

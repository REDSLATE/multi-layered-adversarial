"""Auto-submit policy engine — Phase 1 of the governance throughput
unlock (2026-02-19).

Operator pain point: 4,604 intents/day, 0 submitted. The system was
architected requiring manual operator click on every intent. At ~3
intents/minute the operator cannot keep up — execution rate is 0%.

The fix is NOT to bypass the gate chain; it's to AUTO-CLICK SUBMIT
on intents that meet a conservative checklist. Every gate still
runs. Every audit row is still written. The operator just doesn't
have to be physically present to advance the funnel.

Tier 1 (conservative) is the default opt-in: equity spot-long
intents at confidence ≥ 0.85, notional ≤ tier cap (capped by the
per-order cap anyway), dry-run already passed.

Toggleable via env var `RISEDUAL_AUTO_SUBMIT_TIER_1_ENABLED` (boot
default OFF — operator opts in deliberately) and via the admin
endpoint `POST /api/admin/auto-submit/policy` (runtime override).
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("risedual.auto_submit_policy")

# ── Tier 1 defaults — conservative by design ─────────────────────────
TIER_1_DEFAULTS: dict[str, Any] = {
    "tier_name": "tier_1_conservative",
    "confidence_min": 0.85,
    "notional_default_usd": 5.0,        # the brain's preferred size for a tier-1 auto-trade
    "notional_max_usd": 5000.0,         # absolute ceiling; per-order cap still applies below this
    "allowed_lanes": ["equity"],        # NO crypto in tier 1 — pilot equity first
    "allowed_actions": ["BUY"],         # spot_long only
    "allowed_brains": ["alpha", "camaro", "chevelle", "redeye"],
    "required_dry_run_state": "passed",
}


# ── Runtime override (toggled via admin endpoint) ────────────────────
_POLICY_LOCK = threading.Lock()
_POLICY_OVERRIDE: dict[str, Any] = {}


def _env_enabled() -> bool:
    raw = os.environ.get("RISEDUAL_AUTO_SUBMIT_TIER_1_ENABLED", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def get_policy() -> dict[str, Any]:
    """Current effective policy = defaults + runtime overrides."""
    with _POLICY_LOCK:
        ovr = dict(_POLICY_OVERRIDE)
    enabled = ovr.get("enabled")
    if enabled is None:
        enabled = _env_enabled()
    return {
        "enabled": bool(enabled),
        "source": (
            "runtime_override" if ovr.get("enabled") is not None
            else ("env" if _env_enabled() else "default_off")
        ),
        **TIER_1_DEFAULTS,
        **{k: v for k, v in ovr.items() if k != "enabled"},
    }


def set_policy(enabled: bool, **overrides: Any) -> dict[str, Any]:
    """Operator API — flip the enabled flag + optionally override any
    field. `overrides` must be a subset of TIER_1_DEFAULTS keys."""
    with _POLICY_LOCK:
        _POLICY_OVERRIDE.clear()
        _POLICY_OVERRIDE["enabled"] = bool(enabled)
        for k, v in overrides.items():
            if k in TIER_1_DEFAULTS:
                _POLICY_OVERRIDE[k] = v
    return get_policy()


def reset_policy_for_tests() -> None:
    with _POLICY_LOCK:
        _POLICY_OVERRIDE.clear()


def matches_tier_1(intent: dict, policy: dict | None = None) -> tuple[bool, str]:
    """Returns (matches, reason). Reason describes the first failing
    criterion when matches=False so the audit trail can show WHY a
    given intent didn't auto-submit."""
    p = policy or get_policy()
    if not p["enabled"]:
        return False, "auto_submit_policy_disabled"
    action = (intent.get("action") or "").upper()
    if action not in p["allowed_actions"]:
        return False, f"action {action!r} not in allowed {p['allowed_actions']}"
    lane = (intent.get("lane") or "").lower()
    if lane not in p["allowed_lanes"]:
        return False, f"lane {lane!r} not in allowed {p['allowed_lanes']}"
    brain = (intent.get("stack") or "").lower()
    if brain not in p["allowed_brains"]:
        return False, f"brain {brain!r} not in allowed {p['allowed_brains']}"
    conf = float(intent.get("confidence") or 0.0)
    if conf < p["confidence_min"]:
        return False, f"confidence {conf:.3f} < tier min {p['confidence_min']}"
    state = (intent.get("dry_run_state") or "").lower()
    if state != p["required_dry_run_state"]:
        return False, (
            f"dry_run_state {state!r} != required "
            f"{p['required_dry_run_state']!r}"
        )
    if intent.get("executed"):
        return False, "intent already executed"
    return True, "ok"


def chosen_notional(intent: dict, per_order_cap: float | None = None) -> float:
    """Tier-1 notional = min(brain-preferred, tier ceiling, broker cap).

    `per_order_cap` is the lane's `cap_per_order_lane` from the live
    caps snapshot. We never auto-submit above the operator's hard
    money safety, so the cap dominates the choice in practice (e.g.
    $10 per Webull order)."""
    p = get_policy()
    candidates = [p["notional_default_usd"], p["notional_max_usd"]]
    brain_preferred = intent.get("preferred_notional_usd")
    if brain_preferred is not None:
        try:
            candidates.append(float(brain_preferred))
        except (TypeError, ValueError):
            pass
    if per_order_cap is not None and per_order_cap > 0:
        candidates.append(float(per_order_cap))
    return max(0.01, min(candidates))


async def maybe_auto_submit(intent_id: str) -> dict[str, Any] | None:
    """Called by the dry-run finalizer when an intent transitions to
    dry_run_state=passed. Returns the submit result on auto-submit,
    None when the intent doesn't qualify.

    Doctrine: this function does NOT bypass any gate — it just calls
    the existing `execution_submit` path with a synthesized operator
    identity ("auto_submit_tier_1"). All gates run, all audit rows
    are written, the receipt clearly stamps that this was machine-
    advanced rather than operator-clicked.
    """
    from db import db  # noqa: WPS433
    from namespaces import SHARED_INTENTS  # noqa: WPS433

    intent = await db[SHARED_INTENTS].find_one(
        {"intent_id": intent_id}, {"_id": 0},
    )
    if not intent:
        return None

    policy = get_policy()
    ok, reason = matches_tier_1(intent, policy)
    if not ok:
        logger.debug("auto_submit skip %s: %s", intent_id, reason)
        return None

    # Resolve the lane's per-order cap so we don't try to submit
    # above what the caps gate would reject.
    cap = None
    try:
        from shared.risk_caps import caps_snapshot  # noqa: WPS433
        caps = caps_snapshot()
        lane = intent.get("lane") or "equity"
        lane_caps = (caps.get("lanes") or {}).get(lane) or {}
        cap = lane_caps.get("per_order_usd") or caps.get("per_order_usd")
    except Exception:  # noqa: BLE001
        pass

    notional = chosen_notional(intent, cap)

    # Build the submit body and route through the same endpoint the
    # operator's SUBMIT button uses. The "executed_by" on the receipt
    # will show 'auto_submit_tier_1' so the audit feed shows machine-
    # advanced trades distinctly.
    from shared.execution import execution_submit, SubmitBody  # noqa: WPS433

    submit_body = SubmitBody(
        intent_id=intent_id,
        order_notional_usd=notional,
        confirm="execute",
        operator_override=False,
        override_reason="",
        action_override=None,
    )
    auto_user = {
        "email": "auto_submit_tier_1@risedual.io",
        "auto_submit": True,
    }

    try:
        result = await execution_submit(submit_body, user=auto_user)
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "auto_submit failed for intent %s: %s", intent_id, e,
        )
        # Audit the failure so the post-mortem panel surfaces it.
        from namespaces import SHARED_GATE_RESULTS  # noqa: WPS433
        await db[SHARED_GATE_RESULTS].insert_one({
            "intent_id": intent_id,
            "kind": "auto_submit_failed",
            "ts": datetime.now(timezone.utc).isoformat(),
            "by": "auto_submit_tier_1",
            "reason": str(e)[:500],
            "tier": "tier_1_conservative",
        })
        return None

    logger.info(
        "auto_submit OK intent=%s brain=%s symbol=%s notional=%.2f",
        intent_id, intent.get("stack"), intent.get("symbol"), notional,
    )
    return result

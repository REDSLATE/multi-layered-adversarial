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
import asyncio
import os
import threading
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("risedual.auto_submit_policy")

# ── Tier 1 defaults — operator-driven (2026-02-19 update) ────────────
# Original "tier_1_conservative" was BUY+equity only. Operator directive
# (2026-02-19): "I'm not reviewing, it should be handled by Shelly and
# filed." → broaden the doctrine surface so when the toggle is flipped
# on, Shelly catches every intent the brains emit, regardless of action
# or lane. Risk controls remain:
#   - confidence_min        (≥ 0.85 — high-confidence floor)
#   - notional_max_usd      (≤ $5,000 absolute ceiling per order)
#   - required_dry_run_state (intent must have passed all gates first)
# `enabled` STILL defaults to False — flipping ON is an explicit
# operator action via /api/admin/auto-submit/policy (signed audit
# trail with a typed reason).
TIER_1_DEFAULTS: dict[str, Any] = {
    "tier_name": "tier_1_conservative",
    # 2026-02-20: operator has been hand-flipping 0.85 → 0.70 on every
    # prod deploy. Bake the operator's preferred default into code so
    # disarm-then-arm cycles don't reset to the conservative 0.85.
    # Brain emits below 0.70 are typically noise; 0.85 was suppressing
    # actionable signals.
    "confidence_min": 0.70,
    # 2026-02-20: notional default lifted $5 → $10. Webull cap is now
    # buying-power-scaled (5% of BP), so $10 routinely fits inside the
    # dynamic ceiling on any funded account. The brain's
    # preferred_notional_usd still takes priority via `chosen_notional`
    # — this only affects intents where the brain didn't size.
    "notional_default_usd": 10.0,
    "notional_max_usd": 5000.0,                        # absolute hard cap
    "allowed_lanes": ["equity", "crypto"],             # both lanes
    "allowed_actions": ["BUY", "SELL"],                # both directions
    "allowed_brains": ["alpha", "camaro", "chevelle", "redeye"],
    "required_dry_run_state": "passed",
}


# ── Runtime override (toggled via admin endpoint) ────────────────────
#
# Persistence story (2026-02-19, post-incident):
#   The override was originally a process-local dict. Production
#   operator reported "I flipped the toggle and nothing happened" —
#   investigation showed every K8s pod restart silently reset the
#   override back to default-off, because there was NO persistence.
#
#   Fix: writes go to Mongo (`shared_auto_submit_policy_state`,
#   singleton doc keyed by `_id="singleton"`). On first access we
#   lazy-hydrate the in-memory cache from Mongo so subsequent calls
#   stay cheap. `set_policy_async` is the persisting entrypoint
#   (called by the admin route). `set_policy` (sync) is retained for
#   tests that don't need persistence.
_POLICY_LOCK = threading.Lock()
_POLICY_OVERRIDE: dict[str, Any] = {}
_HYDRATED: bool = False  # set True after first Mongo load

POLICY_STATE_COLL = "shared_auto_submit_policy_state"
POLICY_STATE_DOC_ID = "singleton"


def _env_enabled() -> bool:
    raw = os.environ.get("RISEDUAL_AUTO_SUBMIT_TIER_1_ENABLED", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


async def hydrate_from_mongo() -> dict[str, Any]:
    """Load the persisted policy override from Mongo into the
    in-memory cache. Called once at app startup (lifespan hook) and
    also lazily on first access if the startup hook didn't run
    (tests, scripts, etc).

    Idempotent — safe to call multiple times. Returns the resulting
    effective policy.
    """
    global _HYDRATED
    try:
        from db import db  # late import — module is loaded before db in some tests
        doc = await db[POLICY_STATE_COLL].find_one({"_id": POLICY_STATE_DOC_ID})
    except Exception as e:  # noqa: BLE001
        logger.warning("auto_submit_policy: hydrate_from_mongo failed: %s", e)
        with _POLICY_LOCK:
            _HYDRATED = True  # don't keep retrying — env/default fallback is fine
        return get_policy()
    with _POLICY_LOCK:
        _POLICY_OVERRIDE.clear()
        if doc:
            # `_id` is the doc key; the rest is the override payload.
            for k, v in doc.items():
                if k == "_id":
                    continue
                if k == "enabled" or k in TIER_1_DEFAULTS:
                    _POLICY_OVERRIDE[k] = v
        _HYDRATED = True
    p = get_policy()
    logger.info(
        "auto_submit_policy: hydrated from Mongo · enabled=%s · source=%s",
        p["enabled"], p["source"],
    )
    return p


async def _persist_to_mongo(override: dict[str, Any]) -> None:
    """Upsert the override dict to Mongo. Called from set_policy_async."""
    from db import db
    payload = {**override, "updated_at": datetime.now(timezone.utc).isoformat()}
    await db[POLICY_STATE_COLL].update_one(
        {"_id": POLICY_STATE_DOC_ID},
        {"$set": payload},
        upsert=True,
    )


def get_policy() -> dict[str, Any]:
    """Current effective policy = defaults + runtime overrides.

    Pure read; no Mongo I/O. Callers that need a fresh hydrate
    (post-restart cold start) should `await hydrate_from_mongo()`
    first — the admin route + lifespan hook do this.
    """
    with _POLICY_LOCK:
        ovr = dict(_POLICY_OVERRIDE)
    enabled = ovr.get("enabled")
    if enabled is None:
        enabled = _env_enabled()
    src = (
        "runtime_override" if ovr.get("enabled") is not None
        else ("env" if _env_enabled() else "default_off")
    )
    return {
        "enabled": bool(enabled),
        "source": src,
        **TIER_1_DEFAULTS,
        **{k: v for k, v in ovr.items() if k != "enabled"},
    }


async def set_policy_async(enabled: bool, **overrides: Any) -> dict[str, Any]:
    """Operator API — persist enabled flag + optional overrides to
    Mongo AND update the in-memory cache. This is the entrypoint the
    admin route MUST use so the toggle survives pod restarts.

    `overrides` must be a subset of TIER_1_DEFAULTS keys.
    """
    with _POLICY_LOCK:
        _POLICY_OVERRIDE.clear()
        _POLICY_OVERRIDE["enabled"] = bool(enabled)
        for k, v in overrides.items():
            if k in TIER_1_DEFAULTS:
                _POLICY_OVERRIDE[k] = v
        snapshot = dict(_POLICY_OVERRIDE)
    try:
        await _persist_to_mongo(snapshot)
    except Exception as e:  # noqa: BLE001
        # Persistence failure is loud but does not roll back the
        # in-memory flip — operator's UI click took effect for this
        # process. A pod restart would lose it; logged so monitoring
        # can catch it.
        logger.error(
            "auto_submit_policy: set_policy_async PERSIST FAILED — "
            "in-memory flip will NOT survive pod restart: %s", e,
        )
    return get_policy()


def set_policy(enabled: bool, **overrides: Any) -> dict[str, Any]:
    """SYNC variant — in-memory ONLY, does NOT persist. Retained for
    tests and direct callers that explicitly don't want Mongo I/O.
    Operator-facing toggles MUST use set_policy_async.
    """
    with _POLICY_LOCK:
        _POLICY_OVERRIDE.clear()
        _POLICY_OVERRIDE["enabled"] = bool(enabled)
        for k, v in overrides.items():
            if k in TIER_1_DEFAULTS:
                _POLICY_OVERRIDE[k] = v
    return get_policy()


def reset_policy_for_tests() -> None:
    global _HYDRATED
    with _POLICY_LOCK:
        _POLICY_OVERRIDE.clear()
        _HYDRATED = False


# Skip-reason categories surfaced in the post-mortem outcome strip.
# Keys are stable (UI / tests depend on them); values are the broad
# bucket labels the operator scans at a glance.
SKIP_CATEGORY_HOLD              = "hold_action"           # action=HOLD — by design
SKIP_CATEGORY_DISABLED          = "policy_disabled"       # Shelly is OFF
SKIP_CATEGORY_LOW_CONFIDENCE    = "low_confidence"        # < confidence_min
SKIP_CATEGORY_LANE_FILTERED     = "lane_filtered"         # lane not in allowed list
SKIP_CATEGORY_BRAIN_FILTERED    = "brain_filtered"        # brain not in allowed list
SKIP_CATEGORY_ACTION_FILTERED   = "action_filtered"       # action not BUY/SELL/HOLD
SKIP_CATEGORY_DRY_RUN_NOT_READY = "dry_run_not_ready"     # dry_run not passed yet
SKIP_CATEGORY_ALREADY_EXECUTED  = "already_executed"      # raced ourselves
SKIP_CATEGORY_AFTER_HOURS       = "equity_after_hours"    # equity intent outside US RTH
SKIP_CATEGORY_OTHER             = "other"                 # anything not classified
SKIP_CATEGORY_NOT_FOUND         = "intent_not_found"      # intent_id missing at auto-submit time (DB race or rogue caller)
SKIP_CATEGORY_INTERNAL_ERROR    = "internal_error"        # exception in chain before audit could be written


def _categorize_skip(reason: str) -> str:
    """Bucket a `matches_tier_1` skip reason into a coarse category for
    the post-mortem panel. Kept simple — exact-string match against
    the well-known reason prefixes in `matches_tier_1`."""
    r = reason or ""
    if r == "auto_submit_policy_disabled":
        return SKIP_CATEGORY_DISABLED
    if r.startswith("action "):
        # "action 'HOLD' not in allowed [...]" is the most common skip
        # — call HOLD out specifically so the operator can see
        # "Shelly intentionally skipped 3500 HOLD signals" at a glance.
        if "'HOLD'" in r:
            return SKIP_CATEGORY_HOLD
        return SKIP_CATEGORY_ACTION_FILTERED
    if r.startswith("lane "):
        return SKIP_CATEGORY_LANE_FILTERED
    if r.startswith("brain "):
        return SKIP_CATEGORY_BRAIN_FILTERED
    if r.startswith("confidence "):
        return SKIP_CATEGORY_LOW_CONFIDENCE
    if r.startswith("dry_run_state "):
        return SKIP_CATEGORY_DRY_RUN_NOT_READY
    if r == "intent already executed":
        return SKIP_CATEGORY_ALREADY_EXECUTED
    if r.startswith("equity_after_hours"):
        return SKIP_CATEGORY_AFTER_HOURS
    return SKIP_CATEGORY_OTHER




def _normalize_brain_to_stack(raw: str) -> str:
    """Normalize a brain identifier to its canonical stack code.

    Intents may carry the brain identity in three forms across the
    codebase's rename in flight:
      * stack code  : alpha | camaro | chevelle | redeye  (legacy wire)
      * brain_id    : camino | barracuda | hellcat | gto  (canonical)
      * display name: Camino | Barracuda | Hellcat | GTO  (UI)

    All three normalize to the stack code here so the `allowed_brains`
    list (still keyed on stack codes for backwards compatibility) can
    match any of them. Unknown identifiers are returned lowercased
    unchanged so the audit reason carries the original token.
    """
    key = (raw or "").lower().strip()
    if not key:
        return key
    # Already a stack code → done.
    if key in {"alpha", "camaro", "chevelle", "redeye"}:
        return key
    # brain_id or display_name → resolve via brain_doctrine.
    try:
        from shared.brain_doctrine import BRAIN_ID_TO_STACK  # noqa: WPS433
        if key in BRAIN_ID_TO_STACK:
            return BRAIN_ID_TO_STACK[key]
    except Exception:  # noqa: BLE001
        pass
    return key


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
    # 2026-02-20: Equity-only market-hours gate. Webull 417s any
    # equity order placed outside US RTH; we hold those intents
    # rather than waste an MC receipt + API call + post-mortem row.
    # Crypto trades 24/7 on Kraken, so this gate is equity-only.
    if lane == "equity":
        from shared.market_hours import is_equity_rth, market_hours_reason  # noqa: WPS433
        if not is_equity_rth():
            return False, market_hours_reason()
    # 2026-02-20: brain name normalization. Intents may arrive with
    # the legacy stack code (alpha/camaro/chevelle/redeye), the
    # canonical brain_id (camino/barracuda/hellcat/gto), or the UI
    # display name (Camino/Barracuda/Hellcat/GTO). Normalize to the
    # stack code so a brain rename or display-name drift doesn't
    # silently filter every intent from that brain.
    raw_brain = (intent.get("stack") or "").lower().strip()
    brain = _normalize_brain_to_stack(raw_brain)
    if brain not in p["allowed_brains"]:
        return False, (
            f"brain {raw_brain!r} (normalized {brain!r}) not in allowed "
            f"{p['allowed_brains']}"
        )
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

    Audit-completeness contract (2026-02-20 operator directive — "no
    silent execution-path leak"):
      Every call to this function MUST produce exactly one terminal
      gate-result row keyed by intent_id. Branches & their `kind`:

        intent_id missing in DB    → auto_submit_skipped  (skip_category=intent_not_found)
        Shelly filter says NO      → auto_submit_skipped  (skip_category=<reason>)
        execution_submit raised    → auto_submit_failed   (skip_category=submit_raised)
        execution_submit returned  → auto_submit_submitted (one of: passed/blocked/no_trade)
            None or fall-through   → auto_submit_failed   (skip_category=execution_path_leak)
        Unexpected exception       → auto_submit_exception (kind, re-raised to caller)

      The aggregator in admin_intents_post_mortem.py uses the latest
      row per intent — the chain's outer catch-all may write a second
      row but the inner one's `phase` field helps localize the bug.
    """
    from db import db  # noqa: WPS433
    from namespaces import SHARED_INTENTS, SHARED_GATE_RESULTS  # noqa: WPS433

    async def _write(payload: dict[str, Any]) -> None:
        """Write a terminal audit row. Wrapped so a DB hiccup never
        nukes the caller — the row is best-effort, but if even this
        fails the outer exception wrapper will still produce a row."""
        try:
            await db[SHARED_GATE_RESULTS].insert_one({
                "intent_id": intent_id,
                "ts": datetime.now(timezone.utc).isoformat(),
                "by": "auto_submit_tier_1",
                **payload,
            })
        except Exception as audit_err:  # noqa: BLE001
            logger.error(
                "auto_submit terminal audit write FAILED intent=%s payload=%s err=%s",
                intent_id, payload.get("kind"), audit_err,
            )

    try:
        intent = await db[SHARED_INTENTS].find_one(
            {"intent_id": intent_id}, {"_id": 0},
        )
        if not intent:
            await _write({
                "kind": "auto_submit_skipped",
                "reason": "intent not found in shared_intents at auto-submit time",
                "skip_category": "intent_not_found",
            })
            return None

        policy = get_policy()
        ok, reason = matches_tier_1(intent, policy)
        if not ok:
            logger.debug("auto_submit skip %s: %s", intent_id, reason)
            await _write({
                "kind": "auto_submit_skipped",
                "reason": reason,
                "tier": policy.get("tier_name", "tier_1_conservative"),
                "skip_category": _categorize_skip(reason),
            })
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
                "auto_submit submit-stage raised intent=%s err=%s", intent_id, e,
            )
            await _write({
                "kind": "auto_submit_failed",
                "reason": str(e)[:500],
                "tier": "tier_1_conservative",
                "skip_category": "submit_raised",
            })
            return None

        # ─── Explicit "eligible but no submit path" guard ──────────
        # If execution_submit returned None or fell through without a
        # verdict, the intent silently disappeared. Capture it.
        if result is None:
            await _write({
                "kind": "auto_submit_failed",
                "reason": "eligible_but_no_submit_path",
                "skip_category": "execution_path_leak",
                "tier": "tier_1_conservative",
                "intent_notional_usd": notional,
            })
            return None

        # ─── Success terminal row ──────────────────────────────────
        # execution_submit writes its own gate row (`submit_passed` /
        # `submit_blocked` / `submit_no_trade`). We ALSO write an
        # `auto_submit_submitted` row so the post-mortem can account
        # the auto-submit chain's outcome explicitly: did Shelly hand
        # off and what did the broker layer return?
        verdict = result.get("verdict") if isinstance(result, dict) else None
        await _write({
            "kind": "auto_submit_submitted",
            "tier": "tier_1_conservative",
            "intent_notional_usd": notional,
            "submit_verdict": verdict,
            "executed": bool(isinstance(result, dict) and result.get("executed")),
        })
        logger.info(
            "auto_submit OK intent=%s brain=%s symbol=%s notional=%.2f verdict=%s",
            intent_id, intent.get("stack"), intent.get("symbol"), notional, verdict,
        )
        return result

    except Exception as e:  # noqa: BLE001
        # Catch-ALL: any unexpected exception in the body that wasn't
        # mapped to a terminal row above. Operator directive: re-raise
        # AFTER writing the row so the chain's outer try/except can
        # also see + log the failure.
        logger.error(
            "auto_submit unexpected exception intent=%s err=%r", intent_id, e,
        )
        await _write({
            "kind": "auto_submit_exception",
            "reason": repr(e)[:500],
            "skip_category": "internal_error",
        })
        raise

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
    "allowed_brains": ["camino", "barracuda", "hellcat", "gto"],
    "required_dry_run_state": "passed",
}


# Tier 2 — aggressive preset (2026-06-22, operator pin):
#
# > "Keep Tier 1 as the known stable baseline. Add Tier 2 as a
# > clearly labeled operator choice. That gives you Conservative =
# > stable, Aggressive = deliberate switch — rather than silently
# > turning Tier 1 into something it was not designed to be."
#
# Doctrine: Tier 2 is the SAME shape as Tier 1 (same allowed
# lanes / actions / brains, dry_run must still pass) — only the
# emit-floor and default size loosen. Every other rail stays:
#   • daily exposure cap
#   • RoadGuard (incl. live spread-quality guard)
#   • Webull close buffer
#   • dry_run_state = passed
#   • notional_max_usd hard cap
#
# Switching is a DELIBERATE operator click with a typed reason —
# audit row records the tier transition, not just the parameter
# delta. See `routes/admin_auto_submit.py::policy_toggle`.
TIER_2_AGGRESSIVE: dict[str, Any] = {
    "tier_name": "tier_2_aggressive",
    "confidence_min": 0.45,
    "notional_default_usd": 25.0,
    "notional_max_usd": 5000.0,                        # hard cap unchanged
    "allowed_lanes": ["equity", "crypto"],             # same as Tier 1
    "allowed_actions": ["BUY", "SELL"],                # same as Tier 1
    "allowed_brains": ["camino", "barracuda", "hellcat", "gto"],
    "required_dry_run_state": "passed",                # rail preserved
}


# Tier registry — single source of truth for the admin route, the
# UI dropdown, and the regression test. Keys are stable strings the
# operator sees on the dashboard; do NOT rename without a migration
# (audit rows reference these strings).
TIER_REGISTRY: dict[str, dict[str, Any]] = {
    "tier_1_conservative": TIER_1_DEFAULTS,
    "tier_2_aggressive":   TIER_2_AGGRESSIVE,
}


def get_tier_defaults(tier_name: str) -> dict[str, Any]:
    """Lookup a tier preset by name. Raises ValueError on unknown
    tier — defensive so the admin route can return a clean 400
    instead of silently falling through to Tier 1.
    """
    if tier_name not in TIER_REGISTRY:
        raise ValueError(
            f"unknown tier_name={tier_name!r}; valid: "
            f"{sorted(TIER_REGISTRY.keys())!r}"
        )
    return dict(TIER_REGISTRY[tier_name])


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


async def set_policy_async(
    enabled: bool,
    tier_name: str | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """Operator API — persist enabled flag + optional overrides to
    Mongo AND update the in-memory cache. This is the entrypoint the
    admin route MUST use so the toggle survives pod restarts.

    `tier_name` (2026-06-22): when supplied, loads the named tier's
    preset values into the override before applying the explicit
    `overrides` kwargs. This lets the operator click "switch to
    tier_2_aggressive" in the UI and get every field flipped in one
    atomic write. Unknown tier_name raises ValueError (admin route
    converts to HTTP 400).

    `overrides` must be a subset of TIER_1_DEFAULTS keys (the
    schema is shared across tiers).
    """
    # Resolve the tier preset first (if any) so explicit overrides
    # can fine-tune ON TOP of it. Order: tier preset → explicit
    # overrides → in-memory dict.
    preset_overrides: dict[str, Any] = {}
    if tier_name is not None:
        preset = get_tier_defaults(tier_name)
        for k, v in preset.items():
            if k in TIER_1_DEFAULTS:
                preset_overrides[k] = v
    with _POLICY_LOCK:
        _POLICY_OVERRIDE.clear()
        _POLICY_OVERRIDE["enabled"] = bool(enabled)
        for k, v in preset_overrides.items():
            _POLICY_OVERRIDE[k] = v
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
# 2026-06-19 — was a single `dry_run_not_ready` bucket; split into
# three so the operator can tell "gate chain blocked it" (doctrine-
# correct rejection) apart from "dry-run pending" (benign race) and
# "dry-run missing" (actual silent leak — needs investigation). The
# old label is kept for backwards compatibility with stored audit
# rows; new rows use the precise three.
SKIP_CATEGORY_DRY_RUN_NOT_READY = "dry_run_not_ready"     # (legacy, kept for old audit rows)
SKIP_CATEGORY_DRY_RUN_BLOCKED   = "dry_run_blocked"       # dry-run completed with verdict=blocked (correct rejection)
SKIP_CATEGORY_DRY_RUN_PENDING   = "dry_run_pending"       # dry-run task running; benign race — auto-resolves
SKIP_CATEGORY_DRY_RUN_MISSING   = "dry_run_missing"       # dry_run_state never set — silent leak, needs investigation
SKIP_CATEGORY_ALREADY_EXECUTED  = "already_executed"      # raced ourselves
SKIP_CATEGORY_AFTER_HOURS       = "equity_after_hours"    # equity intent outside US RTH
# 2026-02-23 — three-mode seat authority doctrine. When the
# intent's emitting brain is NOT the current seat holder for the
# intent's lane, auto-submit MUST refuse. Previously this state
# leaked through to `execution_submit` which raised HTTP 403,
# `maybe_auto_submit` caught the exception, and the row landed
# in the `auto_submit_failed/submit_raised` bucket — making a
# doctrine-correct REFUSAL look like a pipeline FAILURE on the
# operator's post-mortem panel. With this category, the same
# state surfaces as a clean SKIP row labeled
# "brain ≠ seat holder" so the operator can see at a glance
# whether the volume is structural (seat assignment mismatch)
# vs. an actual pipeline bug.
SKIP_CATEGORY_SEAT_AUTHORITY_MISMATCH = "seat_authority_mismatch"  # intent.stack != current seat holder
SKIP_CATEGORY_SEAT_VACANT       = "seat_vacant"           # no holder for the intent's lane
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
        # 2026-06-19: the legacy `dry_run_not_ready` bucket lumped three
        # different operational states together — split them so the
        # operator can act on the right signal:
        #   * 'blocked' / 'dry_run_blocked' / 'fail' / 'failed'
        #     → dry-run gate ran and correctly refused. Doctrine
        #       working as designed. Don't investigate — investigate
        #       the intent's source if the volume is high.
        #   * 'pending' / 'running' / 'queued'
        #     → dry-run task hasn't finished. Benign race; the finalizer
        #       will re-trigger auto-submit when the verdict lands.
        #   * '' (empty) / None / 'unknown'
        #     → dry_run_state was never set. Silent leak in the emit
        #       path — needs investigation.
        # We parse the quoted state out of the reason
        # ("dry_run_state 'pending' != required 'passed'") so future
        # rename of the state literals doesn't desync this mapper.
        try:
            first_quote = r.index("'") + 1
            second_quote = r.index("'", first_quote)
            state = r[first_quote:second_quote].lower()
        except ValueError:
            state = ""
        if state in {"blocked", "dry_run_blocked", "fail", "failed", "rejected_at_ingest"}:
            return SKIP_CATEGORY_DRY_RUN_BLOCKED
        if state in {"pending", "running", "queued", "dry_run_pending"}:
            return SKIP_CATEGORY_DRY_RUN_PENDING
        if state in {"", "unknown", "none"}:
            return SKIP_CATEGORY_DRY_RUN_MISSING
        # Future state literal we haven't catalogued yet — fall back to
        # the legacy bucket rather than misclassify into one of the
        # three actionable ones.
        return SKIP_CATEGORY_DRY_RUN_NOT_READY
    if r == "intent already executed":
        return SKIP_CATEGORY_ALREADY_EXECUTED
    if r.startswith("equity_after_hours"):
        return SKIP_CATEGORY_AFTER_HOURS
    # 2026-02-23 — three-mode seat authority doctrine. Two distinct
    # buckets so the operator sees "X intents from non-seat-holders"
    # (rotate or override) separately from "Y intents had no
    # executor seat at all" (assign one).
    if r.startswith("seat_authority "):
        if "vacant" in r:
            return SKIP_CATEGORY_SEAT_VACANT
        return SKIP_CATEGORY_SEAT_AUTHORITY_MISMATCH
    return SKIP_CATEGORY_OTHER




def _normalize_brain_to_stack(raw: str) -> str:
    """Normalize a brain identifier to its canonical brain_id.

    Intents may carry the brain identity in three forms across the
    codebase's rename in flight:
      * canonical brain_id : camino | barracuda | hellcat | gto  (preferred)
      * legacy stack code  : alpha | camaro | chevelle | redeye  (legacy wire)
      * UI display name    : Camino | Barracuda | Hellcat | GTO  (UI)

    All three normalize to the canonical brain_id here so the
    `allowed_brains` list (canonical-keyed since 2026-02-20) can match
    any of them. Unknown identifiers are returned lowercased unchanged
    so the audit reason carries the original token.

    Note (2026-06-25): the function name is historical — it used to
    return legacy stack codes back when `allowed_brains` was stack-
    keyed. It now returns canonical brain_ids. Renaming the function
    requires touching every call site so we kept the name and pinned
    the new semantics here.
    """
    key = (raw or "").lower().strip()
    if not key:
        return key
    # Already canonical → done.
    if key in {"camino", "barracuda", "hellcat", "gto"}:
        return key
    # Legacy stack code or display name → resolve via STACK_TO_BRAIN_ID.
    # The map already contains canonical→canonical entries so lower-
    # case display names ("camino") hit the early return; only legacy
    # codes ("alpha", "camaro", "chevelle", "redeye") need this branch.
    try:
        from shared.brain_doctrine import STACK_TO_BRAIN_ID  # noqa: WPS433
        if key in STACK_TO_BRAIN_ID:
            return STACK_TO_BRAIN_ID[key]
    except Exception:  # noqa: BLE001
        pass
    return key


async def matches_tier_1(intent: dict, policy: dict | None = None) -> tuple[bool, str]:
    """Returns (matches, reason). Reason describes the first failing
    criterion when matches=False so the audit trail can show WHY a
    given intent didn't auto-submit.

    Async since 2026-06-19 so the equity market-hours gate can consult
    the operator's Extended Hours Mongo toggle. Callers must `await`.
    """
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
    #
    # 2026-06-19: respects the operator-flippable Extended Hours
    # toggle (Mongo flag, set via `/api/admin/equity-extended-hours`).
    # When ON, accepts equity intents during Webull's 4 AM – 8 PM ET
    # extended-hours window M-F (still excludes weekends + holidays).
    # Same flag RoadGuard consults — so the two layers stay coherent.
    if lane == "equity":
        from shared.market_hours import (  # noqa: WPS433
            is_equity_extended_hours,
            is_equity_rth,
            market_hours_reason,
        )
        # Async helper, but we're inside an async function (callers
        # use `await _intent_passes_policy`).
        from routes.equity_extended_hours_admin import (  # noqa: WPS433
            get_equity_extended_hours_enabled,
        )
        extended = await get_equity_extended_hours_enabled()
        if extended:
            if not is_equity_extended_hours():
                return False, "equity_after_hours_extended:" + market_hours_reason()
        elif not is_equity_rth():
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

    # ── Seat-authority three-mode pre-check (2026-02-23) ───────────
    # Mirrors the doctrine that `_evaluate_gates` enforces via
    # `seat_authority_classification`: the auto-submit path MUST
    # only fire when the emitting brain is the current seat holder
    # for this lane. If we let non-seat-holder intents flow to
    # `execution_submit`, it raises HTTP 403 with
    # `blocked_by=seat_authority_classification`, and
    # `maybe_auto_submit`'s exception handler files them under
    # `auto_submit_failed/submit_raised` — making the operator's
    # post-mortem panel show 422 doctrine-correct refusals as
    # pipeline-failure noise.
    #
    # Resolving the seat holder here lets us return a clean
    # SKIP row with `skip_category=seat_authority_mismatch` (or
    # `seat_vacant` when no executor seat is assigned). The
    # ultimate execution gate stays authoritative — this is just
    # the auto-path's clean refusal mirror so the audit trail
    # tells the truth.
    #
    # Imported lazily inside the function so the module stays
    # importable from contexts that don't have the seat policy
    # ready at module load (tests, scripts).
    try:
        from shared.executor_seat import (  # noqa: WPS433
            get_seat_holder, seats_with_execute,
        )
        from shared.seat_policy import seat_may_execute_lane  # noqa: WPS433
        eligible_seats = seats_with_execute(lane)
        current_holder = None
        matched_seat = None
        for seat_name in eligible_seats:
            holder = await get_seat_holder(seat_name)
            if holder:
                matched_seat = seat_name
                current_holder = holder
                break
        if current_holder is None or not seat_may_execute_lane(matched_seat, lane):
            return False, (
                f"seat_authority vacant for lane={lane!r} — no executor "
                f"seat assigned (assign via Quick Seat Switches before "
                f"auto-submit can fire)"
            )
        holder_norm = _normalize_brain_to_stack(current_holder.strip().lower())
        # `brain` is already the normalized stack code from the
        # earlier brain-filter step above.
        if brain != holder_norm:
            return False, (
                f"seat_authority intent author {brain!r} != current seat "
                f"holder {holder_norm!r} (seat={matched_seat!r}, "
                f"lane={lane!r}); requires_override path — auto-submit "
                f"refuses by doctrine"
            )
    except Exception as exc:  # noqa: BLE001
        # Defensive: if the seat lookup itself raises (DB hiccup,
        # missing collection), do NOT block the intent on the
        # bookkeeping failure — `_evaluate_gates` will still
        # enforce the doctrine at submit time. Log so we can spot
        # the issue.
        logger.warning(
            "matches_tier_1: seat-authority pre-check raised "
            "(%s: %s); deferring to _evaluate_gates",
            type(exc).__name__, exc,
        )

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
        ok, reason = await matches_tier_1(intent, policy)
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
            # 2026-02-23: brain_name is REQUIRED on every submit.
            # Auto-submit passes the intent's stack verbatim — the
            # endpoint's match-check is therefore a no-op for the
            # auto path but a hard validation for the operator path.
            brain_name=(intent.get("stack") or ""),
        )
        auto_user = {
            "email": "auto_submit_tier_1@risedual.io",
            "auto_submit": True,
        }

        try:
            result = await execution_submit(submit_body, user=auto_user)
        except Exception as e:  # noqa: BLE001
            from shared.auto_submit_receipt import build_receipt
            receipt = build_receipt(intent_id, stage="submit_call", exc=e)
            logger.warning(
                "auto_submit submit-stage raised intent=%s err=%s:%s",
                intent_id, receipt.exception_type, receipt.exception_message,
            )
            await _write(receipt.to_row(
                kind="auto_submit_failed",
                skip_category="submit_raised",
                actor="auto_submit_tier_1",
            ) | {"tier": "tier_1_conservative"})
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
        from shared.auto_submit_receipt import build_receipt
        receipt = build_receipt(intent_id, stage="auto_submit_body", exc=e)
        logger.error(
            "auto_submit unexpected exception intent=%s type=%s msg=%s",
            intent_id, receipt.exception_type, receipt.exception_message,
        )
        await _write(receipt.to_row(
            kind="auto_submit_exception",
            skip_category="internal_error",
            actor="auto_submit_tier_1",
        ))
        raise

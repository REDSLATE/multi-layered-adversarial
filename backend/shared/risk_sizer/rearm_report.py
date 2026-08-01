"""Prod Deploy Watch (2026-08-01): first-organic-re-arm timeline,
re-armed-child outcome classification, and P0 deployment health checks
for the three fixes shipped after the live validation.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

TRIGGERS = "entry_rearm_triggers"

VALIDATION_PREFIXES = ("validate-", "rearmval", "expireval")

INTERNAL_BLOCK_REASONS = frozenset({
    "ENTRY_TIMING_REJECTED", "RISK_SIZER_REJECTED", "RISK_REJECTED",
    "REJECTED_CAP_EXCEEDED", "BELOW_PROBE_THRESHOLD", "AUTHORITY_EXPIRED",
    "ROUTE_TIMEOUT_POISON", "master_switch_disarmed",
})

OUTCOMES = ("waiting", "blocked_again", "submitted", "filled",
            "broker_rejected", "reconciliation_required")

RECONCILE_GRACE_S = 300


def _now() -> datetime:
    return datetime.now(timezone.utc)


def is_validation_doc(doc: dict) -> bool:
    iid = str(doc.get("intent_id") or doc.get("original_intent_id") or "")
    if iid.startswith(VALIDATION_PREFIXES):
        return True
    if (doc.get("stack") or "") == "validation":
        return True
    return "VALIDATION" in str(doc.get("rationale") or "")


def classify_child_outcome(child: dict, now: Optional[datetime] = None) -> str:
    """One of OUTCOMES for a re-armed child intent."""
    now = now or _now()
    if child.get("executed"):
        return "filled"
    gate = str(child.get("gate_state") or "")
    if gate == "submitted":
        try:
            submitted = datetime.fromisoformat(
                str(child.get("last_submit_ts") or child.get("ingest_ts")))
            if (now - submitted).total_seconds() > RECONCILE_GRACE_S:
                return "reconciliation_required"
        except (TypeError, ValueError):
            pass
        return "submitted"
    if gate == "expired_unrouted":
        # a child that never routed is the P0 #2 symptom — reconcile
        return "reconciliation_required"
    if gate in ("blocked", "no_trade", "advisory_only"):
        rr = str(child.get("risk_reason") or "")
        br = str(child.get("broker_reason") or "")
        if (rr.startswith("entry_timing:") or rr.startswith("risk_sizer:")
                or br in INTERNAL_BLOCK_REASONS):
            return "blocked_again"
        if br or child.get("broker_error_bucket"):
            return "broker_rejected"
        return "blocked_again"
    return "waiting"


_CHILD_PROJ = {
    "_id": 0, "intent_id": 1, "symbol": 1, "lane": 1, "gate_state": 1,
    "executed": 1, "ingest_ts": 1, "last_submit_ts": 1, "risk_reason": 1,
    "broker_reason": 1, "broker_error_bucket": 1, "rearm_of": 1,
    "trigger_id": 1, "stack": 1, "rationale": 1,
}


async def child_outcome_counts(db, cut_iso: str) -> dict:
    """Outcome histogram + queue/route counts for re-armed children."""
    from namespaces import SHARED_INTENTS  # noqa: WPS433
    from shared.hotpath import intent_queue  # noqa: WPS433
    children = await db[SHARED_INTENTS].find(
        {"rearm_of": {"$exists": True}, "ingest_ts": {"$gte": cut_iso}},
        _CHILD_PROJ,
    ).sort("ingest_ts", -1).max_time_ms(8000).to_list(200)
    counts = {k: 0 for k in OUTCOMES}
    in_queue = routed = 0
    queue_horizon = (_now() - timedelta(hours=23)).isoformat()
    for c in children:
        counts[classify_child_outcome(c)] += 1
        if str(c.get("gate_state") or "") not in ("", "pending"):
            routed += 1
        # queue prunes at 24h — only check recent children
        if str(c.get("ingest_ts") or "") >= queue_horizon:
            if intent_queue.has(c["intent_id"]):
                in_queue += 1
    return {"created": len(children), "in_local_queue": in_queue,
            "routed": routed, "outcomes": counts}


async def first_organic_rearm(db) -> Optional[dict]:
    """Earliest REARMED trigger that isn't a validation artifact."""
    from namespaces import SHARED_INTENTS  # noqa: WPS433
    rows = await db[TRIGGERS].find(
        {"state": "REARMED"}, {"_id": 0},
    ).sort("state_ts", 1).max_time_ms(8000).to_list(20)
    for t in rows:
        if is_validation_doc(t) or (t.get("stack") or "") == "validation":
            continue
        child = await db[SHARED_INTENTS].find_one(
            {"intent_id": t.get("rearm_attempt_id")}, _CHILD_PROJ,
            max_time_ms=4000,
        ) or {}
        if is_validation_doc(child):
            continue
        return {
            "trigger_id": t.get("trigger_id"), "symbol": t.get("symbol"),
            "lane": t.get("lane"), "rearmed_at": t.get("state_ts"),
            "block_price": t.get("block_price"),
            "new_confirmation_price": t.get("new_confirmation_price"),
            "child_intent_id": t.get("rearm_attempt_id"),
            "outcome": classify_child_outcome(child) if child else "waiting",
        }
    return None


async def build_rearm_timeline(db, trigger_id: Optional[str] = None) -> dict:
    """The single linked timeline the operator asked for: original
    intent → block → watch → re-arm → child → second gate chain →
    queue proof → broker → fills."""
    from namespaces import SHARED_INTENTS  # noqa: WPS433
    from shared.hotpath import intent_queue  # noqa: WPS433
    from shared.risk_sizer.entry_timing import get_config  # noqa: WPS433

    if trigger_id:
        trig = await db[TRIGGERS].find_one(
            {"trigger_id": trigger_id}, {"_id": 0}, max_time_ms=4000)
    else:
        first = await first_organic_rearm(db)
        trig = (await db[TRIGGERS].find_one(
            {"trigger_id": first["trigger_id"]}, {"_id": 0},
            max_time_ms=4000)) if first else None
    if not trig:
        return {"ok": True, "found": False,
                "note": "no qualifying re-armed trigger yet"}

    orig = await db[SHARED_INTENTS].find_one(
        {"intent_id": trig.get("original_intent_id")}, {"_id": 0},
        max_time_ms=4000) or {}
    child = await db[SHARED_INTENTS].find_one(
        {"intent_id": trig.get("rearm_attempt_id")}, {"_id": 0},
        max_time_ms=4000) or {}

    o_receipt = orig.get("entry_timing_receipt") or {}
    profile = o_receipt.get("profile")
    class_cap = None
    try:
        cfg = await get_config()
        class_cap = (cfg["profiles"].get(profile) or {}).get(
            "max_extension_from_confirmation_pct")
    except Exception:  # noqa: BLE001
        pass

    last_check = trig.get("last_check") or {}
    block_price = trig.get("block_price")
    new_conf = trig.get("new_confirmation_price")
    improvement = None
    if block_price and new_conf:
        improvement = round((block_price - new_conf) / block_price * 100, 3)

    # child gate results
    c_receipt = child.get("entry_timing_receipt") or {}
    rr = str(child.get("risk_reason") or "")
    if "not_in_buy_allowlist" in rr:
        allowlist = "REJECTED"
    elif (child.get("gate_state") in ("submitted",) or child.get("executed")
          or rr.startswith("entry_timing:")):
        allowlist = "PASSED"
    elif rr.startswith("risk_sizer:") or rr.startswith("risk:"):
        allowlist = "NOT_REACHED_OR_PASSED"
    else:
        allowlist = "NOT_REACHED"

    exec_rows = await db["executions"].find(
        {"intent_id": child.get("intent_id")},
        {"_id": 0, "ts": 1, "ok": 1, "broker_status": 1, "notional_usd": 1,
         "risk_reason": 1, "exception_type": 1, "exception_msg": 1,
         "broker_order_id": 1},
    ).sort("ts", 1).max_time_ms(4000).to_list(10) if child else []
    fills = await db["shared_broker_fills"].find(
        {"symbol": child.get("symbol"),
         "ts": {"$gte": str(child.get("ingest_ts") or "")}},
        {"_id": 0, "ts": 1, "price": 1, "qty": 1, "fee": 1, "broker": 1,
         "order_id": 1},
    ).sort("ts", 1).max_time_ms(4000).to_list(5) if child else []

    in_queue = (intent_queue.has(child["intent_id"])
                if child.get("intent_id") else False)

    return {
        "ok": True, "found": True,
        "trigger": {k: trig.get(k) for k in (
            "trigger_id", "symbol", "lane", "state", "state_reason",
            "state_ts", "created_at", "expires_at", "attempts",
            "timing_block_reason", "block_price", "peak_price",
            "duplicate_blocks_prevented")},
        "original_intent": {
            "intent_id": orig.get("intent_id"),
            "ingest_ts": orig.get("ingest_ts"),
            "confirmation_price": o_receipt.get("confirmation_price"),
            "confirmation_source": o_receipt.get("confirmation_source"),
            "extension_pct": o_receipt.get(
                "extension_from_confirmation_pct"),
            "class_cap_pct": class_cap,
            "universe_class": o_receipt.get("universe_class"),
            "block_reason": orig.get("entry_timing_reason"),
            "message": o_receipt.get("message"),
        },
        "pullback": {
            "depth_pct": last_check.get("pullback_depth_pct"),
            "volume_contraction": last_check.get("volume_contraction"),
            "support": last_check.get("support"),
            "reacceleration": last_check.get("why"),
            "checked_at": last_check.get("ts"),
        },
        "child": {
            "intent_id": child.get("intent_id"),
            "rearm_attempt_id": child.get("rearm_attempt_id"),
            "rearm_of": child.get("rearm_of"),
            "timing_block_receipt_id": child.get("timing_block_receipt_id"),
            "ingest_ts": child.get("ingest_ts"),
            "new_confirmation_price": child.get("new_confirmation_price"),
            "new_invalidation_price": child.get("new_invalidation_price"),
            "stop_price": child.get("stop_price"),
            "gate_state": child.get("gate_state"),
            "seat_tier": child.get("action_tier"),
            "risk_reason": child.get("risk_reason"),
            "risk_sizing": child.get("risk_sizing"),
            "allowlist": allowlist,
            "second_entry_timing": {
                "decision": child.get("entry_timing_decision"),
                "reason": child.get("entry_timing_reason"),
                "extension_pct": c_receipt.get(
                    "extension_from_confirmation_pct"),
                "confirmation_source": c_receipt.get("confirmation_source"),
                "fresh_price": c_receipt.get("current_price"),
                "message": c_receipt.get("message"),
            } if child.get("entry_timing_decision") else "NOT_REACHED",
            "outcome": classify_child_outcome(child) if child else None,
        },
        "queue": {"in_local_queue": in_queue},
        "broker": {"executions": exec_rows, "fills": fills},
        "improvement_pct": improvement,
    }


async def health_checks(db) -> dict:
    """The three P0-fix deployment guards."""
    from namespaces import SHARED_INTENTS  # noqa: WPS433
    from shared.hotpath import intent_queue  # noqa: WPS433
    now = _now()
    cut24 = (now - timedelta(hours=24)).isoformat()
    checks: list[dict] = []

    # 1 — organic BUY reached the gate without a usable confirmation
    q1 = {"entry_timing_reason": "NO_TIMING_DATA",
          "ingest_ts": {"$gte": cut24},
          "intent_id": {"$not": {"$regex": "^(validate-|rearmval|expireval)"}},
          "rationale": {"$not": {"$regex": "VALIDATION"}}}
    rows1 = await db[SHARED_INTENTS].find(
        q1, {"_id": 0, "intent_id": 1, "symbol": 1, "lane": 1},
    ).max_time_ms(8000).to_list(5)
    n1 = await db[SHARED_INTENTS].count_documents(q1, maxTimeMS=8000)
    checks.append({
        "id": "confirmation_derivation",
        "ok": n1 == 0, "count": n1, "samples": rows1,
        "detail": ("all organic BUYs derived a confirmation price"
                   if n1 == 0 else
                   f"{n1} organic BUY(s) hit NO_TIMING_DATA in 24h — "
                   "confirmation derivation (P0 #1 fix) failing"),
    })

    # 2 — REARMED child in Mongo but absent from the local queue
    horizon = (now - timedelta(hours=23)).isoformat()
    rearmed = await db[TRIGGERS].find(
        {"state": "REARMED", "state_ts": {"$gte": horizon},
         "rearm_attempt_id": {"$ne": None}},
        {"_id": 0, "trigger_id": 1, "symbol": 1, "rearm_attempt_id": 1},
    ).max_time_ms(8000).to_list(50)
    missing = [t for t in rearmed
               if not intent_queue.has(t["rearm_attempt_id"])]
    checks.append({
        "id": "child_in_local_queue",
        "ok": not missing, "count": len(missing),
        "samples": missing[:5],
        "detail": (f"all {len(rearmed)} recent re-armed children present "
                   "in the local queue" if not missing else
                   f"{len(missing)} re-armed child(ren) missing from the "
                   "local queue — they will NEVER route (P0 #2 fix failing)"),
    })

    # 3 — thawed freeze must not roadguard-block sizing
    fz = await db["broker_freeze_state"].find_one(
        {"_id": "current"}, {"_id": 0, "frozen": 1, "reason": 1},
        max_time_ms=4000) or {}
    stale_reason = (not fz.get("frozen")) and bool(fz.get("reason"))
    rg_n = 0
    if not fz.get("frozen"):
        rg_n = await db[SHARED_INTENTS].count_documents(
            {"risk_reason": "risk_sizer:roadguard_hard_block",
             "ingest_ts": {"$gte": cut24},
             "rationale": {"$not": {"$regex": "VALIDATION"}}},
            maxTimeMS=8000)
    ok3 = not stale_reason and rg_n == 0
    checks.append({
        "id": "freeze_thaw_roadguard",
        "ok": ok3, "count": rg_n,
        "stale_reason": fz.get("reason") if stale_reason else None,
        "detail": ("freeze doc clean, no roadguard blocks while unfrozen"
                   if ok3 else
                   f"broker_frozen={bool(fz.get('frozen'))} but "
                   f"reason={fz.get('reason')!r} lingers / {rg_n} roadguard "
                   "block(s) in 24h while unfrozen (P0 #3 fix failing)"),
    })
    return {"ok": all(c["ok"] for c in checks), "checks": checks,
            "checked_at": now.isoformat()}

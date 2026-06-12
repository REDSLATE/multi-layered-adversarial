"""Diagnostics endpoints. Read-only system health + per-runtime liveness."""
import os
from datetime import datetime, timezone
from fastapi import APIRouter, Depends

from auth import get_current_user
from db import db, client
from namespaces import (
    SHARED_RECEIPTS, SHARED_MEMORY, SHARED_HEARTBEATS,
    ALPHA_DECISION_LOG, CAMARO_SHADOW_ROWS, CHEVELLE_MEMORY_LABELS,
    REDEYE_DECISION_LOG, RUNTIMES,
    SHARED_OPINIONS,
    HEARTBEAT_STALE_AFTER_SECONDS,
    HEARTBEAT_OK_BELOW_SECONDS,
    HEARTBEAT_PREVIEW_DRIFT_SECONDS,
    RECEIPT_STALE_AFTER_SECONDS,
)


def _heartbeat_tier(age: float | None) -> str:
    """Liveness band ONLY — derived purely from heartbeat age.

    Doctrine (2026-02-18): this function used to return a
    `preview_drift` tier that conflated "stale heartbeat" with "wrong
    MC URL". That heuristic produced false alarms whenever a brain
    did real LLM work that exceeded the 110s window — operators spent
    cycles chasing phantom MC_BASE_URL misconfiguration. The actual
    "is this pod on preview?" verdict comes from
    `sidecar_checkin._verdict_from_validation`, which inspects the
    brain's stamped `env_name` + `mc_url`. THIS function answers only:
    "how long since the brain last said hello?".

    Bands:
        ok            < HEARTBEAT_OK_BELOW_SECONDS      (healthy)
        stale         < HEARTBEAT_PREVIEW_DRIFT_SECONDS (slow ping)
        dead          ≥ HEARTBEAT_PREVIEW_DRIFT_SECONDS (no recent ping)
        unknown       no heartbeat ever recorded
    """
    if age is None:
        return "unknown"
    if age < HEARTBEAT_OK_BELOW_SECONDS:
        return "ok"
    if age < HEARTBEAT_PREVIEW_DRIFT_SECONDS:
        return "stale"
    return "dead"


def _effective_tier(hb_tier: str, receipt_age_s: float | None) -> str:
    """Operator-facing tier that joins heartbeat freshness with
    decision-receipt freshness (2026-02-19, May-14 silent-hang tripwire).

    Doctrine: a brain that heartbeats every 30s but hasn't produced a
    decision in 12 days is NOT live — it's silent. The legacy badge
    showed `LIVE` for both because it only inspected the heartbeat.
    This function downgrades a fresh-heartbeat brain to `silent` when
    its last receipt is older than `RECEIPT_STALE_AFTER_SECONDS`. All
    non-`ok` heartbeat tiers (stale/dead/unknown) pass through unchanged —
    a dead heartbeat is a stronger signal than a stale receipt.

    Bands (in addition to the heartbeat bands):
        silent  — heartbeat fresh, but last receipt > threshold (or
                  no receipt ever recorded). Operator action: the
                  brain process is alive but its decision loop is
                  wedged or it has never produced an intent.
    """
    if hb_tier != "ok":
        return hb_tier
    # Fresh heartbeat — now check the decision loop. None means
    # "no receipt ever" which IS silent (the brain never wrote anything).
    if receipt_age_s is None or receipt_age_s > RECEIPT_STALE_AFTER_SECONDS:
        return "silent"
    return "ok"


router = APIRouter(prefix="/admin/diagnostics", tags=["diagnostics"])


async def _last_receipt_ts(runtime: str) -> str | None:
    """Latest decision-artifact timestamp for `runtime` across ALL the
    collections a brain writes to when it makes a decision.

    Doctrine (2026-02-19, rev3 — prod SILENT-badge bug):
        The legacy `shared_receipts` collection was the original
        canonical "the brain just decided something" tripwire. But
        the in-process runner doesn't write to it — modern brains
        post intents to `shared_intents` and opinions to
        `shared_brain_opinions`, and only authority-call mirrors
        backfill `shared_receipts`. So a brain firing intents every
        second showed SILENT for 13 days straight because nothing
        was touching the legacy collection.

        The operator-facing definition of "silent" is "this brain
        hasn't produced ANY decision artifact recently" — not "this
        brain hasn't written to one specific legacy collection".
        Take the MAX over all the modern decision-emitting paths:

          * `shared_intents.ingest_ts`            where `stack==runtime`
          * `shared_brain_opinions.posted_at`     where `runtime==runtime`
          * `shared_receipts.timestamp`           where `runtime==runtime`
                                                  (legacy authority-call
                                                  mirror — still respected)
          * `<brain>_decision_log.timestamp`      per-brain audit trail

        If ANY of these is fresh, the brain is NOT silent.

    Returns ISO timestamp string or None if no artifact exists.
    """
    candidates: list[str] = []

    # 1. Legacy receipts collection — still consulted for backward
    #    compat with the authority-call mirror written by
    #    `shared/opinions.py::_mirror_authority_call_to_receipt`.
    doc = await db[SHARED_RECEIPTS].find_one(
        {"runtime": runtime}, {"_id": 0, "timestamp": 1},
        sort=[("timestamp", -1)],
    )
    if doc and doc.get("timestamp"):
        candidates.append(doc["timestamp"])

    # 2. Cross-brain opinion stream — runner posts on every intent.
    doc = await db[SHARED_OPINIONS].find_one(
        {"runtime": runtime}, {"_id": 0, "posted_at": 1},
        sort=[("posted_at", -1)],
    )
    if doc and doc.get("posted_at"):
        candidates.append(doc["posted_at"])

    # 3. shared_intents — the actual decision artifact. Note the
    #    identifier field is `stack`, not `runtime`.
    try:
        from namespaces import SHARED_INTENTS  # noqa: WPS433
    except ImportError:
        SHARED_INTENTS = None  # very old layout; skip gracefully
    if SHARED_INTENTS:
        doc = await db[SHARED_INTENTS].find_one(
            {"stack": runtime}, {"_id": 0, "ingest_ts": 1},
            sort=[("ingest_ts", -1)],
        )
        if doc and doc.get("ingest_ts"):
            candidates.append(doc["ingest_ts"])

    # 4. Per-brain canonical decision log — the brain's own append-
    #    only audit trail. No `runtime` filter needed; the collection
    #    IS the brain.
    coll_per_brain = {
        "alpha":    ALPHA_DECISION_LOG,
        "camaro":   CAMARO_SHADOW_ROWS,
        "chevelle": CHEVELLE_MEMORY_LABELS,
        "redeye":   REDEYE_DECISION_LOG,
    }.get(runtime)
    if coll_per_brain:
        doc = await db[coll_per_brain].find_one(
            {}, {"_id": 0, "timestamp": 1},
            sort=[("timestamp", -1)],
        )
        if doc and doc.get("timestamp"):
            candidates.append(doc["timestamp"])

    if not candidates:
        return None
    # Return the latest. ISO-8601 strings sort lexicographically
    # provided they all share a timezone (they do — every writer
    # uses `datetime.now(timezone.utc).isoformat()`).
    return max(candidates)


async def _runtime_log_count(runtime: str) -> int:
    """Per-brain canonical decision-log count.

    2026-05-29: REDEYE now has its own `redeye_decision_log` collection
    (parity with alpha/camaro/chevelle). MC reads from it directly so
    the column shows TRUE intent count instead of falling back to the
    opinion-post count it was using before. Contract for the RedEye
    team: see /app/memory/MC_HANDOFF_redeye_decision_log.md.

    If RedEye's log doesn't exist yet (brand-new pod, or stamp not
    arrived), the count returns 0 instead of crashing.
    """
    coll = {
        "alpha":    ALPHA_DECISION_LOG,
        "camaro":   CAMARO_SHADOW_ROWS,
        "chevelle": CHEVELLE_MEMORY_LABELS,
        "redeye":   REDEYE_DECISION_LOG,
    }.get(runtime)
    if coll is None:
        # Unknown runtime — opinion-post count as the safe fallback.
        return await db[SHARED_OPINIONS].count_documents({"runtime": runtime})
    return await db[coll].count_documents({})


def _hb_age_and_stale(hb: dict | None) -> tuple[float | None, bool]:
    """Compute heartbeat age in seconds and whether it's stale."""
    if not hb or not hb.get("last_seen"):
        return None, True
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(hb["last_seen"])).total_seconds()
    except Exception:  # noqa: BLE001
        return None, True
    return age, age > HEARTBEAT_STALE_AFTER_SECONDS


@router.get("")
async def diagnostics(_user: dict = Depends(get_current_user)):
    try:
        await client.admin.command("ping")
        mongo_ok = True
        mongo_err = None
    except Exception as e:  # noqa: BLE001
        mongo_ok = False
        mongo_err = str(e)

    per_runtime = []
    for rt in RUNTIMES:
        hb = await db[SHARED_HEARTBEATS].find_one({"runtime": rt}, {"_id": 0})
        hb_age, hb_stale = _hb_age_and_stale(hb)
        hb_tier = _heartbeat_tier(hb_age)
        last_receipt_ts = await _last_receipt_ts(rt)
        # Receipt freshness — joined against hb_tier to produce the
        # `silent` band (May-14 tripwire). None means no receipt ever.
        receipt_age_s: float | None = None
        if last_receipt_ts:
            try:
                receipt_age_s = (
                    datetime.now(timezone.utc)
                    - datetime.fromisoformat(last_receipt_ts)
                ).total_seconds()
            except Exception:  # noqa: BLE001
                receipt_age_s = None
        per_runtime.append({
            "runtime": rt,
            "last_receipt_ts": last_receipt_ts,
            "last_receipt_age_seconds": receipt_age_s,
            "log_count": await _runtime_log_count(rt),
            "memory_labels_count": await db[SHARED_MEMORY].count_documents({"runtime": rt}),
            "heartbeat": hb,
            "heartbeat_age_seconds": hb_age,
            "heartbeat_stale": hb_stale,
            "heartbeat_tier": hb_tier,
            # Operator-facing tier that joins heartbeat + receipt
            # freshness. The UI keys its badge color/label off this
            # field (2026-02-19 silent-hang tripwire).
            "effective_tier": _effective_tier(hb_tier, receipt_age_s),
        })

    # Lane execution toggles — the operator's real kill switch.
    # Surface alongside `deploy_mode` so the UI can stop misleading
    # the operator with an env-var-only banner.
    from shared.lane_execution import get_toggles as _lane_toggles  # noqa: WPS433
    lane_toggles = await _lane_toggles()

    return {
        "now": datetime.now(timezone.utc).isoformat(),
        "deploy_mode": os.environ.get("DEPLOY_MODE", "observation"),
        "lane_execution": {
            "equity": lane_toggles["equity"],
            "crypto": lane_toggles["crypto"],
            "any_enabled": lane_toggles["equity"] or lane_toggles["crypto"],
        },
        "heartbeat_stale_after_seconds": HEARTBEAT_STALE_AFTER_SECONDS,
        "heartbeat_ok_below_seconds": HEARTBEAT_OK_BELOW_SECONDS,
        "heartbeat_preview_drift_seconds": HEARTBEAT_PREVIEW_DRIFT_SECONDS,
        "receipt_stale_after_seconds": RECEIPT_STALE_AFTER_SECONDS,
        "mongo": {"ok": mongo_ok, "error": mongo_err},
        "runtimes": per_runtime,
    }

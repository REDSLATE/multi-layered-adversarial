"""Sidecar diagnostics aggregator (2026-05-27).

One-curl operator surface for "is my sidecar fleet healthy?" Answers
the questions that drove this pass:
  * Why is CAMARO showing DEAD with 31425s stale heartbeat?
  * Why does RedEye have 21k audit checkpoints but ZERO gate-chain
    intent emissions?
  * Which brains are heartbeating but never contributing?

Pulls from FIVE collections in parallel and folds into a single
per-brain row:

  * `shared_heartbeats`     — last heartbeat ping (sidecar process alive)
  * `sovereign_state`       — last sovereign contribution (brain is
                              posting weights, not just pinging)
  * `shared_intents`        — last gate-chain intent (brain is actually
                              emitting trade signals)
  * `shared_brain_opinions` — last opinion (brain participates in
                              cross-brain discussion)
  * `sovereign_audit_log`   — total audit checkpoint count (the
                              "21k mystery" answer — these are healthy
                              heartbeat-style audit rows)

Doctrine: read-only. Never modifies seats, never reroutes traffic,
never affects authority. Pure operator visibility.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query

from auth import get_current_user
from db import db
from namespaces import (
    DISCUSSION_PARTICIPANTS,
    SHARED_HEARTBEATS,
    SHARED_INTENTS,
    SHARED_OPINIONS,
    SOVEREIGN_AUDIT_LOG,
    SOVEREIGN_STATE,
)


router = APIRouter(tags=["admin", "sidecar-diagnostics"])


# Liveness bands matching the existing LivePulse classifier in
# `shared/heartbeat_ping.py` so the operator sees the same vocabulary
# everywhere. Heartbeat-only and sovereign-only thresholds:
HB_FRESH_SEC = 300.0      # heartbeat fresher than this ⇒ pod alive
                          # (2026-02-19: raised from 90s to 300s — see
                          # namespaces.HEARTBEAT_OK_BELOW_SECONDS for rationale)
SV_FRESH_SEC = 300.0      # sovereign contribution fresher than this ⇒ "connected"
SV_STALE_SEC = 1800.0     # 30 min — still recoverable, no banner yet


def _age_seconds(iso: Optional[str], now: datetime) -> Optional[float]:
    if not iso:
        return None
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return (now - t).total_seconds()
    except (ValueError, AttributeError):
        return None


def _classify(hb_age: Optional[float], sv_age: Optional[float]) -> str:
    """Same classifier as `/heartbeat-status/{brain}` so the diagnostics
    panel and the LivePulse badge never disagree on the verdict."""
    hb_fresh = hb_age is not None and hb_age < HB_FRESH_SEC
    sv_fresh = sv_age is not None and sv_age < SV_FRESH_SEC
    sv_stale_band = sv_age is not None and sv_age < SV_STALE_SEC
    if hb_age is None and sv_age is None:
        return "never"
    if hb_fresh and sv_fresh:
        return "connected"
    if hb_fresh and not sv_fresh:
        return "partial"
    if sv_stale_band:
        return "stale"
    return "dead"


@router.get("/admin/sidecar-diagnostics")
async def sidecar_diagnostics(
    _user: dict = Depends(get_current_user),
):
    """Aggregate per-brain sidecar health into one operator-visible row.

    Returns one row per brain in `DISCUSSION_PARTICIPANTS`. Each row
    carries every signal needed to triage "is this brain alive, is it
    contributing, is it emitting intents, is it participating in
    discussion?" without the operator needing to query five separate
    endpoints.

    Doctrine pin: read-only. Doesn't modify seats, authority, or
    routing. Purely a visibility surface for the dashboard.
    """
    now = datetime.now(timezone.utc)
    rows: list[dict] = []

    for brain in DISCUSSION_PARTICIPANTS:
        # 1. Heartbeat — sidecar process pinging
        hb = await db[SHARED_HEARTBEATS].find_one(
            {"runtime": brain}, {"_id": 0},
        )
        hb_iso = (hb or {}).get("last_seen")
        hb_age = _age_seconds(hb_iso, now)
        hb_count = (hb or {}).get("heartbeat_count")

        # 2. Sovereign contribution — brain posting its weights
        sv = await db[SOVEREIGN_STATE].find_one({"brain": brain}, {"_id": 0})
        sv_iso = (sv or {}).get("updated_at")
        sv_age = _age_seconds(sv_iso, now)
        sv_contribution_count = (sv or {}).get("contribution_count")

        # 3. Sovereign audit log total — the "21k mystery" answer.
        # These rows are healthy heartbeat-style checkpoints — one row
        # per sidecar tick (~1/min). High counts are EXPECTED and
        # GOOD — they mean the brain has been alive for a long time.
        # 21k ÷ ~60s ≈ 358h ≈ 15 days of operation.
        sv_audit_total = await db[SOVEREIGN_AUDIT_LOG].count_documents(
            {"brain": brain},
        )

        # 4. Gate-chain intent emissions — actual trade signals
        intent_total = await db[SHARED_INTENTS].count_documents(
            {"stack": brain},
        )
        latest_intent = await db[SHARED_INTENTS].find_one(
            {"stack": brain}, {"_id": 0, "ingest_ts": 1, "symbol": 1,
                                "action": 1, "lane": 1, "gate_state": 1},
            sort=[("ingest_ts", -1)],
        )
        intent_iso = (latest_intent or {}).get("ingest_ts")
        intent_age = _age_seconds(intent_iso, now)

        # 5. Cross-brain opinions — discussion-layer participation
        opinion_total = await db[SHARED_OPINIONS].count_documents(
            {"runtime": brain},
        )
        latest_opinion = await db[SHARED_OPINIONS].find_one(
            {"runtime": brain}, {"_id": 0, "posted_at": 1},
            sort=[("posted_at", -1)],
        )
        opinion_iso = (latest_opinion or {}).get("posted_at")
        opinion_age = _age_seconds(opinion_iso, now)

        # Operator-facing health verdict — same classifier as LivePulse.
        verdict = _classify(hb_age, sv_age)

        # Per-brain operator hint: what's the single most-actionable
        # thing the operator can do for this brain right now?
        hint = _operator_hint(
            verdict=verdict,
            sv_audit_total=sv_audit_total,
            intent_total=intent_total,
            intent_age=intent_age,
            opinion_total=opinion_total,
        )

        rows.append({
            "brain": brain,
            "verdict": verdict,
            "operator_hint": hint,
            # Heartbeat (pod alive)
            "heartbeat": {
                "last_seen": hb_iso,
                "age_seconds": round(hb_age, 1) if hb_age is not None else None,
                "count": hb_count,
                "fresh": hb_age is not None and hb_age < HB_FRESH_SEC,
            },
            # Sovereign contribution (brain posting weights)
            "sovereign_contribution": {
                "last_seen": sv_iso,
                "age_seconds": round(sv_age, 1) if sv_age is not None else None,
                "live_count": sv_contribution_count,    # from sovereign_state
                "audit_log_total": sv_audit_total,      # from sovereign_audit_log
                "fresh": sv_age is not None and sv_age < SV_FRESH_SEC,
            },
            # Gate-chain intent emissions (real trade signals)
            "intents": {
                "total": intent_total,
                "last_seen": intent_iso,
                "age_seconds": round(intent_age, 1) if intent_age is not None else None,
                "latest_symbol": (latest_intent or {}).get("symbol"),
                "latest_action": (latest_intent or {}).get("action"),
                "latest_lane": (latest_intent or {}).get("lane"),
                "latest_gate_state": (latest_intent or {}).get("gate_state"),
            },
            # Cross-brain discussion participation
            "opinions": {
                "total": opinion_total,
                "last_seen": opinion_iso,
                "age_seconds": round(opinion_age, 1) if opinion_age is not None else None,
            },
        })

    # Fleet-wide rollup for quick at-a-glance triage.
    fleet = {
        "total_brains": len(rows),
        "connected": sum(1 for r in rows if r["verdict"] == "connected"),
        "partial": sum(1 for r in rows if r["verdict"] == "partial"),
        "stale": sum(1 for r in rows if r["verdict"] == "stale"),
        "dead": sum(1 for r in rows if r["verdict"] == "dead"),
        "never": sum(1 for r in rows if r["verdict"] == "never"),
        "brains_with_no_intents_ever": sum(
            1 for r in rows if r["intents"]["total"] == 0
        ),
        "brains_with_no_opinions_ever": sum(
            1 for r in rows if r["opinions"]["total"] == 0
        ),
    }

    return {
        "generated_at": now.isoformat(),
        "fleet": fleet,
        "brains": rows,
        "doctrine_note": (
            "Read-only diagnostics. Sovereign audit log total is a "
            "HEALTHY HEARTBEAT counter (~1/min × days alive), NOT a "
            "backlog. The actionable counters are `intents.total` "
            "(real trade signals emitted), `opinions.total` "
            "(discussion participation), and `verdict` (LivePulse "
            "classification)."
        ),
    }


def _operator_hint(
    *,
    verdict: str,
    sv_audit_total: int,
    intent_total: int,
    intent_age: Optional[float],
    opinion_total: int,
) -> str:
    """One-line, operator-actionable next step per brain.
    Doctrine: hints are guidance only — they NEVER change behavior.
    """
    if verdict == "never":
        return "Brain has never contacted MC. Check sidecar pod is deployed and base URL points to MC."
    if verdict == "dead":
        return "No heartbeat or sovereign contribution in the recovery window. Check sidecar pod logs — likely hung, OOM-killed, or rate-limited."
    if verdict == "stale":
        return "Sovereign contribution last seen 5–30 min ago. Sidecar may have stalled mid-tick; watch for the next contribution."
    if verdict == "partial":
        return "Heartbeat is fresh but sovereign contribution is stale/missing. Either legacy ingest only, OR sidecar isn't calling /sovereign/contribution. Check brain's reporting loop."
    # verdict == "connected"
    if intent_total == 0 and opinion_total == 0 and sv_audit_total > 0:
        return "Auditing only — heartbeats fine, but the brain has never emitted an intent or opinion. Check whether this brain has an emitter role or is observer-only by design."
    if intent_total == 0 and opinion_total > 0:
        return "Brain participates in discussion but has never emitted an intent. Normal for observer-only roles; investigate if this brain SHOULD be trading."
    if intent_age is not None and intent_age > 86400:
        return f"Last intent was {round(intent_age/3600, 1)}h ago. Brain may have stopped emitting signals — check brain-side decision loop."
    return "Healthy — heartbeating, contributing, emitting intents."

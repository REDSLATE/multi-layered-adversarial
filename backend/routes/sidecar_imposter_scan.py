"""Imposter scan over the sidecar_checkin_audit collection.

What it catches:
    For each runtime, look at the last `window_hours` of check-in
    audit rows. If MC saw more than ONE distinct
    `(env_name, pip_freeze_sha256)` pair claiming to be that brain,
    something is impersonating it (or the brain has two pods running
    against the same MC).

    Per Alpha's request after the preview-pod-impersonating-prod
    incident: rapid distinct (pid, hostname) tuples are also a signal,
    so we surface those too when `process_identity` is present.

Read-only. Bounded by Mongo aggregation, no LLM, no heavy compute.

Mounted at:
    GET /api/admin/runtime/sidecar-imposter-scan?window_hours=24

Returns:
{
  "ok": true,
  "window_hours": 24,
  "by_runtime": [
     {
       "runtime": "redeye",
       "checkin_count": 288,
       "distinct_env_names": ["preview"],
       "distinct_pip_shas":  ["1aeb..."],
       "distinct_source_ips": ["10.0.1.4"],
       "distinct_process_identities": [{"pid": 1, "hostname": "redeye-pod-7"}],
       "imposter_suspected": false,
       "reasons": []
     },
     ...
  ],
  "any_imposter_suspected": false
}
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, Query

from auth import get_current_user
from db import db
from namespaces import DISCUSSION_PARTICIPANTS


router = APIRouter(prefix="/admin/runtime", tags=["sidecar-imposter-scan"])


@router.get("/sidecar-imposter-scan")
async def imposter_scan(
    window_hours: int = Query(24, ge=1, le=168),
    env: str = Query(
        "all",
        description=(
            "Filter check-ins by `stamp.env_name` before scanning. "
            "Use `prod` on the production dashboard to ignore "
            "preview-pod check-ins that share Mongo. `all` (default) "
            "preserves legacy behavior."
        ),
    ),
    _user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    """Scan the audit log for any runtime that's shown TWO+ distinct
    identities in the recent window. Operator surface — flags it,
    never auto-acts.

    2026-02-XX: added `env` filter. Preview and prod share the same
    Mongo cluster, so both pods' check-ins land in
    `sidecar_checkin_audit`. The default `env=all` view shows ALL
    sources (useful for debugging cross-env confusion); `env=prod`
    isolates the prod-side stream so the prod dashboard stops
    flagging legitimate preview check-ins as imposters.
    """
    cutoff_epoch = (
        datetime.now(timezone.utc) - timedelta(hours=window_hours)
    ).timestamp()

    match: dict[str, Any] = {"ts_epoch": {"$gte": cutoff_epoch}}
    env_normalized = (env or "all").strip().lower()
    if env_normalized != "all":
        # Filter at the Mongo layer — keeps the aggregation lean.
        match["stamp_env_name"] = env_normalized

    pipeline = [
        {"$match": match},
        {"$group": {
            "_id": "$runtime",
            "checkin_count": {"$sum": 1},
            "env_names": {"$addToSet": "$stamp_env_name"},
            "pip_shas": {"$addToSet": "$stamp_pip_sha"},
            "source_ips": {"$addToSet": "$source_ip"},
            "process_identities": {"$addToSet": "$process_identity"},
            "git_shas": {"$addToSet": "$stamp_git_sha"},
            "verdicts": {"$addToSet": "$verdict"},
            "first_seen": {"$min": "$ts"},
            "last_seen": {"$max": "$ts"},
        }},
        {"$sort": {"_id": 1}},
    ]
    rows = []
    async for r in db["sidecar_checkin_audit"].aggregate(pipeline):
        rows.append(r)

    by_runtime: list[dict[str, Any]] = []
    any_imposter = False

    # Build a set of runtimes we know about so unknown app_names that
    # show up in audit are reported separately ("unauthorized brain").
    known = set(DISCUSSION_PARTICIPANTS)

    for r in rows:
        runtime = r["_id"]
        # Strip Nones from set values so we don't count a missing field
        # as a distinct identity.
        env_names = sorted({x for x in (r.get("env_names") or []) if x})
        pip_shas = sorted({x for x in (r.get("pip_shas") or []) if x})
        source_ips = sorted({x for x in (r.get("source_ips") or []) if x})
        git_shas = sorted({x for x in (r.get("git_shas") or []) if x})
        # process_identity is a dict — dedupe by (pid, hostname).
        pi_seen: dict[tuple, dict] = {}
        for pi in (r.get("process_identities") or []):
            if not pi:
                continue
            key = (pi.get("pid"), pi.get("hostname"))
            pi_seen[key] = pi
        process_identities = list(pi_seen.values())

        reasons: list[str] = []
        if runtime not in known:
            reasons.append(
                f"UNKNOWN_RUNTIME — {runtime!r} is not in DISCUSSION_PARTICIPANTS"
            )
        if len(env_names) > 1:
            reasons.append(f"DIVERGENT_ENV_NAME: {env_names}")
        if len(pip_shas) > 1:
            reasons.append(
                f"DIVERGENT_PIP_FINGERPRINT: {len(pip_shas)} distinct shas"
            )
        if len(git_shas) > 1:
            # Two git shas in 24h could be a deploy rollover — still
            # worth surfacing, just lower severity.
            reasons.append(
                f"MULTIPLE_GIT_SHAS: {git_shas} "
                f"(legitimate if a redeploy happened in window)"
            )
        if len(process_identities) > 1:
            reasons.append(
                f"MULTIPLE_PROCESSES: {len(process_identities)} distinct "
                f"(pid, hostname) tuples — possible duplicate pod"
            )

        imposter_suspected = any(
            r.startswith(("DIVERGENT_ENV_NAME", "DIVERGENT_PIP_FINGERPRINT",
                          "UNKNOWN_RUNTIME", "MULTIPLE_PROCESSES"))
            for r in reasons
        )
        if imposter_suspected:
            any_imposter = True

        by_runtime.append({
            "runtime": runtime,
            "checkin_count": r["checkin_count"],
            "first_seen": r.get("first_seen"),
            "last_seen": r.get("last_seen"),
            "distinct_env_names": env_names,
            "distinct_pip_shas": pip_shas,
            "distinct_git_shas": git_shas,
            "distinct_source_ips": source_ips,
            "distinct_process_identities": process_identities,
            "distinct_verdicts": sorted(
                {x for x in (r.get("verdicts") or []) if x}
            ),
            "imposter_suspected": imposter_suspected,
            "reasons": reasons,
        })

    return {
        "ok": True,
        "window_hours": window_hours,
        "env_filter": env_normalized,
        "by_runtime": by_runtime,
        "any_imposter_suspected": any_imposter,
    }
